"""成本闸的两种计量单位：成功调用（老）与真实请求（新）。

实测缺陷（2026-10-31 审计）：`PM_MAX_LLM_CALLS` 读 `llm_calls`，而那是**成功调用**数。
429 退避、超时重试、通道降级都会让真实请求跑到成功数的数倍 —— 预算"还剩很多"的同时
账单已经翻倍。

修法刻意**不动**老开关的语义：`llm_calls` 是报告头那个被多处引用的数，
改它的计量会让历史读数与历史预算配置一起失去可比性。新增
`PM_MAX_LLM_REQUESTS`（成功 + 失败 = 真实请求），两个都设时取更严格的那个。
"""

from __future__ import annotations

import pytest
from pm.llm import usage_scope
from pm.nodes.judge import _budget_stop, _requests_used


# ------------------------------------------------------------- 请求数的口径
def test_requests_count_success_plus_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    """请求数 = 成功 + 失败；`retry_requests` 不参与（失败已计在 failed_calls 里，相加会重复）。"""
    monkeypatch.delenv("PM_MAX_LLM_CALLS", raising=False)
    with usage_scope() as led:
        led.add("target", 10, 20, 100)  # 1 次成功
        led.add_failure("target", 50)  # 1 次失败
        led.add_failure("target", 50)  # 又一次失败
        led.add_retry_request("target", 1)  # 额外请求的计数，不参与求和
        assert _requests_used({"llm_calls": 0}, 0) == 3


def test_requests_never_below_successful_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """台账不可用（单测直调节点）时退化成成功调用数，而不是报 0 —— 报 0 会让闸静默失效。"""
    monkeypatch.delenv("PM_MAX_LLM_CALLS", raising=False)
    assert _requests_used({"llm_calls": 7}, 2) == 9


# --------------------------------------------------------------- 闸的取值
def test_old_switch_still_counts_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PM_MAX_LLM_REQUESTS", raising=False)
    monkeypatch.setenv("PM_MAX_LLM_CALLS", "10")
    # 调用 8 + 预计 3 > 10 ⇒ 触发；请求数再大也不关它的事
    hit = _budget_stop(calls_used=8, requests_used=50, per_iter_est=3)
    assert hit is not None and "PM_MAX_LLM_CALLS" in hit and "调用" in hit


def test_new_switch_counts_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PM_MAX_LLM_CALLS", raising=False)
    monkeypatch.setenv("PM_MAX_LLM_REQUESTS", "10")
    # 真实请求 50 已经远超；成功调用只有 8
    hit = _budget_stop(calls_used=8, requests_used=50, per_iter_est=3)
    assert hit is not None and "PM_MAX_LLM_REQUESTS" in hit and "请求" in hit


def test_both_switches_take_the_stricter_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PM_MAX_LLM_CALLS", "100")
    monkeypatch.setenv("PM_MAX_LLM_REQUESTS", "10")
    hit = _budget_stop(calls_used=8, requests_used=50, per_iter_est=3)
    assert hit is not None
    assert "PM_MAX_LLM_REQUESTS" in hit
    assert "PM_MAX_LLM_CALLS" not in hit, "只该报那条真的触发了的闸"


def test_neither_switch_means_no_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in ("PM_MAX_LLM_CALLS", "PM_MAX_LLM_REQUESTS"):
        monkeypatch.delenv(k, raising=False)
    assert _budget_stop(calls_used=10_000, requests_used=10_000, per_iter_est=999) is None


def test_bad_value_is_ignored_not_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    """非法值告警并忽略该闸（与全仓 env 解析的一贯立场一致），不许把评估链带崩。"""
    monkeypatch.delenv("PM_MAX_LLM_CALLS", raising=False)
    monkeypatch.setenv("PM_MAX_LLM_REQUESTS", "ten")
    assert _budget_stop(calls_used=999, requests_used=999, per_iter_est=999) is None


def test_zero_budget_stops_immediately(monkeypatch: pytest.MonkeyPatch) -> None:
    """`=0` 是"一次都不许再发"，不是"关掉闸"。"""
    monkeypatch.delenv("PM_MAX_LLM_REQUESTS", raising=False)
    monkeypatch.setenv("PM_MAX_LLM_CALLS", "0")
    assert _budget_stop(calls_used=0, requests_used=0, per_iter_est=1) is not None
