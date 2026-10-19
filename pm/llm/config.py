"""配置与客户端构造：环境变量、角色温度/预算、Key 池、`build_config` / `get_llm`。

2026-10-19 从 `pm/llm.py` 拆出。**这里在模块级读环境变量**，所以 `load_dotenv()` 必须在本文件
常量定义之前执行 —— 这是本包唯一有"导入顺序要求"的模块。
"""

from __future__ import annotations

import hashlib
import os
import threading
from typing import Any

from dotenv import load_dotenv
from langchain_core.language_models import BaseChatModel
from pydantic import BaseModel

from ._log import logger

load_dotenv()


TEMPERATURES: dict[str, float] = {
    "clarifier": 0.2,
    "optimizer": 0.4,
    "mockgen": 0.8,
    "evaluator": 0.1,
    "evaluator_b": 0.1,  # 第二评委（双评委交叉验证）
    "arbiter": 0.0,  # 仲裁评委：分差过大时复核，温度压最低
    "reviser": 0.3,
    "comparator": 0.0,  # 成对盲评：只要选边，温度压到最低保可复现
    "target": 0.7,
}


MAX_TOKENS: dict[str, int] = {
    # 2026-09-14 全部上调：**思考型模型把 reasoning 计入 max_tokens**，
    # 实测 reasoning 消耗约为 prompt 长度的 1.1~1.5 倍（第 11 轮 evaluator 实测
    # prompt≈3.5k → reasoning 5477~6000），正文额度极易归零 → 空返回或 LengthFinish。
    #
    # 直接对照（第 11 轮，同模型/同端点/同任务/同调用次数 15 vs 15）：
    #     evaluator   max_tokens=6000 → 额度耗尽 **5 次**（33%）    ← 本轮代码默认值
    #     evaluator_b max_tokens=8000 → 额度耗尽 **0 次**（0%）     ← 来自 .env
    #   → evaluator 由 6000 提到 8000（与 evaluator_b 对齐）。
    # 其余角色按同一口径推算（reasoning ≈ prompt × 1.5 + 正文余量），**无逐角色对照**，
    # 标注为推算值；后续任一轮日志发现某角色仍告警，按该值再调。
    "clarifier": 3000,  # 原 1500：实测 reasoning=1500 恰好吃满，正文 0
    "optimizer": 6000,  # 原 4000：与 reviser 同级（整份提示词）
    "mockgen": 4000,  # 原 2000：n 上限 8 时用例 + rationale，需留推理余量
    "evaluator": 24000,  # 8000→24000（2026-09-27，有对照）：人工锚点校准中空产出类
    # 锚点（空壳日报/纯拒答）在最难评的输入上反复 reasoning 耗尽 → 空 JSON → 重试 3 次
    # 全灭（每次重试都计费，比一次长调用更贵）；24000 下同批锚点成功出分。
    # max_tokens 是上限不是目标——正常调用不会因上限抬高而多花钱。
    "evaluator_b": 24000,  # 对齐 evaluator
    "arbiter": 8000,  # 同 evaluator 类角色
    "reviser": 6000,  # 2026-09-12：4000 → 6000。思考型模型的 reasoning 计入 max_tokens，
    # glm-5.2 两次真实运行都在修订环节把预算耗光、返回空内容 → early_stopped；
    # DeepSeek-V4-Flash 实测正常。修订输出 = 整份提示词，是所有角色里最长的，预算必须最宽。
    "comparator": 2500,  # 原 600：连一次推理都不够（第 11 轮 comparator 从未走通原生通道）
    # 2026-09-16：4000 → 8000。上一轮复核时**漏了这个角色**。
    # 矩阵 A（客服分诊）暴露：基线 8 条里 4 条 output 为空（len=0），
    # error=None + latency 58~71s = reasoning 吃满预算的空返回。
    # 直接对照（scripts/target_budget_probe.py，同输入 × 3）：
    #     4000 → 非空 **0/3**；8000 → 非空 **3/3**（930/1162/933 字符）
    # 影响：空输出被当低分 → **基线被系统性低估**，Δ 被夸大（矩阵 A 的 +6.02 即含此偏差）。
    "target": 8000,
}


TEMPERATURE_DECAY = 0.15


class LLMConfig(BaseModel):
    provider: str = "openai"  # openai | anthropic
    api_key: str = ""
    base_url: str | None = None
    model: str = "gpt-4o-mini"
    temperature: float = 0.3
    max_tokens: int = 2000
    timeout: int = 120


