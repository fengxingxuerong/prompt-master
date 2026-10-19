"""预算与传输层：墙钟预算、DNS 预解析、限流与瞬时连接错误的分类。

2026-10-19 从 `pm/llm.py` 拆出。零 LLM 依赖，只有 socket/时间。
"""

from __future__ import annotations

import ipaddress
import socket
import threading
import time
from typing import Any
from urllib.parse import urlsplit

from ._log import logger
from .config import LLMConfig, build_config


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
    # 实测的真实超时形态（openai/langchain 包一层后不带 "read timeout" 字样）
    "timeouterror",
    "timed out",
)


_TRANSIENT_STATUS_CODES = frozenset({408, 500, 502, 503, 504})


_DNS_RESOLVED: set[str] = set()


_DNS_RESOLVED_LOCK = threading.Lock()


class CallBudgetExceeded(RuntimeError):
    """一次调用的墙钟预算用尽。继承 RuntimeError：调用方现有的兜底路径不用改就能接住。

    刻意不叫 "TimeoutError"：那会被 `_TRANSIENT_CONN_HINTS` 一类文本判据再认成一次
    可重试故障，预算反而变成重试的燃料。
    """

    def __init__(self, role: str, budget_s: float, wall_s: float, requests: int, last_err: str):
        # 「用尽」只有在预算真的花完时才是事实。真实案例 A（2026-10-15，`4077d3fa3711`）
        # 打的是"墙钟预算 600s 用尽（实际 302s…）"——自相矛盾：它是在**剩余 298s 装不下
        # 一发 300s 超时**时停的，不是花完了。写成"用尽"会让人以为再等就有结果，
        # 而真正可执行的旋钮是 PM_TIMEOUT 与 PM_CALL_BUDGET 的配比。
        remaining = budget_s - wall_s
        if remaining <= 1:
            head = f"[{role}] 墙钟预算 {budget_s:.0f}s 用尽（实际 {wall_s:.0f}s，已发 {requests} 次真实请求）"
        else:
            head = (
                f"[{role}] 墙钟预算未用尽即停（预算 {budget_s:.0f}s，已用 {wall_s:.0f}s，"
                f"剩 {remaining:.0f}s 装不下一发完整超时 ⇒ 不再重试；已发 {requests} 次真实请求）"
            )
        super().__init__(f"{head}。最后一次错误：{last_err}")
        self.role = role
        self.details: dict[str, Any] = {
            "role": role,
            "budget_s": round(budget_s),
            "wall_s": round(wall_s),
            "requests": requests,
            "last_error": last_err,
        }


class DnsResolutionTimeout(CallBudgetExceeded):
    """域名解析在预算内没有返回（§三十·二十三）。

    继承 `CallBudgetExceeded` 而不是 OSError/TimeoutError：调用方现有的
    "预算耗尽直接上抛"路径不用改就能接住，也**不会**被 `_is_transient_conn_error`
    判成可重试——解析挂起时重试只会把墙钟再烧一遍，那正是要修掉的形态。
    消息里"已发 0 次真实请求"是这条错误最有诊断价值的部分：死在了端点之前。
    """


