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

import pytest
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


def test_route_needs_reanalysis_goes_back_to_clarify():
    """收到用户回答后必须重回 clarify 重分析，而不是直接 optimize。

    这是 `ask_user` → `clarify` 那条回边的路由依据：漏了它，
    用户答完就被当成"已分析过"直接进入优化 —— 答案永远进不了优化器
    （`docs/design-notes.md` §二.1/§二.3 记的就是这类事故）。
    """
    s = _state_for_route(needs_reanalysis=True)
    assert route_after_clarify(s) == "clarify"


def test_route_needs_reanalysis_wins_over_clarity():
    """`needs_reanalysis` 优先级高于"看起来已经清晰"。

    重分析期间 `clarification` 可能还留着上一轮的 `is_clear=True`，
    若先判清晰就会跳过重分析、把用户的回答丢掉 —— 顺序是行为的一部分。
    """
    s = _state_for_route(
        needs_reanalysis=True,
        clarification={
            "is_clear": True,
            "task_summary": "上一轮的结论",
            "inferred_context": {},
            "clarifying_questions": [],
            "suggested_next_step": "optimize",
        },
    )
    assert route_after_clarify(s) == "clarify"


def test_route_failed_status_short_circuits_to_report():
    """失败态必须直通 report（否则会带着空 prompt 继续烧调用）。"""
    assert route_after_clarify(_state_for_route(status="failed")) == "report"


def test_route_none_clarification_reenters_clarify():
    """clarification 还没产出时不能放行去 optimize。"""
    assert route_after_clarify(_state_for_route(clarification=None)) == "clarify"


# --------------------------------------------------------------------------
# build_app(sqlite_path=...)：文档承诺的「跨进程恢复」此前零覆盖
# --------------------------------------------------------------------------
def test_build_app_with_sqlite_checkpoint_persists_state(tmp_path):
    """`--checkpoint` 的语义是**状态落到 SQLite**，可被另一个实例读回。

    这是 `docs/agent-cli-guide.md` 明确承诺的能力（"带 --checkpoint 可跨进程断点续跑"），
    而 `build_app` 的 sqlite 分支此前从未被执行过 —— 承诺从没被验证。
    这里用**新建一个 app 实例**读取上一实例写下的状态来证明持久化真的发生，
    而不是只断言"函数没抛异常"（后者对"能不能续跑"毫无信息量）。
    """
    from pm.graph import build_app as _build_app
    from pm.state import initial_state as _init

    db = tmp_path / "ckpt.db"
    init = _init(task="让 AI 分析销售数据", target_model="fake-target", n_test_cases=2)
    cfg = {"configurable": {"thread_id": "persist-probe"}, "recursion_limit": 40}

    with testing.fake_backend(scenario="progress"):
        app1 = _build_app(sqlite_path=str(db))
        app1.invoke(init, cfg)
        seen_by_first = app1.get_state(cfg).values

        # 关键：换一个**全新的 app 实例**（模拟另一次进程启动）读同一份 SQLite
        app2 = _build_app(sqlite_path=str(db))
        seen_by_second = app2.get_state(cfg).values

    assert db.exists() and db.stat().st_size > 0, "SQLite 检查点文件没被写出来"
    assert seen_by_first.get("run_id") == init["run_id"]
    # 跨实例读回同一线程的状态 = 持久化生效（若走 MemorySaver，第二个实例读不到）
    assert seen_by_second.get("run_id") == init["run_id"]
    assert seen_by_second.get("task") == "让 AI 分析销售数据"
    assert seen_by_second.get("final_report"), "续跑依赖的交付内容没落进检查点"


def test_build_app_without_sqlite_uses_in_memory_saver(tmp_path):
    """不传路径时用内存检查点：同样是"能跑"，但**不落盘**（别指望跨进程）。"""
    from pm.graph import build_app as _build_app
    from pm.state import initial_state as _init

    before = set(tmp_path.iterdir())
    app = _build_app()
    cfg = {"configurable": {"thread_id": "mem-probe"}, "recursion_limit": 40}
    with testing.fake_backend(scenario="progress"):
        app.invoke(
            _init(task="让 AI 分析销售数据", target_model="fake-target", n_test_cases=2), cfg
        )
    assert app.get_state(cfg).values.get("final_report")
    assert set(tmp_path.iterdir()) == before, "内存模式不该产生任何文件"


def test_build_app_sqlite_rejects_unwritable_path(tmp_path):
    """路径不可写时必须**报错**，不许静默退化成内存检查点。

    静默退化比报错危险得多：调用方以为开了跨进程恢复，实际没有，
    等真要续跑时才发现状态是空的 —— 而那时已经花掉了一整轮的调用。
    """
    import sqlite3

    bad = tmp_path / "不存在的目录" / "x" / "ckpt.db"
    # 断言**具体**异常类型而不是裸 Exception（实测就是 sqlite3.OperationalError）：
    # 若将来这里被改成"捕获后静默退化"，类型就变了 —— 那个改动必须让这条红。
    with pytest.raises(sqlite3.OperationalError):
        build_app(sqlite_path=str(bad))
