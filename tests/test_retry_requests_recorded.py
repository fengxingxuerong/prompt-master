"""「重试请求数」必须真的被记上，而不是永远印 0。

实测缺陷（2026-10-31 审计）：`UsageLedger.add_retry_request` 自 §三十·十八 引入，
但**全仓没有任何生产调用点** —— 于是报告里那列「重试请求数」在每一轮真跑里恒为 0。
那比"没有这一列"更坏：读者会把 0 读成"这一轮没重试过"，而 429 退避、
连接重试、通道降级、解析重试全都在发生，只是没人记账。

口径：**同一次逻辑调用里，除首发以外的每一条真实 HTTP 请求**。
成功那发不记 —— 它与用量表的 `calls` 一一对应，重复记会让两个数都读不懂。
"""

from __future__ import annotations

from typing import Any

import pytest
from pm.llm import _invoke_with_rate_limit_retry, current_ledger, usage_scope


class _RateLimited(Exception):
    """带 status_code=429 的假异常（判据走 `_is_rate_limit_error` 的第一条分支）。"""

    status_code = 429


def test_rate_limit_retries_are_counted(monkeypatch: pytest.MonkeyPatch) -> None:
    """两发 429、第三发成功 ⇒ 台账上必须记 2 条重试请求。"""
    from pm import llm as L

    # 常量都从**包命名空间**取（见 retry.py 模块 docstring 的说明），所以补在包上
    monkeypatch.setattr(L, "RATE_LIMIT_BASE_SLEEP", 0.0)
    monkeypatch.setattr(L, "RATE_LIMIT_RETRIES", 5)
    monkeypatch.setattr(L, "RATE_LIMIT_MAX_WAIT", 10.0)
    monkeypatch.setattr(L, "_llm_within_budget", lambda *_a, **_k: (object(), 0.0))

    calls = {"n": 0}

    def invoke(_llm: Any) -> str:
        calls["n"] += 1
        if calls["n"] <= 2:
            raise _RateLimited("429 too many requests")
        return "ok"

    with usage_scope() as led:
        assert _invoke_with_rate_limit_retry("evaluator", invoke) == "ok"
        snap = led.snapshot()
    assert calls["n"] == 3, "前置：应当发了 3 发"
    assert snap["evaluator"]["retry_requests"] == 2, (
        f"两次重发没进台账：{snap['evaluator'].get('retry_requests')}"
    )
    # 成功那发按 `calls` 记，且不重复进 retry_requests
    assert snap["evaluator"]["calls"] == 1


def test_failure_is_recorded_on_each_failed_attempt() -> None:
    """失败的每一发也要进 `failed_calls`（与重试是两个正交的计数）。"""
    from pm.llm import _record_failure, _record_retry

    with usage_scope() as led:
        _record_retry("reviser")
        _record_failure("reviser", 1234)
        snap = led.snapshot()
    assert snap["reviser"]["retry_requests"] == 1
    assert snap["reviser"]["failed_calls"] == 1
    assert snap["reviser"]["failed_latency_ms"] == 1234
    assert snap["reviser"]["calls"] == 0, "失败不许并进 calls（速率判据按成功算）"


def test_no_ledger_in_scope_is_a_silent_noop() -> None:
    """单测直调（没有 usage_scope）时记账必须是空操作，绝不因为记账炸掉主流程。"""
    from pm.llm import _record_retry

    assert current_ledger() is None
    _record_retry("target")  # 不抛即通过


def test_nested_scope_merges_retry_counts() -> None:
    """嵌套作用域里的重试要并回父账 —— 否则子作用域（并发分支）的重试在父账上消失。"""
    from pm.llm import _record_retry

    with usage_scope() as outer:
        _record_retry("judge")
        with usage_scope() as inner:
            _record_retry("judge")
            _record_retry("judge")
            assert inner.snapshot()["judge"]["retry_requests"] == 2
        assert outer.snapshot()["judge"]["retry_requests"] == 3
