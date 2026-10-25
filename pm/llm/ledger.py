"""用量账本：ContextVar 隔离 + 每次调用的 token/延迟记账（2026-10-19 从 `pm/llm.py` 拆出）。"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any


@dataclass
class UsageLedger:
    """按角色汇总的用量台账。totals[role] = {calls, input_tokens, output_tokens, latency_ms}。

    target 并发采样走线程池：carry_context 会把 ContextVar 快照（含本对象）带进
    工作线程，多个线程会同时 add() —— 计数必须加锁，否则并发下丢账。

    §三十·十八 起追加 `failed_calls` / `failed_latency_ms` / `retry_requests`：
    **失败与重试原来完全不可见**（`_record_usage` 只在成功路径调用），
    于是"慢"和"失败"在台账上长得一模一样——429/超时只能翻 `errors` 才知道。
    这三个字段是**纯增量**：不动 `calls`/`input_tokens`/`output_tokens`/`latency_ms`
    的语义，因此 §三十·十六/·十七 那批按成功调用算的读数与守卫**依然有效**。
    """

    totals: dict[str, dict[str, int]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # 失败/重试的独立计数。不放进 totals，避免污染既有口径（`calls` 只数成功）。
    failures: dict[str, int] = field(default_factory=dict)
    failed_latency_ms: dict[str, int] = field(default_factory=dict)
    retry_requests: dict[str, int] = field(default_factory=dict)

    def add(self, role: str, input_tokens: int, output_tokens: int, latency_ms: int) -> None:
        with self._lock:
            t = self.totals.setdefault(
                role, {"calls": 0, "input_tokens": 0, "output_tokens": 0, "latency_ms": 0}
            )
            t["calls"] += 1
            t["input_tokens"] += max(0, int(input_tokens or 0))
            t["output_tokens"] += max(0, int(output_tokens or 0))
            t["latency_ms"] += max(0, int(latency_ms or 0))

    def add_failure(self, role: str, latency_ms: int) -> None:
        """记一次**没能拿到结果**的调用（超时、限流耗尽、解析重试耗尽…）。

        为什么不并进 `calls`：并进去就等于说"这次调用产出了 token"，
        而 §三十·十六/·十七 的速率判据（秒/千输出token）是按**成功**调用算的，
        混进失败会把分母弄脏、让那些守卫立刻失真。分开记才两边都准。
        """
        with self._lock:
            self.failures[role] = self.failures.get(role, 0) + 1
            self.failed_latency_ms[role] = self.failed_latency_ms.get(role, 0) + max(
                0, int(latency_ms or 0)
            )

    def add_retry_request(self, role: str, n: int = 1) -> None:
        """记重试**实际打到端点的请求数**（429 退避重试、Key 轮换重试、连接重试）。"""
        with self._lock:
            self.retry_requests[role] = self.retry_requests.get(role, 0) + max(0, int(n or 0))

    def snapshot(self) -> dict[str, dict[str, int]]:
        with self._lock:
            out = {role: dict(t) for role, t in self.totals.items()}
        for role, n in self.failures.items():
            out.setdefault(
                role, {"calls": 0, "input_tokens": 0, "output_tokens": 0, "latency_ms": 0}
            )
            out[role]["failed_calls"] = n
            out[role]["failed_latency_ms"] = self.failed_latency_ms.get(role, 0)
        for role, n in self.retry_requests.items():
            out.setdefault(
                role, {"calls": 0, "input_tokens": 0, "output_tokens": 0, "latency_ms": 0}
            )
            out[role]["retry_requests"] = n
        return out


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
            with outer._lock:
                for role, t in ledger.totals.items():
                    ot = outer.totals.setdefault(
                        role, {"calls": 0, "input_tokens": 0, "output_tokens": 0, "latency_ms": 0}
                    )
                    for k in ot:
                        ot[k] += t.get(k, 0)
                # 失败/重试也要并回（否则嵌套作用域里的失败在父账上消失）
                for attr in ("failures", "failed_latency_ms", "retry_requests"):
                    mine = getattr(ledger, attr)
                    theirs = getattr(outer, attr)
                    for role, n in mine.items():
                        theirs[role] = theirs.get(role, 0) + n


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


def _record_failure(role: str, latency_ms: int) -> None:
    """失败调用记一笔（§三十·十八）；台账不在作用域内则跳过。

    单列一个函数是为了让**所有**失败出口都能调用它，而不必各自去摸
    `current_ledger()` 并重复那段 max(0, int(...)) 兜底。
    """
    ledger = current_ledger()
    if ledger is None:
        return
    ledger.add_failure(role, latency_ms)


def _record_retry(role: str, n: int = 1) -> None:
    """记一次**额外打到端点**的重试请求（2026-10-31 接通）。

    为什么这个函数现在才出现：`UsageLedger.add_retry_request` 自 §三十·十八 起就存在，
    但**全仓没有任何生产调用点** —— 于是报告里那列「重试请求数」在每一轮真跑里恒为 0。
    那比"没有这一列"更坏：读者会把 0 读成"这一轮没重试过"，而实际上 429 退避、
    连接重试、解析重试都在发生，只是没人记账。

    计数口径：**同一次逻辑调用里，除首发以外的每一条真实 HTTP 请求**。
    各重试点分别登记（连接重试、限流退避、通道降级、解析重试），
    所以一次"通道 A 失败 → 通道 B 解析重试 2 次"会记 3 条，
    而成功那次本身不记 —— 它与上表的 `calls` 一一对应，不该重复。
    """
    ledger = current_ledger()
    if ledger is None:
        return
    ledger.add_retry_request(role, n)


def _record_retry_if(role: str, cond: bool) -> None:
    """条件成立才记一条重试（把 `if` 挪进这里，调用点的圈复杂度不涨）。

    为什么要这个小壳：复杂度棘轮对 `structured_call`（CC 24）与
    `_invoke_with_rate_limit_retry`（CC 11→12）是逐函数预算，
    "多一个记账分支"这种纯记账的事不该挤占它们的预算——分支本身没有语义。
    """
    if cond:
        _record_retry(role)
