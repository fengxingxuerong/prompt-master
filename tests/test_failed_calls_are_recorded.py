"""失败与重试必须在台账上可见（§三十·十八）。

§三十·十三·三 记的缺口：`_record_usage` 只在成功路径调用，于是
**失败的调用在 `llm_usage` 里完全不存在**。后果不是"少了个字段"，
而是**两种完全不同的故障在台账上长得一模一样**：

  · 真的慢（成功，但每次 140s）
  · 一直失败（429/超时，然后返回兜底分）

排障时只能去翻 `errors` 数组猜——本会话已经因此查错了三轮
（查错了本账、误判成"偶发长尾"、误判成"端点排队"）。

这一节把失败单独记进台账，并**刻意不并进 `calls`**：
§三十·十六/·十七 的速率判据（秒/千输出token）是按**成功**调用算的，
混进失败会让那些守卫立刻失真。

⚠️ 判据设计记录：第一版用 `backend.use(structured=...)` 注入失败钩子，
   结果**测不到**——钩子在 `structured_call` 入口就返回了，走不到
   重试循环里加记账的那段。现版直接调 `_invoke_with_rate_limit_retry`
   （记账就加在那里），走**真路径**。读代码前别猜 API。
"""

from __future__ import annotations

from typing import Any, ClassVar

import pytest
from pm.llm import (
    UsageLedger,
    _invoke_with_rate_limit_retry,
    usage_scope,
)


def _rate_limit() -> Exception:
    """端点侧限流形态。`_is_rate_limit_error` 要能认出它，否则测试没意义。"""
    from pm.llm import _is_rate_limit_error

    for cand in (
        RuntimeError("Error code: 429 - insufficient_quota"),
        RuntimeError("rate_limit_error"),
        RuntimeError("OpenAIError: inference exceeds tpm/rpm limit"),
    ):
        if _is_rate_limit_error(cand):
            return cand
    pytest.fail("连构造限流异常都做不到，判据失效")


def _fast_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    """把重试压到最快且不真睡——否则单测要真的等退避。"""
    monkeypatch.setattr("pm.llm.RATE_LIMIT_BASE_SLEEP", 0.0)
    monkeypatch.setattr("pm.llm.get_llm", lambda role, overrides=None: object())
    monkeypatch.setattr(
        "pm.llm._llm_within_budget", lambda role, overrides, budget: (object(), 0.0)
    )


# ---------------------------------------------------------------- 记账本身


def test_failure_is_counted_separately_from_success() -> None:
    """失败一次 → failed_calls=1，而 calls 仍是 0。"""
    with usage_scope() as led:
        led.add_failure("evaluator", 1234)
        snap = led.snapshot()

    assert snap["evaluator"]["calls"] == 0, "失败不许混进 calls"
    assert snap["evaluator"]["failed_calls"] == 1
    assert snap["evaluator"]["failed_latency_ms"] == 1234


def test_snapshot_keeps_success_shape_unchanged() -> None:
    """只有成功调用时，snapshot 的形状与旧版逐字一致。

    这是兼容性判据：§三十·十六/·十七 的守卫和所有历史读数都按这四个字段算，
    失败记账**绝不能**改它们。
    """
    led = UsageLedger()
    led.add("evaluator", 100, 200, 500)
    assert led.snapshot() == {
        "evaluator": {"calls": 1, "input_tokens": 100, "output_tokens": 200, "latency_ms": 500}
    }


def test_retry_requests_are_counted() -> None:
    led = UsageLedger()
    led.add_retry_request("evaluator", 2)
    assert led.snapshot()["evaluator"]["retry_requests"] == 2


def test_nested_scope_merges_failures_upward() -> None:
    """嵌套作用域（并发采样那条路径）里的失败必须并回父账。

    不并的话，失败会在嵌套那一层消失——那等于白记。
    """
    with usage_scope() as outer:
        with usage_scope() as inner:
            inner.add_failure("target", 500)
            inner.add_retry_request("target", 1)
        outer.add("target", 10, 20, 30)
        snap = outer.snapshot()

    assert snap["target"]["failed_calls"] == 1, "嵌套里的失败丢了"
    assert snap["target"]["retry_requests"] == 1
    assert snap["target"]["calls"] == 1, "成功计数也丢了"


def test_negative_and_none_latency_are_clamped() -> None:
    """脏输入不许把台账写成负数。"""
    led = UsageLedger()
    led.add_failure("evaluator", -5)
    led.add_failure("evaluator", None)  # type: ignore[arg-type]
    assert led.snapshot()["evaluator"]["failed_latency_ms"] == 0


# ------------------------------------------------- 真实失败路径真的会记账


def test_rate_limit_exhaustion_is_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    """429 重试耗尽 → 台账上出现 failed_calls（真路径）。

    这是 §三十·十四 真实跑到的那一类（`run_71ee15abd123` 的两次 429）。
    """
    _fast_retry(monkeypatch)
    monkeypatch.setattr("pm.llm.RATE_LIMIT_RETRIES", 1)
    err = _rate_limit()

    def _boom(_llm: Any) -> Any:
        raise err

    with usage_scope() as led:
        with pytest.raises(Exception):  # noqa: B017 - 形态随重试策略变
            _invoke_with_rate_limit_retry("evaluator", _boom)
        snap = led.snapshot()

    got = snap.get("evaluator") or {}
    assert got.get("failed_calls", 0) >= 1, f"429 重试耗尽却没记账：{snap}"
    assert got.get("calls", 0) == 0, "失败不许混进 calls"


def test_non_rate_limit_failure_is_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    """非限流异常（连接错误/超时）同样要记账。"""
    _fast_retry(monkeypatch)

    def _boom(_llm: Any) -> Any:
        raise TimeoutError("Request timed out")

    with usage_scope() as led:
        with pytest.raises(TimeoutError):
            _invoke_with_rate_limit_retry("evaluator", _boom)
        snap = led.snapshot()

    assert (snap.get("evaluator") or {}).get("failed_calls", 0) >= 1, f"超时却没记账：{snap}"


def test_success_after_retry_is_still_a_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """429 之后重试成功 → 成功照记，失败也留痕，但 calls 只有 1。

    这条防的是"为了让失败可见，把重试成功也算成两次调用"。
    """
    _fast_retry(monkeypatch)
    monkeypatch.setattr("pm.llm.RATE_LIMIT_RETRIES", 2)

    state = {"n": 0}
    err = _rate_limit()

    class _Resp:
        usage_metadata: ClassVar[dict[str, int]] = {"input_tokens": 7, "output_tokens": 11}
        response_metadata: ClassVar[dict[str, Any]] = {}

    def _flaky(_llm: Any) -> Any:
        state["n"] += 1
        if state["n"] == 1:
            raise err
        return _Resp()

    with usage_scope() as led:
        out = _invoke_with_rate_limit_retry("evaluator", _flaky)
        snap = led.snapshot()

    assert state["n"] == 2, "没真的走到重试"
    assert out is not None
    got = snap.get("evaluator") or {}
    assert got.get("calls") == 1, f"成功只该记一次：{got}"
    assert got.get("input_tokens") == 7
    assert got.get("failed_calls", 0) >= 1, "第一次失败没留痕"
