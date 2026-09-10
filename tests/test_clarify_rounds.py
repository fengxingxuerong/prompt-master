"""M8 回归：「最多 2 轮澄清提问」的语义对齐。

旧实现用 clarify_round（每次 clarify 分析都 +1，含带答案重分析）当提问上限：
一轮交互 = clarify + ask_user + clarify = 3 次分析，所以 `clarify_round < 2`
实际最多只能问 1 轮 —— 与 README/graph.py 承诺的 2 轮不符（QA 报告 M8）。
修复后引入独立的 clarify_questions_asked（只在 ask_user 节点 +1）。

覆盖：
1. 路由单测：提问轮数 0/1 时允许再问，2 及以上拒绝；
2. 集成测试：fake clarifier 连续两轮判不清晰 → 应产生两次 interrupt，
   用户两次回答后流程继续；第三轮仍不清晰时不再提问，带推断上下文直接优化。
"""

from __future__ import annotations

from typing import Any

from langgraph.types import Command
from pm import testing
from pm.graph import build_app, route_after_clarify
from pm.schemas import ClarificationResult, InferredContext, MockInputSet
from pm.state import initial_state
from pm.testing import _fake_structured


# --------------------------------------------------------------------------
# 路由单测
# --------------------------------------------------------------------------
def _state_for_route(**over: Any) -> dict[str, Any]:
    base = initial_state(task="写个 prompt", target_model="fake", auto_clarify=False)
    base.update(
        {
            "clarification": {
                "is_clear": False,
                "task_summary": "模糊需求",
                "inferred_context": {},
                "clarifying_questions": ["给谁看？"],
                "suggested_next_step": "ask_user",
            },
        }
    )
    base.update(over)
    return base


def test_route_allows_first_two_question_rounds():
    assert route_after_clarify(_state_for_route(clarify_questions_asked=0)) == "ask_user"
    assert route_after_clarify(_state_for_route(clarify_questions_asked=1)) == "ask_user"


def test_route_blocks_third_question_round():
    assert route_after_clarify(_state_for_route(clarify_questions_asked=2)) == "optimize"


def test_route_counts_question_rounds_not_clarify_runs():
    """关键回归：clarify_round 被重分析撑大时，只要提问轮数没用完就仍可提问。

    旧实现 `clarify_round < 2` 在这里会错误地直接放行去 optimize（M8 的缩水根源）。
    """
    s = _state_for_route(clarify_round=3, clarify_questions_asked=1)
    assert route_after_clarify(s) == "ask_user"


# --------------------------------------------------------------------------
# 集成：两轮 interrupt
# --------------------------------------------------------------------------
def _unclear_twice_structured(role, model_cls, system, user, max_retries=3, overrides=None):
    """前两次 clarify 都判不清晰，之后判清晰；其余模型类复用 fake 后端。"""
    if model_cls is ClarificationResult:
        n = _unclear_twice_structured.calls  # type: ignore[attr-defined]
        _unclear_twice_structured.calls = n + 1  # type: ignore[attr-defined]
        if n < 2:
            return ClarificationResult(
                is_clear=False,
                task_summary=f"（fake）第 {n + 1} 次分析仍不清晰",
                inferred_context=InferredContext(
                    target_audience="未知",
                    domain="未知",
                    output_format="未知",
                    tone="未知",
                    constraints=["需求描述缺少关键信息"],
                ),
                clarifying_questions=[f"第 {n + 1} 轮：请说明受众与格式？"],
                suggested_next_step="ask_user",
            ), {
                "role": role,
                "model": "fake-model",
                "channel": "fake",
                "attempts": 1,
                "latency_ms": 1,
            }
        return ClarificationResult(
            is_clear=True,
            task_summary="（fake）两轮回答后已清晰",
            inferred_context=InferredContext(
                target_audience="业务分析师",
                domain="商业分析",
                output_format="Markdown",
                tone="专业",
                constraints=[],
            ),
            clarifying_questions=[],
            suggested_next_step="proceed_to_optimizer",
        ), {"role": role, "model": "fake-model", "channel": "fake", "attempts": 1, "latency_ms": 1}
    if model_cls is MockInputSet:
        return _fake_structured(role, model_cls, system, user, max_retries, overrides)
    # EvaluationResult 等直接走默认 fake 实现
    return _fake_structured(role, model_cls, system, user, max_retries, overrides)


