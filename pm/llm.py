"""
LLM 客户端工厂 + 鲁棒的结构化输出调用。

设计要点：
1. **双通道结构化输出**：优先用 provider 原生 structured output（function calling / JSON schema）；
   一旦不可用（很多 OpenAI 兼容端点、国产模型不支持），自动降级为
   "注入 JSON Schema + 文本解析 + 校验重试"，保证流程不中断。
2. **格式错误重试**：解析失败自动重试，每次降温并附带上一次的错误信息（原文档提到的策略，这里落地）。
3. **按节点调温**：不同节点用不同 temperature，见 TEMPERATURES。
4. **全链路可观测**：每次调用记录 model / temperature / latency / tokens / 是否降级。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, TypeVar

from dotenv import load_dotenv
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, ValidationError

from . import backend

load_dotenv()

logger = logging.getLogger("pm.llm")

T = TypeVar("T", bound=BaseModel)

# 各节点温度：创造性需求高的调高，判定类的压到最低
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
    "clarifier": 1500,
    "optimizer": 4000,
    "mockgen": 2000,
    "evaluator": 3500,
    "evaluator_b": 3500,
    "arbiter": 3500,
    "reviser": 6000,  # 2026-09-12：4000 → 6000。思考型模型的 reasoning 计入 max_tokens，
    # glm-5.2 两次真实运行都在修订环节把预算耗光、返回空内容 → early_stopped；
    # DeepSeek-V4-Flash 实测正常。修订输出 = 整份提示词，是所有角色里最长的，预算必须最宽。
    "comparator": 600,  # 只要 winner + 一句依据，给多了浪费
    "target": 4000,
}
# 说明：思考型模型（如 glm-5.2 / deepseek 系列 reasoning 模式）的推理 token
# 也计入 max_tokens，实测简单请求就可能消耗 600+ 推理 token。
# 2026-09-08 追记（LLM 分配评审）：
# - clarifier 800 → 1500：它要输出 is_clear + 摘要 + 完整推断上下文（4 字段）+
#   问题列表，配思考模型时 800 极易截断（截断 = 解析失败 = 白跑降级通道）；
# - evaluator/evaluator_b/arbiter 2500 → 3500：issues/suggestions 是列表字段，
#   面对长输出 + 多问题时 2500 会截断，后果是评估器悄悄少报问题（评估失真）；
# - mockgen 1600 → 2000：n_test_cases 上限 8 时用例 + rationale 的 JSON 余量。

# 每次重试降温幅度
TEMPERATURE_DECAY = 0.15
# 解析/校验失败时**升温**而不是降温（2026-09-13 第 7 轮实测：降温会让模型更确定，
# 于是把同一份坏 JSON 原样复现，3 次重试全败）。结构性问题要靠多样性突破，不是靠保守。
TEMPERATURE_BOOST_ON_PARSE = 0.1


# --------------------------------------------------------------------------
# 用量台账（token / 调用 / 耗时，按任务隔离）
# --------------------------------------------------------------------------
# 旧版只有 llm_calls 计数：一次真实 e2e 花了多少 token / 哪个角色最贵，
# 只能去翻网关账单人肉算。这里在调用层记一本按角色汇总的台账，
# report_node 收进 state，报告与 /api/status 直接展示。
# 用 ContextVar 而不是全局 dict：target 并发走线程池（carry_context 快照），
# 全局账本会跨任务串账；ContextVar 随 backend.use / carry_context 自动隔离。
@dataclass
class UsageLedger:
    """按角色汇总的用量台账。totals[role] = {calls, input_tokens, output_tokens, latency_ms}。

    target 并发采样走线程池：carry_context 会把 ContextVar 快照（含本对象）带进
    工作线程，多个线程会同时 add() —— 计数必须加锁，否则并发下丢账。
    """

    totals: dict[str, dict[str, int]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add(self, role: str, input_tokens: int, output_tokens: int, latency_ms: int) -> None:
        with self._lock:
            t = self.totals.setdefault(
                role, {"calls": 0, "input_tokens": 0, "output_tokens": 0, "latency_ms": 0}
            )
            t["calls"] += 1
            t["input_tokens"] += max(0, int(input_tokens or 0))
            t["output_tokens"] += max(0, int(output_tokens or 0))
            t["latency_ms"] += max(0, int(latency_ms or 0))

    def snapshot(self) -> dict[str, dict[str, int]]:
        with self._lock:
            return {role: dict(t) for role, t in self.totals.items()}


_LEDGER_VAR: ContextVar[UsageLedger | None] = ContextVar("pm_usage_ledger", default=None)


def current_ledger() -> UsageLedger | None:
    return _LEDGER_VAR.get()


@contextmanager
def usage_scope() -> Iterator[UsageLedger]:
    """在当前上下文（任务）内记录用量；嵌套时追加到外层台账。"""
    outer = _LEDGER_VAR.get()
    ledger = UsageLedger()
    token = _LEDGER_VAR.set(ledger)
    try:
        yield ledger
    finally:
        _LEDGER_VAR.reset(token)
        # 嵌套作用域（如图内并发分支的快照上下文）把账并回父级
        if outer is not None:
            for role, t in ledger.totals.items():
                ot = outer.totals.setdefault(
                    role, {"calls": 0, "input_tokens": 0, "output_tokens": 0, "latency_ms": 0}
                )
                for k in ot:
                    ot[k] += t.get(k, 0)


def _tokens_of(resp: Any) -> tuple[int, int]:
    """从 LangChain 响应里取 (input_tokens, output_tokens)；拿不到就记 0。

    各 provider 字段名不一（usage_metadata.input_tokens / response_metadata.token_usage
    .prompt_tokens），统一在这里兜：宁可 0 也不让记账炸掉主流程。
    """
    try:
        um = getattr(resp, "usage_metadata", None)
        if isinstance(um, dict):
            return int(um.get("input_tokens") or 0), int(um.get("output_tokens") or 0)
        tu = (getattr(resp, "response_metadata", None) or {}).get("token_usage") or {}
        if isinstance(tu, dict):
            return int(tu.get("prompt_tokens") or 0), int(tu.get("completion_tokens") or 0)
    except Exception:  # noqa: BLE001 - 记账绝不影响主流程
        pass
    return 0, 0


def _record_usage(role: str, resp: Any, latency_ms: int) -> None:
    """成功调用后记一笔；台账不在作用域内（如单测直调）则跳过。"""
    ledger = current_ledger()
    if ledger is None:
        return
    inp, out = _tokens_of(resp)
    ledger.add(role, inp, out, latency_ms)


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


def _int_env(key: str, default: int) -> int:
    """整型环境变量容错。

    旧版直接 `int(_env(k, "5"))`：`.env` 里写了空值（`PM_TIMEOUT=`）就
    `ValueError: invalid literal for int()`，每次调用都抛，整条流水线全线 failed（A2）。
    """
    raw = _env(key)
    if not raw:
        return default
    try:
        return int(float(raw))
    except ValueError:
        logger.warning("环境变量 %s=%r 不是整数，回退默认值 %s", key, raw, default)
        return default


def _float_env(key: str, default: float) -> float:
    """浮点环境变量容错，理由同 _int_env。"""
    raw = _env(key)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("环境变量 %s=%r 不是数字，回退默认值 %s", key, raw, default)
        return default


# --------------------------------------------------------------------------
# API Key 池：支持 PM_API_KEYS="sk-a,sk-b,sk-c" 多 Key 轮换，
# 单 Key 触发 429（rpm/tpm exhausted）时自动切换下一个，避免流水线被配额卡死。
# --------------------------------------------------------------------------
_KEY_POOL: list[str] = []
_KEY_CURSOR: int = 0
# 并发安全：target 并发化后多个线程会同时取 Key / 改游标，必须加锁
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


# 429 退避参数：首次等 5s，之后指数递增，最多 RETRY_ON_429 次
# （这两个在 import 期求值，所以必须走容错解析，否则 `PM_RATE_LIMIT_RETRIES=` 空值会让服务根本起不来）
RATE_LIMIT_RETRIES = _int_env("PM_RATE_LIMIT_RETRIES", 5)
RATE_LIMIT_BASE_SLEEP = _float_env("PM_RATE_LIMIT_BASE_SLEEP", 5.0)
# 单次调用的退避总时长上限（A3）：旧版 5/10/20/40/80 累加可达 155s（单 Key 加倍后 310s），
# 而通道 A 耗尽后还有通道 B 的 3 次重试各自再跑一轮退避，最坏能阻塞十几分钟。
# 现在给每个调用点一个硬预算，超了就上抛，宁叫失败不叫卡死。
RATE_LIMIT_MAX_WAIT = _float_env("PM_RATE_LIMIT_MAX_WAIT", 60.0)


def _is_rate_limit_error(e: Exception) -> bool:
    """限流判定。

    旧版用 `"429" in text` 子串，实测 `"Error code: 400 - used 100429 tokens"` 会被当成限流，
    白退避 155s（A4）。这里改为先看 status_code，再匹配带上下文的限流短语。
    """
    code = getattr(e, "status_code", None) or getattr(
        getattr(e, "response", None), "status_code", None
    )
    if code == 429:
        return True
    text = f"{type(e).__name__}: {e}".lower()
    if any(k in text for k in ("ratelimit", "rate limit", "too many requests", "quota exceeded")):
        return True
    # 兼容只往 message 里拼状态码的兼容层：只接受带错误码前缀的形式，不接受任意位置的 429
    return any(
        k in text for k in ("error code: 429", "code: 429", "status_code: 429", "429 too many")
    )


# 瞬时连接故障（网关抖动 / 连接被重置 / 读超时 / 5xx 网关错误）与 429 是两类问题：
# 429 是配额，换 Key 有意义；连接故障换 Key 无用，但**短退避原地重试往往就能过**。
# 2026-09-13 第 6 轮实测：一次 48 分钟运行里 4 次盲评全灭 + 多次评估失败，
# 错误签名全是 OpenAIConnectionError: Connection error. —— 只重试 429 的策略白丢了一整轮。
_TRANSIENT_CONN_HINTS = (
    "connection error",
    "connection reset",
    "connection aborted",
    "connecterror",
    "readtimeout",
    "read timeout",
    "remotedisconnected",
    "server disconnected",
    "502 bad gateway",
    "503 service",
    "504 gateway",
)
TRANSIENT_CONN_RETRIES = _int_env("PM_CONN_RETRIES", 2)
TRANSIENT_CONN_BASE_SLEEP = _float_env("PM_CONN_BASE_SLEEP", 1.5)


def _is_transient_conn_error(e: Exception) -> bool:
    """连接类瞬时故障（可原地重试），与限流、与"请求本身有问题"都区分开。"""
    text = f"{type(e).__name__}: {e}".lower()
    return any(k in text for k in _TRANSIENT_CONN_HINTS)


def _invoke_with_conn_retry(role: str, call: Any) -> Any:
    """执行一次调用；瞬时连接故障按 1.5s/3s 短退避重试（默认 2 次），其余异常直接上抛。"""
    for i in range(TRANSIENT_CONN_RETRIES + 1):
        try:
            return call()
        except Exception as e:
            if not _is_transient_conn_error(e) or i >= TRANSIENT_CONN_RETRIES:
                raise
            sleep_s = TRANSIENT_CONN_BASE_SLEEP * (2**i)
            logger.warning(
                "[%s] 瞬时连接故障（%s），%.1fs 后重试 %d/%d",
                role,
                type(e).__name__,
                sleep_s,
                i + 1,
                TRANSIENT_CONN_RETRIES,
            )
            time.sleep(sleep_s)
    raise AssertionError("unreachable")  # pragma: no cover


def _invoke_with_rate_limit_retry(
    role: str,
    invoke_fn,  # Callable[[BaseChatModel], Any]：接收 llm 实例执行一次调用
    overrides: dict[str, Any] | None = None,
):
    """带 429 退避与 Key 轮换的调用封装。

    每次撞到限流：先退避等待，再轮换到 Key 池中的下一个 Key 重建客户端重试；
    Key 池只有 1 个 Key 时退避时间加倍，给上游配额留恢复窗口。
    非限流异常直接上抛，不吞错。

    退避总时长受 `PM_RATE_LIMIT_MAX_WAIT` 约束（A3）：预算用尽就不再死等，直接上抛最后错误。
    连接类瞬时故障单独处理：原地短退避重试（`PM_CONN_RETRIES` / `PM_CONN_BASE_SLEEP`）。
    """
    last_exc: Exception | None = None
    wait_deadline = time.monotonic() + max(0.0, RATE_LIMIT_MAX_WAIT)
    for attempt in range(RATE_LIMIT_RETRIES + 1):
        if attempt > 0:
            sleep_s = RATE_LIMIT_BASE_SLEEP * (2 ** (attempt - 1))
            pool = _key_pool(role)
            if len(pool) <= 1:
                sleep_s *= 2
            if time.monotonic() + sleep_s > wait_deadline:
                logger.warning(
                    "[%s] 限流退避预算已用完（%.0fs，本次还需等 %.0fs），停止重试并上抛最后错误",
                    role,
                    RATE_LIMIT_MAX_WAIT,
                    sleep_s,
                )
                break
            logger.warning(
                "[%s] 疑似限流，第 %d/%d 次退避 %.0fs 后重试（Key 池大小 %d）",
                role,
                attempt,
                RATE_LIMIT_RETRIES,
                sleep_s,
                len(pool),
            )
            time.sleep(sleep_s)
            api_key: str | None = None
            if len(pool) > 1:
                global _KEY_CURSOR
                with _KEY_LOCK:
                    api_key = pool[_KEY_CURSOR % len(pool)]
                    _KEY_CURSOR += 1
                logger.info("[%s] 轮换到 Key 池第 %d 个 Key 重试", role, _KEY_CURSOR % len(pool))
            llm = get_llm(role, {"api_key": api_key} if api_key else None)
        else:
            llm = get_llm(role, overrides)
        try:
            t0 = time.time()
            # 默认参数绑定当前轮的 llm，避免闭包捕获循环变量（B023）
            resp = _invoke_with_conn_retry(role, lambda _llm=llm: invoke_fn(_llm))
            # 记账只记真实成功的调用（429 退避后的重试、双通道各自的真实请求都覆盖；
            # 演示/测试钩子在 structured_call/plain_call 入口就返回，不会到这里）
            _record_usage(role, resp, int((time.time() - t0) * 1000))
            return resp
        except Exception as e:
            if _is_rate_limit_error(e):
                last_exc = e
                continue
            raise
    raise last_exc  # type: ignore[misc]


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
                logger.warning(
                    "评委B与评委A同模型同端点（%s @ %s）：交叉验证去同质化不足，%s。"
                    "建议为 PM_EVALUATOR_B_MODEL 配置不同模型家族（如 A 用 DeepSeek、B 用 GLM）",
                    a.model,
                    a.base_url or "(默认端点)",
                    f"已将B温度自动升至 {cfg.temperature:.1f} 以降低采样相关性"
                    if not explicit_temp
                    else "温度为显式配置未改动",
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
        max_tokens=_int_env(f"PM_{role_u}_MAX_TOKENS", MAX_TOKENS.get(role, 2000)),
        timeout=_int_env("PM_TIMEOUT", 120),
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
        )

    from langchain_openai import ChatOpenAI

    kwargs: dict[str, Any] = {
        "api_key": cfg.api_key,
        "model": cfg.model,
        "temperature": cfg.temperature,
        "max_tokens": cfg.max_tokens,
        "timeout": cfg.timeout,
    }
    if cfg.base_url:
        kwargs["base_url"] = cfg.base_url
    return ChatOpenAI(**kwargs)


# --------------------------------------------------------------------------
# JSON 提取：从可能带围栏/前后缀的文本里抠出第一个合法 JSON 对象
# --------------------------------------------------------------------------
def extract_json_object(text: str) -> dict | None:
    if not text:
        return None
    # 优先处理 ```json 围栏
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        try:
            return json.loads(fenced.group(1))
        except json.JSONDecodeError:
            pass

    # 括号配平扫描，避免贪婪匹配截断嵌套对象
    start = text.find("{")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start : i + 1])
                    except json.JSONDecodeError:
                        break
        start = text.find("{", start + 1)
    return None


def _schema_hint(model: type[BaseModel]) -> str:
    schema = model.model_json_schema()
    return json.dumps(schema, ensure_ascii=False, indent=2)


class _ChannelASkipped(RuntimeError):
    """主动跳过原生结构化输出通道（不是故障，不走告警日志）。"""


_UNSUPPORTED_A: set[str] = set()
_UNSUPPORTED_A_LOCK = threading.Lock()


def _capability_key(cfg: LLMConfig) -> str:
    return f"{cfg.provider}|{cfg.base_url}|{cfg.model}"


# 通道 A 的结构化输出方法（A10）：
#   空      → 用 langchain-openai 的默认（1.6.0 是 method="json_schema"）；
#   指定后  → 显式传给 with_structured_output。
# 为什么需要开关：langchain-openai 默认走 json_schema，请求体里根本不带 tools，
# 桩服务/端点的 function-calling 分支永远触发不到（"双通道"实际 0 覆盖其中一条）。
STRUCTURED_METHODS = ("", "json_schema", "function_calling")


def _structured_method() -> str:
    """读取 PM_STRUCT_METHOD（容错：非法值告警并忽略，保持默认行为）。"""
    raw = _env("PM_STRUCT_METHOD").lower()
    if raw not in STRUCTURED_METHODS:
        logger.warning(
            "PM_STRUCT_METHOD=%r 无效（可选：json_schema / function_calling），忽略并使用默认方法",
            raw,
        )
        return ""
    return raw


def _skip_channel_a(cfg: LLMConfig) -> str | None:
    """该端点是否应直接走文本通道。返回跳过原因（None = 照常尝试）。"""
    if _int_env("PM_FORCE_JSON_CHANNEL", 0) > 0:
        return "PM_FORCE_JSON_CHANNEL=1 已强制使用文本通道"
    key = _capability_key(cfg)
    with _UNSUPPORTED_A_LOCK:
        if key in _UNSUPPORTED_A:
            return f"端点 {key} 此前已报不兼容结构化输出，本次直接降级"
    return None


def _remember_channel_a_unsupported(cfg: LLMConfig, err: str) -> None:
    """记住「这个端点不支持结构化输出」，避免每次调用先白打一个 400 请求（M7）。

    只记确定性的不兼容错语；网络抖动 / 限流 / 5xx 不能记，
    否则一次偶发错误会把原生通道永久关掉。
    """
    low = err.lower()
    if not any(
        k in low for k in ("does not support", "not supported", "unsupported", "json_schema")
    ):
        return
    if not any(k in low for k in ("400", "invalid_request", "bad request")):
        return
    key = _capability_key(cfg)
    with _UNSUPPORTED_A_LOCK:
        fresh = key not in _UNSUPPORTED_A
        if fresh:
            _UNSUPPORTED_A.add(key)
    if fresh:
        logger.info("已记住端点 %s 不支持原生结构化输出，后续调用直接走文本通道", key)


def structured_call(
    role: str,
    model_cls: type[T],
    system: str,
    user: str,
    max_retries: int = 3,
    overrides: dict[str, Any] | None = None,
) -> tuple[T, dict[str, Any]]:
    """调用 LLM 并强制拿到 model_cls 实例。

    通道 A：provider 原生 structured output
    通道 B：Schema 注入 + 文本解析（自动降级）

    返回 (实例, 调用元信息)。失败抛 RuntimeError。
    """
    # 演示模式 / 自测注入的钩子：只影响当前线程或当前异步任务，不改动模块属性（C4）
    hook = backend.current()
    if hook is not None and hook.structured is not None:
        return hook.structured(  # type: ignore[no-any-return]
            role, model_cls, system, user, max_retries=max_retries, overrides=overrides
        )

    cfg = build_config(role, overrides)
    meta: dict[str, Any] = {
        "role": role,
        "model": cfg.model,
        "channel": None,
        "attempts": 0,
        "latency_ms": None,
        "temperature": cfg.temperature,
    }

    t0 = time.time()
    last_err: str | None = None

    # ---- 通道 A ----
    # method 只有在显式配置时才传：不同 provider 的 with_structured_output
    # 签名不一致（如 ChatAnthropic 没有 method 形参），默认路径不能多传参。
    method = _structured_method()
    channel_a = "function_calling" if method == "function_calling" else "structured_output"
    so_kwargs: dict[str, Any] = {"include_raw": False}
    if method:
        so_kwargs["method"] = method
    try:
        skip_reason = _skip_channel_a(cfg)
        if skip_reason:
            raise _ChannelASkipped(skip_reason)
        result = _invoke_with_rate_limit_retry(
            role,
            lambda llm: llm.with_structured_output(model_cls, **so_kwargs).invoke(
                [SystemMessage(content=system), HumanMessage(content=user)]
            ),
            overrides,
        )
        if isinstance(result, model_cls):
            meta.update(channel=channel_a, attempts=1, latency_ms=int((time.time() - t0) * 1000))
            return result, meta
        if isinstance(result, dict):
            validated = model_cls.model_validate(result)
            meta.update(channel=channel_a, attempts=1, latency_ms=int((time.time() - t0) * 1000))
            return validated, meta
        # 端点"支持"结构化输出却返回了意外类型（常见：None / 空内容）。
        # 必须显式记录再降级，否则故障原因会在日志里彻底消失。
        last_err = (
            f"原生结构化输出返回非预期类型: {type(result).__name__}"
            f"（期望 {model_cls.__name__} 或 dict）"
        )
        logger.warning("[%s] %s，降级为 JSON 文本解析", role, last_err)
    except _ChannelASkipped as e:
        last_err = f"跳过原生结构化输出：{e}"
        logger.debug("[%s] %s", role, last_err)
    except Exception as e:  # noqa: BLE001
        last_err = f"{type(e).__name__}: {e}"
        logger.warning("[%s] 原生结构化输出不可用，降级为 JSON 文本解析：%s", role, last_err)
        _remember_channel_a_unsupported(cfg, last_err)

    # ---- 通道 B：注入 Schema + 解析重试 ----
    schema_block = (
        "\n\n<json_schema>\n"
        "你必须严格输出一个符合以下 JSON Schema 的 JSON 对象，且只输出该 JSON 对象，"
        "不要任何解释、不要 Markdown 围栏：\n"
        f"{_schema_hint(model_cls)}\n"
        "</json_schema>"
    )
    temp = cfg.temperature

    for attempt in range(1, max_retries + 1):
        retry_hint = ""
        if last_err:
            retry_hint = (
                f"\n\n<previous_error>\n上一次输出解析失败：{last_err}\n"
                "请修正后重新输出，只输出 JSON 对象本身。\n</previous_error>"
            )
        messages = [
            SystemMessage(content=system + schema_block),
            HumanMessage(content=user + retry_hint),
        ]
        try:
            # 闭包按默认参数绑定当前轮的 temp/messages，避免 B023 循环变量捕获
            resp = _invoke_with_rate_limit_retry(
                role,
                lambda llm, _t=temp, _msgs=messages: llm.with_config(
                    temperature=max(_t, 0.0)
                ).invoke(_msgs),
                overrides,
            )
            raw = resp.content if isinstance(resp.content, str) else str(resp.content)
            data = extract_json_object(raw)
            if data is None:
                raise ValueError("未在输出中找到合法 JSON 对象")
            validated = model_cls.model_validate(data)
            meta.update(
                channel="json_fallback", attempts=attempt, latency_ms=int((time.time() - t0) * 1000)
            )
            return validated, meta
        except (ValidationError, ValueError, json.JSONDecodeError) as e:
            # 解析失败要两条一起改：①把缺哪些顶层键写进下一次提示（模型常是漏字段，不是乱写）
            # ②升温而不是降温 —— 降温会让模型更确定地复现同一份坏 JSON
            need = ", ".join(sorted((getattr(model_cls, "model_fields", None) or {}).keys())[:12])
            last_err = f"{type(e).__name__}: {e}"
            if need:
                last_err += f"。输出必须包含这些顶层键：{need}"
            logger.warning("[%s] 第 %d/%d 次解析失败：%s", role, attempt, max_retries, last_err)
            temp = min(1.0, temp + TEMPERATURE_BOOST_ON_PARSE)
        except Exception as e:  # noqa: BLE001 - LLM 端点行为不可预期，任何异常都重试降温
            last_err = f"{type(e).__name__}: {e}"
            logger.warning("[%s] 第 %d/%d 次调用异常：%s", role, attempt, max_retries, last_err)
            # 端点/网络类异常沿用降温：这类失败与采样随机性无关，保守重试即可
            temp = max(0.0, temp - TEMPERATURE_DECAY)

    meta.update(channel="failed", attempts=max_retries, latency_ms=int((time.time() - t0) * 1000))
    raise RuntimeError(f"[{role}] 结构化输出失败，已重试 {max_retries} 次。最后错误：{last_err}")


def plain_call(
    role: str, system: str, user: str, overrides: dict[str, Any] | None = None
) -> tuple[str, dict[str, Any]]:
    """纯文本调用，用于 Optimizer / Reviser / 被测目标模型。"""
    hook = backend.current()
    if hook is not None and hook.plain is not None:
        return hook.plain(role, system, user, overrides=overrides)

    cfg = build_config(role, overrides)
    t0 = time.time()
    resp = _invoke_with_rate_limit_retry(
        role,
        lambda llm: llm.invoke([SystemMessage(content=system), HumanMessage(content=user)]),
        overrides,
    )
    text = resp.content if isinstance(resp.content, str) else str(resp.content)
    meta = {
        "role": role,
        "model": cfg.model,
        "channel": "plain",
        "attempts": 1,
        "latency_ms": int((time.time() - t0) * 1000),
        "temperature": cfg.temperature,
    }
    return text, meta