def _env(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


def int_env(key: str, default: int) -> int:
    """整型环境变量容错。

    旧版直接 `int(_env(k, "5"))`：`.env` 里写了空值（`PM_TIMEOUT=`）就
    `ValueError: invalid literal for int()`，每次调用都抛，整条流水线全线 failed（A2）。
    小数串按宽口径解析（`"12.9"` → 12，`test_llm_branches` 钉住的契约）；
    `int(float())` 对 `"1e999"` 会抛 OverflowError，与 ValueError 一并回退。
    曾经 pm/scheduler.py 有一份 `int(raw)` 严格版副本——同名不同义，已并到这里。
    """
    raw = _env(key)
    if not raw:
        return default
    try:
        return int(float(raw))
    except (ValueError, OverflowError):
        logger.warning("环境变量 %s=%r 不是整数，回退默认值 %s", key, raw, default)
        return default


_int_env = int_env


def _float_env(key: str, default: float) -> float:
    """浮点环境变量容错，理由同 int_env。"""
    raw = _env(key)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("环境变量 %s=%r 不是数字，回退默认值 %s", key, raw, default)
        return default


_KEY_POOL: list[str] = []


_KEY_CURSOR: int = 0


_KEY_LOCK = threading.Lock()


def _key_pool(cfg_role: str) -> list[str]:
    """构造当前可用的 Key 池。

    规则（跨端点隔离）：
    - 角色配置了独立 Key（PM_<ROLE>_API_KEY）→ 池 = [该 Key]。
      角色通常也配了独立 base_url（如 SenseNova），此时**绝不能**混入全局 Key，
      否则 429 退避轮换会拿别的端点 Key 打过来 → 401。
    - 角色无独立 Key → 池 = 全局 Key + PM_API_KEYS（同一端点多 Key 轮换）。
    """
    role_key = _env(f"PM_{cfg_role.upper()}_API_KEY")
    if role_key:
        return [role_key]
    keys: list[str] = []
    global_key = _env("PM_API_KEY")
    if global_key:
        keys.append(global_key)
    pool = [k.strip() for k in _env("PM_API_KEYS").split(",") if k.strip()]
    for k in pool:
        if k not in keys:
            keys.append(k)
    return keys


def _next_key(role: str) -> str:
    """轮换取下一个 Key（带游标，进程内均匀分摊负载）。线程安全。"""
    global _KEY_CURSOR
    pool = _key_pool(role)
    if not pool:
        return ""
    with _KEY_LOCK:
        key = pool[_KEY_CURSOR % len(pool)]
        _KEY_CURSOR += 1
    return key


RATE_LIMIT_RETRIES = max(0, int_env("PM_RATE_LIMIT_RETRIES", 5))


RATE_LIMIT_BASE_SLEEP = _float_env("PM_RATE_LIMIT_BASE_SLEEP", 5.0)


RATE_LIMIT_MAX_WAIT = _float_env("PM_RATE_LIMIT_MAX_WAIT", 60.0)


RATE_LIMIT_TOTAL_WAIT = _float_env("PM_RATE_LIMIT_TOTAL_WAIT", 120.0)


TRANSIENT_CONN_RETRIES = max(0, int_env("PM_CONN_RETRIES", 2))


TRANSIENT_CONN_BASE_SLEEP = _float_env("PM_CONN_BASE_SLEEP", 1.5)


SDK_RETRIES = max(0, int_env("PM_SDK_RETRIES", 0))


CALL_BUDGET = max(0.0, _float_env("PM_CALL_BUDGET", 600.0))


DNS_BUDGET = max(0.0, _float_env("PM_DNS_BUDGET", 5.0))


TEMPERATURE_BOOST_ON_PARSE = _float_env("PM_TEMP_BOOST_ON_PARSE", 0.0)


_JUDGE_B_SAME_WARN_LOCK = threading.Lock()


_JUDGE_B_SAME_WARNED = False


def _decorrelate_judge_b(cfg: LLMConfig) -> LLMConfig:
    """评委 B 与评委 A 完全同源时的去同质化处理（M5 从"文档承认"升级为代码兜底）。

    未单独配置 PM_EVALUATOR_B_* 时，评委 B 会与 A 同模型同端点同温度——
    "双评委交叉验证"退化为同一配置自评两遍，防放水机制名存实亡。
    处理：
    - 用户显式配了 PM_EVALUATOR_B_TEMPERATURE → 尊重配置，只告警不改动；
    - 否则（静默回退到与 A 逐项相同）→ 温度自动 +0.1，让两次采样至少不完全相关。
    均为每进程只告警一次（build_config 每次调用都会进来，不能刷屏）。
    注意：温度变化会进入 call_fingerprint，旧评估缓存会一次性失效——这是预期行为。
    """
    global _JUDGE_B_SAME_WARNED
    a = build_config("evaluator")
    identical = (
        cfg.model == a.model and cfg.base_url == a.base_url and cfg.temperature == a.temperature
    )
    if not identical:
        return cfg
    explicit_temp = _env("PM_EVALUATOR_B_TEMPERATURE")
    if not explicit_temp:
        cfg.temperature = min(1.0, round(cfg.temperature + 0.1, 2))
    if not _JUDGE_B_SAME_WARNED:
        with _JUDGE_B_SAME_WARN_LOCK:
            if not _JUDGE_B_SAME_WARNED:
                _JUDGE_B_SAME_WARNED = True
                # 温度已是 1.0 时 +0.1 被 min 截断，实际没升：文案必须如实说明，
                # 否则"已自动升至 1.0"会误导（且 1.0 与 0.9 的去相关本就有限）。
                if not explicit_temp and cfg.temperature >= 1.0:
                    mitigation = "B 温度已到上限 1.0 无法再升，去同质化实际失效"
                elif not explicit_temp:
                    mitigation = f"已将B温度自动升至 {cfg.temperature:.1f} 以降低采样相关性"
                else:
                    mitigation = "温度为显式配置未改动"
                logger.warning(
                    "评委B与评委A同模型同端点（%s @ %s）：交叉验证去同质化不足，%s。"
                    "建议为 PM_EVALUATOR_B_MODEL 配置不同模型家族（如 A 用 DeepSeek、B 用 GLM）",
                    a.model,
                    a.base_url or "(默认端点)",
                    mitigation,
                )
    return cfg


def build_config(role: str, overrides: dict[str, Any] | None = None) -> LLMConfig:
    """按角色构造配置。环境变量约定：

    PM_<ROLE>_MODEL / PM_<ROLE>_BASE_URL / PM_<ROLE>_API_KEY 可覆盖全局，
    用于把「优化器用强模型、评估器用便宜模型」的部署建议真正落地。
    """
    role_u = role.upper()
    cfg = LLMConfig(
        provider=_env(f"PM_{role_u}_PROVIDER") or _env("PM_PROVIDER", "openai"),
        api_key=_env(f"PM_{role_u}_API_KEY") or _env("PM_API_KEY", ""),
        base_url=_env(f"PM_{role_u}_BASE_URL") or _env("PM_BASE_URL") or None,
        model=_env(f"PM_{role_u}_MODEL") or _env("PM_MODEL", "gpt-4o-mini"),
        temperature=_float_env(f"PM_{role_u}_TEMPERATURE", TEMPERATURES.get(role, 0.3)),
        max_tokens=int_env(f"PM_{role_u}_MAX_TOKENS", MAX_TOKENS.get(role, 2000)),
        timeout=int_env("PM_TIMEOUT", 120),
    )
    if overrides:
        for k, v in overrides.items():
            if v is not None and hasattr(cfg, k):
                setattr(cfg, k, v)
    if role == "evaluator_b":
        # 与评委 A 同源时去同质化（M5 兜底）：静默回退场景下自动温度 +0.1 并告警
        cfg = _decorrelate_judge_b(cfg)
    return cfg


def call_fingerprint(role: str, overrides: dict[str, Any] | None = None) -> str:
    """某角色本次实际生效的调用配置指纹（模型 / 端点 / 采样参数）。

    用于本地缓存键：旧版键只含写进 prompt 文本的目标模型标签，换了真实模型、
    改了 temperature / max_tokens / base_url 后仍命中旧缓存，参数改动被整体冻结（H1）。
    """
    cfg = build_config(role, overrides)
    raw = f"{cfg.provider}|{cfg.model}|{cfg.base_url}|{cfg.temperature}|{cfg.max_tokens}|{cfg.timeout}"
    return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:16]