_unclear_twice_structured.calls = 0  # type: ignore[attr-defined]


def _interrupts(snap: Any) -> list[Any]:
    out: list[Any] = []
    for task_ in getattr(snap, "tasks", []) or []:
        out.extend(getattr(task_, "interrupts", []) or [])
    return out


def test_two_question_rounds_are_allowed():
    _unclear_twice_structured.calls = 0  # type: ignore[attr-defined]
    with testing.scope("progress", structured=_unclear_twice_structured):
        app = build_app()
        config = {"configurable": {"thread_id": "m8-two-rounds"}, "recursion_limit": 200}
        init = initial_state(task="写个 prompt", target_model="fake", auto_clarify=False)

        app.invoke(init, config)
        snaps = [_interrupts(app.get_state(config))]
        assert snaps[0], "第一轮应产生 interrupt"
        assert "第 1 轮" in snaps[0][0].value["questions"][0]

        app.invoke(Command(resume="给业务分析师看的 Markdown 报告"), config)
        snaps.append(_interrupts(app.get_state(config)))
        assert snaps[1], "第二_round：提问轮数未用完，应产生第二次 interrupt"
        assert "第 2 轮" in snaps[1][0].value["questions"][0]

        final = app.invoke(Command(resume="输出必须包含数据表格"), config)

    assert final["clarify_questions_asked"] == 2
    assert final["clarification"]["is_clear"] is True
    assert "给业务分析师看的 Markdown 报告" in final["clarification_answers"]
    assert "输出必须包含数据表格" in final["clarification_answers"]
    assert final["status"] == "passed"


def test_third_unclear_round_stops_asking():
    """第三轮仍不清晰：不再提问，带推断上下文直接进入优化（2 轮上限）。"""
    _unclear_twice_structured.calls = -10  # type: ignore[attr-defined]  # 永远不清晰
    with testing.scope("progress", structured=_fake_structured_always_unclear):
        app = build_app()
        config = {"configurable": {"thread_id": "m8-three-rounds"}, "recursion_limit": 200}
        init = initial_state(task="写个 prompt", target_model="fake", auto_clarify=False)

        app.invoke(init, config)
        assert _interrupts(app.get_state(config)), "第一轮 interrupt"

        app.invoke(Command(resume="第一次回答"), config)
        assert _interrupts(app.get_state(config)), "第二轮 interrupt"

        final = app.invoke(Command(resume="第二次回答"), config)

    assert final["clarify_questions_asked"] == 2
    # 不再产生第三次 interrupt：直接带着推断上下文走完主路
    assert any(t["node"] == "optimize" for t in final["trace"])
    assert final["status"] in ("passed", "max_iterations", "early_stopped")


def _fake_structured_always_unclear(role, model_cls, system, user, max_retries=3, overrides=None):
    if model_cls is ClarificationResult:
        return ClarificationResult(
            is_clear=False,
            task_summary="（fake）始终不清晰",
            inferred_context=InferredContext(
                target_audience="未知",
                domain="未知",
                output_format="未知",
                tone="未知",
                constraints=["需求描述缺少关键信息"],
            ),
            clarifying_questions=["到底要什么？"],
            suggested_next_step="ask_user",
        ), {"role": role, "model": "fake-model", "channel": "fake", "attempts": 1, "latency_ms": 1}
    return _fake_structured(role, model_cls, system, user, max_retries, overrides)
