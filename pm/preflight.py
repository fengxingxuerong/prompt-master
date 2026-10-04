"""运行前端点预检（--preflight）：用最小真实调用探明各角色端点的健康状况。

为什么需要它：真实跑积累的运维性格——IP 级限流冷却要 5 分钟以上、长纯文本
调用偶发 504、晚高峰可能全池 429——以前只靠人记忆和错峰经验。批任务烧到一半
撞限流，浪费的不只是额度还有时间。预检用一次 64 token 的冒烟调用逐角色探测，
把「现在能不能跑」的判断提前到花大钱之前。

输出两块：
1. 角色配置摘要（脱敏）：模型 / 端点 / Key 尾号——核对"我以为配的"和"实际生效的"
   是不是同一个东西（历史教训：角色级配置漏覆盖时 .env 会把真实端点注回来）；
2. 逐角色冒烟结果：成功带延迟与 token 用量，失败带错误签名归类
   （限流 / 网关超时 / 鉴权 / 其他），并给出对症建议。

设计要点：
- 冒烟调用走 pm.llm.plain_call（64 token 覆盖），演示/测试钩子天然生效，可离线测试；
- 评委 B 只在 PM_JUDGES=2 时探测（单评委档没有这个角色）；
- 成对盲评默认开启时 comparator 也探测（它与 evaluator 同配置的概率高，但显式列出更诚实）；
- 不替用户做"换端点"的决策：只报告事实与建议，退出码反映健康状态。
"""

from __future__ import annotations

import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from .backend import carry_context
from .llm import _env, _float_env, _int_env, build_config, plain_call

logger = logging.getLogger("pm.preflight")

# 错误签名归类：预检的价值不在"报错"而在"告诉你现在该等还是该改配置"
_ERROR_SIGNATURES: list[tuple[str, str]] = [
    # (签名子串, 对症建议)
    (
        "rate limit",
        "IP/账号级限流：实测冷却需 5 分钟以上（Key 池轮换无效），错峰后再跑",
    ),
    (
        "504",
        "网关超时：该端点对当前调用形态不稳定，建议错峰或换角色端点",
    ),
    (
        "timeout",
        "调用超时：检查网络或调大 PM_TIMEOUT（一次调用的总墙钟还受 PM_CALL_BUDGET 约束，"
        "真要长跑两个都得给够）",
    ),
    (
        "401",
        "鉴权失败：核对 PM_<角色>_API_KEY 与端点是否匹配",
    ),
    (
        "404",
        "模型或路径不存在：核对 PM_<角色>_MODEL 是否为该端点支持的模型名",
    ),
    (
        "insufficient",
        "余额不足：该 Key 配额已耗尽，换 Key 或充值",
    ),
]


@dataclass
class RoleReport:
    """单个角色的预检结果。"""

    role: str
    model: str
    base_url: str
    key_tail: str  # Key 尾号（脱敏，只展示最后 4 位；无 Key 则为空）
    ok: bool = False
    latency_ms: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    error: str | None = None
    error_kind: str | None = None  # rate_limit / gateway / auth / not_found / quota / other
    advice: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "model": self.model,
            "base_url": self.base_url,
            "key_tail": self.key_tail,
            "ok": self.ok,
            "latency_ms": self.latency_ms,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "error": self.error,
            "error_kind": self.error_kind,
            "advice": self.advice,
        }


@dataclass
class PreflightReport:
    """全部角色的预检汇总。"""

    roles: list[RoleReport] = field(default_factory=list)

    @property
    def all_ok(self) -> bool:
        return bool(self.roles) and all(r.ok for r in self.roles)

    def as_dict(self) -> dict[str, Any]:
        return {"all_ok": self.all_ok, "roles": [r.as_dict() for r in self.roles]}


def _classify_error(err: str) -> tuple[str, str | None]:
    """错误签名归类，返回 (kind, advice)。"""
    low = err.lower()
    for sig, advice in _ERROR_SIGNATURES:
        if sig in low:
            kind = {
                "rate limit": "rate_limit",
                "504": "gateway",
                "timeout": "timeout",
                "401": "auth",
                "404": "not_found",
                "insufficient": "quota",
            }[sig]
            return kind, advice
    return "other", None


def _mask_key(key: str) -> str:
    """Key 脱敏：只留尾号 4 位。凭据绝不完整出现在输出里。"""
    key = (key or "").strip()
    if not key:
        return ""
    return f"…{key[-4:]}" if len(key) > 4 else "…"