def get_llm(role: str, overrides: dict[str, Any] | None = None) -> BaseChatModel:
    # 从包命名空间取：测试用 pm.llm.SDK_RETRIES 注入（见 __init__.py 的兼容说明）。
    from . import SDK_RETRIES as sdk_retries

    cfg = build_config(role, overrides)
    if not cfg.api_key:
        raise RuntimeError(
            f"缺少 API Key：请设置 PM_API_KEY（或 PM_{role.upper()}_API_KEY）。"
            f"可复制 .env.example 为 .env 后填写。"
        )

    if cfg.provider == "anthropic":
        from langchain_anthropic import ChatAnthropic  # 延迟导入，未安装也不影响主流程

        return ChatAnthropic(
            api_key=cfg.api_key,
            model=cfg.model,
            temperature=cfg.temperature,
            max_tokens=cfg.max_tokens,
            timeout=cfg.timeout,
            max_retries=sdk_retries,  # 不传就是 SDK 缺省的 2 次隐式重试（见 SDK_RETRIES 注释）
        )

    from langchain_openai import ChatOpenAI

    kwargs: dict[str, Any] = {
        "api_key": cfg.api_key,
        "model": cfg.model,
        "temperature": cfg.temperature,
        "max_tokens": cfg.max_tokens,
        "timeout": cfg.timeout,
        "max_retries": sdk_retries,
    }
    if cfg.base_url:
        kwargs["base_url"] = cfg.base_url
    return ChatOpenAI(**kwargs)