class WallBudget:
    """一次调用的墙钟预算句柄：只在**每次真实发起之前**检查。

    旧版也有 `deadline`，但它只约束"还要不要睡这一觉"（`_invoke_with_rate_limit_retry`
    在 sleep 前比一次）——一次挂住的调用完全不受它约束，所以那条线管不住卡死。
    这里两件事都管：①发起前看还剩多少，不够就停；②把这一发的客户端 timeout 夹到剩余量，
    免得最后一发冲破预算（预算外最多溢出**一发**，见 test_worst_case_is_bounded_by_the_declared_budget）。

    ⚠️ **管不到域名解析**（2026-10-07 §三十·二十三 实测）。上面那条"最多溢出一发"的上界
    隐含假设 DNS 立刻返回，而 httpx 的 timeout 只覆盖 connect/read，`socket.getaddrinfo`
    在它外面。实测：声明 `timeout=300s`、DNS 挂起 420s ⇒ 实测 **420.4s** 才返回
    （同一台机器上拒绝连接 2.08s、黑洞 300.01s、不可路由 IP 21.05s 都被兜住了）。
    `socket.setdefaulttimeout()` 同样拦不住它。
    ⇒ 修复前，一次 DNS 挂起就能让本预算形同虚设：真实跑 run_aa98d99e66b5 里
      声明 600s 预算的那发 evaluator 实际跑了 **10581s**（§三十·二十二·四）。
    这层防护由 `_invoke_with_rate_limit_retry` 入口的 **DNS 预解析**补上
    （`PM_DNS_BUDGET`，默认 5s）：解析挂起就在预算内上抛 `DnsResolutionTimeout`，
    不再跟着解析器走完。只对显式配置的 base_url 生效，默认端点不预解析
    —— 权衡见 `_preflight_dns` 的文档字符串。判据见 tests/test_dns_outside_timeout.py。
    """

    def __init__(self, role: str, seconds: float):
        self.role = role
        self.seconds = seconds
        self.start = time.monotonic()

    def remaining(self) -> float:
        return self.seconds - (time.monotonic() - self.start)

    def elapsed(self) -> float:
        return time.monotonic() - self.start

    def expired(self) -> bool:
        return self.remaining() <= 0

    def clamp(self, timeout: float) -> float:
        """这一发最多允许用掉的秒数：不超过剩余预算，也不低于 1s（0 会被 httpx 当"无限等"）。"""
        return max(1.0, min(float(timeout), self.remaining()))

    def exceeded(self, requests: int, last_err: str) -> CallBudgetExceeded:
        return CallBudgetExceeded(self.role, self.seconds, self.elapsed(), requests, last_err)


_BUDGET_MIN_REMAINING = 5.0  # 剩余不足 5s 就别再发起：那一发几乎必挂


def _is_transient_conn_error(e: Exception) -> bool:
    """连接类瞬时故障（可原地重试），与限流、与"请求本身有问题"都区分开。

    有状态码就按状态码判（5xx 与 408 可重试，其余不可），拿不到码才回退文本匹配——
    文本匹配是按"响应体里恰好有什么字样"蒙的，实测会漏掉 503 这种带 JSON 错误体的形态。
    """
    code = getattr(e, "status_code", None) or getattr(
        getattr(e, "response", None), "status_code", None
    )
    if isinstance(code, int):
        return code in _TRANSIENT_STATUS_CODES
    text = f"{type(e).__name__}: {e}".lower()
    return any(k in text for k in _TRANSIENT_CONN_HINTS)


def _endpoint_host(cfg: LLMConfig) -> tuple[str, int] | None:
    """这一发真正要连的 (host, port)；没有显式 base_url 时返回 None（权衡见 `_preflight_dns`）。"""
    if not cfg.base_url:
        return None
    parts = urlsplit(cfg.base_url)
    if not parts.hostname:
        return None
    return parts.hostname, parts.port or (443 if parts.scheme == "https" else 80)


def _resolve_into_box(box: dict[str, object], host: str, port: int) -> None:
    """子线程体：getaddrinfo 的结果或异常装进 box。解析挂住时整个线程被遗弃。"""
    try:
        box["ok"] = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
    except Exception as e:  # noqa: BLE001  # 异常要跨线程带给主线程，类型在这里无关紧要
        box["error"] = e


