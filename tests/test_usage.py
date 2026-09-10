"""用量台账（可观测性）回归：按角色的 token/调用/耗时记账。

覆盖：
1. 台账单元行为：累加、快照隔离、并发 add 不丢账；
2. usage_scope：作用域隔离与嵌套归并；
3. _tokens_of：LangChain 两种 usage 字段形态都能取到，异常时记 0 不炸主流程；
4. 图级集成：fake 后端跑通后，state["llm_usage"] 存在（fake 记账 0 token 但有调用数）。
"""

from __future__ import annotations

import threading
from types import SimpleNamespace

from pm import testing
from pm.graph import build_app
from pm.llm import UsageLedger, _tokens_of, usage_scope
from pm.state import initial_state


def test_ledger_accumulates_per_role():
    led = UsageLedger()
    led.add("target", 100, 50, 1200)
    led.add("target", 80, 40, 800)
    led.add("evaluator", 200, 100, 900)
    snap = led.snapshot()
    assert snap["target"] == {
        "calls": 2,
        "input_tokens": 180,
        "output_tokens": 90,
        "latency_ms": 2000,
    }
    assert snap["evaluator"]["calls"] == 1


def test_ledger_negative_values_clamped():
    led = UsageLedger()
    led.add("target", -5, None, -1)  # type: ignore[arg-type]
    assert led.snapshot()["target"] == {
        "calls": 1,
        "input_tokens": 0,
        "output_tokens": 0,
        "latency_ms": 0,
    }


def test_ledger_concurrent_adds_do_not_lose_accounts():
    led = UsageLedger()
    threads = [
        threading.Thread(target=lambda: [led.add("target", 1, 1, 1) for _ in range(200)])
        for _ in range(8)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert led.snapshot()["target"]["calls"] == 1600


def test_snapshot_is_a_copy():
    led = UsageLedger()
    led.add("target", 1, 1, 1)
    snap = led.snapshot()
    snap["target"]["calls"] = 999
    assert led.snapshot()["target"]["calls"] == 1


def test_usage_scope_isolates_and_merges_nested():
    with usage_scope() as outer:
        outer.add("target", 10, 5, 100)
        with usage_scope() as inner:
            inner.add("evaluator", 20, 8, 50)
        # 内层已归并到外层
        assert outer.snapshot()["evaluator"]["input_tokens"] == 20
        # 内层作用域结束后，外层继续记账不受影响
        outer.add("target", 1, 1, 1)
    assert outer.snapshot()["target"]["calls"] == 2


def test_no_scope_outside_context():
    # 台账不在作用域内时 _record_usage 直接跳过（不炸、不记账）
    from pm.llm import _record_usage

    assert (
        _record_usage(
            "target", SimpleNamespace(usage_metadata={"input_tokens": 1, "output_tokens": 1}), 1
        )
        is None
    )


def test_tokens_of_various_shapes():
    # usage_metadata 形态（langchain-core ≥0.2）
    resp = SimpleNamespace(usage_metadata={"input_tokens": 12, "output_tokens": 34})
    assert _tokens_of(resp) == (12, 34)
    # response_metadata.token_usage 形态（openai 兼容层）
    resp2 = SimpleNamespace(
        response_metadata={"token_usage": {"prompt_tokens": 5, "completion_tokens": 7}}
    )
    assert _tokens_of(resp2) == (5, 7)
    # 拿不到 → (0, 0)
    assert _tokens_of(SimpleNamespace()) == (0, 0)
    # 异常对象也不炸
    assert _tokens_of(None) == (0, 0)  # type: ignore[arg-type]


def test_graph_run_records_usage_snapshot():
    with testing.fake_backend("progress"):
        app = build_app()
        final = app.invoke(
            initial_state(
                task="分析销售数据", target_model="fake", n_test_cases=1, max_iterations=1
            ),
            {"configurable": {"thread_id": "usage-test"}, "recursion_limit": 100},
        )
    # fake 后端不走 _invoke_with_rate_limit_retry，token 记 0；但 report_node 会写 llm_usage 键
    usage = final.get("llm_usage")
    assert isinstance(usage, dict)
    for _role, t in usage.items():
        assert set(t) == {"calls", "input_tokens", "output_tokens", "latency_ms"}