def preflight_roles() -> list[str]:
    """按当前配置列出需要探测的角色（含条件角色：评委 B / 成对盲评）。"""
    roles = ["clarifier", "optimizer", "mockgen", "evaluator", "reviser", "target"]
    try:
        n_judges = max(1, min(2, int(os.getenv("PM_JUDGES", "2"))))
    except ValueError:
        n_judges = 2
    if n_judges >= 2:
        roles.append("evaluator_b")
        # 仲裁评委必须在预检里：它平时不被调用，只在双评委分差超阈值时才出场——
        # 配置写错（模型名/额度/端点）会正好在跑批中途、最贵的那一刻才暴露。
        roles.append("arbiter")
    if (_env("PM_PAIRWISE") or "1") != "0":
        roles.append("comparator")
    return roles


# 预检单角色的超时预算（秒）。为什么单独设：全局 PM_TIMEOUT 默认 120s，
# 而预检是"廉价冒烟"——一个角色挂住就能把整轮预检拖到几分钟，
# 2026-09-13 实测端点抖动时 --preflight 直接超掉了测试里 90s 的子进程预算。
PREFLIGHT_ROLE_TIMEOUT = _float_env("PM_PREFLIGHT_TIMEOUT", 20.0)
# 预检并发度：角色之间互不依赖，串行跑纯粹是白等（8 角色 × 慢端点可达分钟级）
PREFLIGHT_CONCURRENCY = _int_env("PM_PREFLIGHT_CONCURRENCY", 8)


def _preflight_one(role: str, smoke_prompt: str, max_tokens: int) -> RoleReport:
    """单角色冒烟（线程池 worker）。异常一律转成报告行，绝不外抛。"""
    cfg = build_config(role)
    rr = RoleReport(
        role=role,
        model=cfg.model,
        base_url=cfg.base_url or "(provider 默认)",
        key_tail=_mask_key(cfg.api_key),
    )
    if not cfg.api_key:
        rr.error = "缺少 API Key"
        rr.error_kind = "auth"
        rr.advice = f"设置 PM_API_KEY 或 PM_{role.upper()}_API_KEY"
        return rr
    t0 = time.time()
    try:
        _text, meta = plain_call(
            role,
            "你是连通性探测器。只输出两个字：OK",
            smoke_prompt,
            overrides={"max_tokens": max_tokens, "timeout": PREFLIGHT_ROLE_TIMEOUT},
        )
        rr.ok = True
        rr.latency_ms = int((time.time() - t0) * 1000) or meta.get("latency_ms")
    except Exception as e:  # noqa: BLE001 - 预检就是来接住一切错误的
        rr.error = f"{type(e).__name__}: {e}"
        rr.error_kind, rr.advice = _classify_error(str(e))
        logger.info("[%s] 预检失败：%s", role, rr.error)
    return rr


def run_preflight(smoke_prompt: str = "回复：OK", max_tokens: int = 64) -> PreflightReport:
    """角色并发执行最小冒烟调用。64 token 足够确认端点活着，成本可忽略。

    走 plain_call：演示/测试的 CallHook 钩子自动生效（离线测试不碰真实端点）。
    并发必须包 carry_context —— ThreadPoolExecutor 不继承 ContextVar，
    不包就会在并发分支上绕过注入的假后端（并发路径踩过同一个坑）。
    """
    report = PreflightReport()
    roles = list(preflight_roles())
    if PREFLIGHT_CONCURRENCY > 1 and len(roles) > 1:
        with ThreadPoolExecutor(max_workers=min(PREFLIGHT_CONCURRENCY, len(roles))) as ex:
            results = list(
                ex.map(carry_context(lambda r: _preflight_one(r, smoke_prompt, max_tokens)), roles)
            )
    else:
        results = [_preflight_one(r, smoke_prompt, max_tokens) for r in roles]
    report.roles.extend(results)
    return report


def render_preflight(report: PreflightReport) -> str:
    """渲染人读报告：配置摘要表 + 冒烟结果 + 对症建议。"""
    lines: list[str] = []
    lines.append("端点预检（--preflight）")
    lines.append("")
    lines.append("| 角色 | 模型 | 端点 | Key | 冒烟 | 延迟 | 错误签名 | 建议 |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for r in report.roles:
        status = "✅" if r.ok else "❌"
        latency = f"{r.latency_ms}ms" if r.latency_ms is not None else "-"
        err_kind = r.error_kind or "-"
        advice = r.advice or "-"
        lines.append(
            f"| {r.role} | {r.model} | {r.base_url} | {r.key_tail or '(无)'} "
            f"| {status} | {latency} | {err_kind} | {advice} |"
        )
    lines.append("")
    if report.all_ok:
        lines.append("全部角色端点可达，可以开始任务。")
    else:
        failed = [r.role for r in report.roles if not r.ok]
        lines.append(f"⚠️ {len(failed)} 个角色预检失败：{', '.join(failed)}")
        lines.append("限流类失败建议等 5 分钟以上再试（实测经验）；其余按表中建议处理。")
    return "\n".join(lines)
