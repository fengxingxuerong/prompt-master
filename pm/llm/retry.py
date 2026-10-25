"""重试层：限流退避、瞬时连接重试、墙钟预算夹取。

2026-10-19 从 `pm/llm.py` 拆出。**注意延迟导入**：本仓测试一律补 `pm.llm.get_llm` /
`pm.llm._llm_within_budget` / `pm.llm.RATE_LIMIT_*`，而这里若在模块顶层 `from .config import get_llm`
会把名字焊死在模块命名空间里，包上的补丁就静默失效（测试变绿、改的是另一份函数）。
所以这些名字在**调用点**用 `from . import X` 取，走包属性查找。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from . import config as _config
from ._log import logger
from .budget import (
    _BUDGET_MIN_REMAINING,
    WallBudget,
    _is_rate_limit_error,
    _is_transient_conn_error,
    _preflight_dns,
)
from .config import build_config
from .ledger import _record_failure, _record_retry_if, _record_usage


def _invoke_with_conn_retry(
    role: str,
    call: Any,
    counter: list[int] | None = None,
    budget: WallBudget | None = None,
    attempt_s: float = 0.0,
) -> Any:
    """执行一次调用；瞬时连接故障按 1.5s/3s 短退避重试（默认 2 次），其余异常直接上抛。

    `counter`（单元素 list，可选）：每次**真实发起网络请求**（含连接重试）就 +1。
    放在最内层是为了让计数覆盖所有实际打到端点的调用——外层 429 重试/Key 轮换
    每次都会重新走进这里，连接故障的内部重试也在这里发生，语义最准确。

    `budget`（可选）：整次调用的墙钟预算。旧版这里**没有任何时间约束**，
    一次"端点挂住到 timeout"会连着重试到 `PM_CONN_RETRIES` 用完，最坏 (1+N)×timeout；
    现在每发之前查预算，退避睡一觉之前也查，不够就带着现场（发了几发、用了多久）上抛。

    `attempt_s`：这一发的客户端 timeout（由 `_llm_within_budget` 夹过）。第一发照例放行
    （它刚被夹进剩余预算里），**后续重试要求剩余预算装得下完整一发**——否则会出现
    "剩 50s 却发出一个 100s timeout 的请求"这种注定冲破预算的子弹。
    """
    # 从包命名空间取：测试用 pm.llm.TRANSIENT_CONN_* 注入（见模块 docstring）。
    from . import TRANSIENT_CONN_BASE_SLEEP, TRANSIENT_CONN_RETRIES

    last_exc: Exception | None = None
    for i in range(TRANSIENT_CONN_RETRIES + 1):
        # 首发只要求"还剩预算"（timeout 已被 `_llm_within_budget` 夹进剩余量）；
        # 后续重试额外要求装得下一整个 timeout——否则会出现"剩 50s 却发出 100s 的请求"。
        floor = 0.0 if i == 0 else max(_BUDGET_MIN_REMAINING, attempt_s)
        if budget is not None and budget.remaining() <= floor:
            raise budget.exceeded(
                counter[0] if counter else i, str(last_exc or f"剩余预算 {budget.remaining():.0f}s")
            )
        try:
            if counter is not None:
                counter[0] += 1
            # 除首发外的每一条都是**额外打到端点**的请求（2026-10-31 接通记账）。
            # 不记的话，报告里的「重试请求数」在每一轮真跑里恒为 0 —— 而
            # 连接故障重试恰恰是"看起来正常、实际多发了几发"的那一类。
            _record_retry_if(role, i > 0)
            return call()
        except Exception as e:
            last_exc = e
            if not _is_transient_conn_error(e) or i >= TRANSIENT_CONN_RETRIES:
                raise
            sleep_s = TRANSIENT_CONN_BASE_SLEEP * (2**i)
            if budget is not None and budget.remaining() - sleep_s < _BUDGET_MIN_REMAINING:
                raise budget.exceeded(
                    counter[0] if counter else i, f"{type(e).__name__}: {e}"
                ) from e
            logger.warning(
                "[%s] 瞬时连接故障（%s），%.1fs 后重试 %d/%d（墙钟已用 %.0fs）",
                role,
                type(e).__name__,
                sleep_s,
                i + 1,
                TRANSIENT_CONN_RETRIES,
                budget.elapsed() if budget else 0.0,
            )
            time.sleep(sleep_s)
    raise AssertionError("unreachable")  # pragma: no cover


def _llm_within_budget(
    role: str, overrides: dict[str, Any] | None, budget: WallBudget | None
) -> tuple[Any, float]:
    """构造这一发要用的客户端，返回 `(llm, 这发的秒数预算)`。

    有墙钟预算时把 timeout 夹进剩余量——不夹的话：预算剩 6s 而 `PM_TIMEOUT=300`，
    这一发仍可挂满 300s，预算就只是"发与发之间"的软约束。
    返回夹出来的秒数给连接重试层，让它在"剩余预算连一发都装不下"时直接收手。
    """
    # 从**包命名空间**取：测试补的是 `pm.llm.get_llm`（见模块 docstring 的说明）。
    from . import get_llm

    if budget is None:
        return get_llm(role, overrides), 0.0
    base = build_config(role, overrides).timeout
    clamped = int(budget.clamp(base))
    merged = {**(overrides or {}), "timeout": clamped}
    return get_llm(role, merged), float(clamped)


def _invoke_with_rate_limit_retry(
    role: str,
    invoke_fn: Callable[[Any], Any],  # 接收 llm 实例执行一次调用
    overrides: dict[str, Any] | None = None,
    deadline: float | None = None,
    counter: list[int] | None = None,
    budget: WallBudget | None = None,
) -> Any:
    """带 429 退避与 Key 轮换的调用封装。

    每次撞到限流：先退避等待，再轮换到 Key 池中的下一个 Key 重建客户端重试；
    Key 池只有 1 个 Key 时退避时间加倍，给上游配额留恢复窗口。
    非限流异常直接上抛，不吞错。

    退避总时长受 `PM_RATE_LIMIT_MAX_WAIT` 约束（A3）：预算用尽就不再死等，直接上抛最后错误。
    连接类瞬时故障单独处理：原地短退避重试（`PM_CONN_RETRIES` / `PM_CONN_BASE_SLEEP`）。

    `deadline`（monotonic 绝对时刻）是可选的外层共享预算：结构化调用在通道 A/B 之间
    共享一个总限流预算（③ 解耦），取两者更早者生效；不传时行为与旧版完全一致。

    `budget`（`WallBudget`）是**墙钟**预算，与 `deadline`（只管退避睡多久）不是一回事：
    - 每发之前查剩余量，不够 `_BUDGET_MIN_REMAINING` 就不再发起；
    - 把这一发的客户端 timeout 夹到剩余量内，避免最后一发冲破预算。
    没有这一层时 `deadline` 挡不住"挂住不返回"的调用（实测就是这么挂了 900s）。

    `counter`（单元素 list）可选：每发起一次**真实网络请求**就 +1（含 429 退避重试、
    Key 轮换后的重试与连接类内部重试），供调用方在 meta 里如实记录请求次数。
    计数实际发生在最内层 `_invoke_with_conn_retry`（所有重试路径最终都会重新走进它），
    这里只是透传。默认 None 行为不变。
    """
    # 从**包命名空间**取这五个名字：测试用 `monkeypatch.setattr("pm.llm.RATE_LIMIT_RETRIES", …)`
    # 或 `pm.llm._llm_within_budget` 注入，顶层 import 会让补丁静默失效（见模块 docstring）。
    from . import (
        RATE_LIMIT_BASE_SLEEP,
        RATE_LIMIT_MAX_WAIT,
        RATE_LIMIT_RETRIES,
        _key_pool,
        _llm_within_budget,
    )

    # DNS 预解析（§三十·二十三）：httpx 的 timeout 管不到 getaddrinfo，一次解析挂起
    # 就能让下面所有预算检查形同虚设（实测 600s 预算实跑 10581s）。在进入重试循环
    # 之前先兜住：挂住就在预算内快速失败，不再跟着解析器走完。
    _preflight_dns(role, overrides, budget, counter)
    last_exc: Exception | None = None
    wait_deadline = time.monotonic() + max(0.0, RATE_LIMIT_MAX_WAIT)
    if deadline is not None:
        wait_deadline = min(wait_deadline, deadline)
    for attempt in range(RATE_LIMIT_RETRIES + 1):
        # 首发只要求还剩预算（timeout 会在 `_llm_within_budget` 里夹进剩余量）；
        # 退避后的重试还要多留一点，免得"睡完觉只剩 1s"再发一发注定失败的请求。
        floor = 0.0 if attempt == 0 else _BUDGET_MIN_REMAINING
        if budget is not None and budget.remaining() <= floor:
            raise budget.exceeded(counter[0] if counter else attempt, str(last_exc or "未发起请求"))
        if attempt > 0:
            sleep_s = RATE_LIMIT_BASE_SLEEP * (2 ** (attempt - 1))
            pool = _key_pool(role)
            if len(pool) <= 1:
                sleep_s *= 2
            if time.monotonic() + sleep_s > wait_deadline:
                logger.warning(
                    "[%s] 限流退避预算已用完（还剩 %.0fs，本次还需等 %.0fs），停止重试并上抛最后错误",
                    role,
                    max(0.0, wait_deadline - time.monotonic()),
                    sleep_s,
                )
                break
            if budget is not None and budget.remaining() - sleep_s < _BUDGET_MIN_REMAINING:
                raise budget.exceeded(counter[0] if counter else attempt, str(last_exc))
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
                # 游标属于 config（与 _next_key 共用一份），拆包后不能各持一个副本
                with _config._KEY_LOCK:
                    api_key = pool[_config._KEY_CURSOR % len(pool)]
                    _config._KEY_CURSOR += 1
                logger.info(
                    "[%s] 轮换到 Key 池第 %d 个 Key 重试", role, _config._KEY_CURSOR % len(pool)
                )
            llm, attempt_s = _llm_within_budget(
                role, {"api_key": api_key} if api_key else None, budget
            )
        else:
            llm, attempt_s = _llm_within_budget(role, overrides, budget)
        try:
            t0 = time.time()
            # 限流退避后的这一发同样是额外请求（首发不记，重试记）。
            # 连接重试在更内层各自记一次，两层**不互相覆盖**——它们各自都是真实请求。
            _record_retry_if(role, attempt > 0)
            # 默认参数绑定当前轮的 llm，避免闭包捕获循环变量（B023）
            resp = _invoke_with_conn_retry(
                role, lambda _llm=llm: invoke_fn(_llm), counter, budget, attempt_s
            )
            # 记账只记真实成功的调用（429 退避后的重试、双通道各自的真实请求都覆盖；
            # 演示/测试钩子在 structured_call/plain_call 入口就返回，不会到这里）
            _record_usage(role, resp, int((time.time() - t0) * 1000))
            return resp
        except Exception as e:
            # §三十·十八：失败这一次也要留痕。以前完全不可见 ⇒ 台账上
            # "慢"和"失败"长得一样，429/超时只能翻 errors 才知道。
            # 单独记（`add_failure`），**不并进 calls** —— 并进去会让按成功调用
            # 算的速率判据失真。
            _record_failure(role, int((time.time() - t0) * 1000))
            if _is_rate_limit_error(e):
                last_exc = e
                continue
            raise
    raise last_exc  # type: ignore[misc]