def resolve_host_bounded(
    role: str, host: str, port: int, budget_s: float, requests: int = 0
) -> float:
    """在 budget_s 秒内把 host:port 解析一次，返回耗时（秒）。§三十·二十三·四 方向 1 的落地。

    三种结局：
    - **预算内返回** → 记进程内正面缓存，返回耗时。真实请求的 getaddrinfo 会命中
      OS 解析缓存，不再单独挂一次；
    - **解析报错**（NXDOMAIN 等）→ 在这里**不上抛**：预解析的旗标/地址族与 SDK 的
      真实调用不一定一致，在这里失败可能误伤本来能通的端点。让真实请求去产生
      它本来会产生的错误（异常类型与重试分类都保持原样）；
    - **挂住超预算** → 上抛 `DnsResolutionTimeout`。这是本函数存在的意义：
      把「跟着解析器走完」（实测挂 1800s 就真等 1800s）变成「预算内快速失败」。

    ⚠️ 已知上限（§三十·二十三·四 方向 1 原话）：`getaddrinfo` 不可中断，挂住的
    子线程只能遗弃（daemon，随进程退出），泄漏上限 = 解析器挂住的次数。本函数
    承诺的是"调用方不再被解析拖住"，不是"把 DNS 修快"。
    IP 字面量直接跳过：getaddrinfo 对它不发网络包，不值得开线程。
    """
    # 从包命名空间取：测试用 pm.llm._DNS_RESOLVED 注入一个空集合（见 __init__.py）。
    from . import _DNS_RESOLVED

    try:
        ipaddress.ip_address(host)
        return 0.0
    except ValueError:
        pass
    key = f"{host}:{port}"
    with _DNS_RESOLVED_LOCK:
        if key in _DNS_RESOLVED:
            return 0.0
    box: dict[str, object] = {}
    started = time.monotonic()
    worker = threading.Thread(
        target=_resolve_into_box,
        args=(box, host, port),
        name=f"pm-dns-{host}",
        daemon=True,
    )
    worker.start()
    worker.join(max(budget_s, 0.1))
    elapsed = time.monotonic() - started
    if worker.is_alive():
        logger.warning(
            "[%s] DNS 预解析 %s:%d 在 %.1fs 内未返回，按预算快速失败"
            "（解析挂起不受 timeout 约束，§三十·二十三；已发 %d 次真实请求）",
            role,
            host,
            port,
            elapsed,
            requests,
        )
        raise DnsResolutionTimeout(
            role, budget_s, elapsed, requests, f"DNS 解析 {host}:{port} 挂起"
        )
    if "error" in box:
        logger.debug(
            "[%s] DNS 预解析 %s 报错但不在此上抛（真实请求会原样复现）：%s",
            role,
            host,
            box["error"],
        )
        return elapsed
    with _DNS_RESOLVED_LOCK:
        _DNS_RESOLVED.add(key)
    return elapsed


def _preflight_dns(
    role: str,
    overrides: dict[str, Any] | None,
    budget: WallBudget | None,
    counter: list[int] | None,
) -> None:
    """发起前把端点域名解析一次（`PM_DNS_BUDGET`，默认 5s，0 = 关闭）。

    **只管显式配置的 base_url**，两个原因：
    1. 事故形态就是自建端点（§三十·二十二·四 的 run_aa98d99e66b5）；
    2. 这是单测保持零出网的**构造性保证**——conftest 清空连接配置后 base_url 恒为
       None，预解析永远不会在测试里开火。默认端点（不配 base_url）的解析挂起仍会
       跟满 timeout，这是已知且接受的边界；真要兜住，把 base_url 显式写出来。
    """
    # 从包命名空间取：测试用 pm.llm.DNS_BUDGET 注入（见 __init__.py）。
    from . import DNS_BUDGET

    if DNS_BUDGET <= 0:
        return
    host_port = _endpoint_host(build_config(role, overrides))
    if host_port is None:
        return
    host, port = host_port
    dns_s = DNS_BUDGET if budget is None else min(DNS_BUDGET, budget.remaining())
    if dns_s <= 0:
        return  # 预算已尽：让发起前的预算检查去抛它的错，这里不抢戏
    resolve_host_bounded(role, host, port, dns_s, counter[0] if counter else 0)
