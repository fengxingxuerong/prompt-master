"""
双评委交叉验证 + 仲裁 的回归测试。

覆盖：
- PM_JUDGES 默认 2 / 可降级 1（向后兼容）
- 双评委分差 ≤ 阈值 → merged（各维度取均值）
- 双评委分差 > 阈值 → 第三评委仲裁（arbiter）
- progress / dispute 场景在整条图上都能跑通
"""

from __future__ import annotations

from pm import testing
from pm.graph import build_app
from pm.nodes import _active_judges, _merge_judge_results
from pm.schemas import DimensionScores, EvaluationResult
from pm.state import initial_state


def _mk_ev(score: float, idx: int = 0, revise: bool = False) -> EvaluationResult:
    d = round(score)
    return EvaluationResult(
        dimension_scores=DimensionScores(
            task_completion=d,
            format_adherence=d,
            constraint_compliance=d,
            robustness=d,
            quality=d,
        ),
        model_reported_score=score,
        should_revise=revise,
        test_case_index=idx,
    ).finalize()


# --------------------------------------------------------------------------
# 1. 评委配置
# --------------------------------------------------------------------------
def test_active_judges_default_two(monkeypatch):
    monkeypatch.delenv("PM_JUDGES", raising=False)
    assert _active_judges() == ["evaluator", "evaluator_b"]


def test_active_judges_single_mode(monkeypatch):
    monkeypatch.setenv("PM_JUDGES", "1")
    assert _active_judges() == ["evaluator"]


def test_active_judges_clamped(monkeypatch):
    monkeypatch.setenv("PM_JUDGES", "99")
    assert _active_judges() == ["evaluator", "evaluator_b"]


# --------------------------------------------------------------------------
# 2. 合并逻辑（单元级）
# --------------------------------------------------------------------------
def test_merge_consistent_judges_average():
    """分差 ≤ 阈值：各维度取均值，issues/suggestions 并集，should_revise 保守。"""
    a = _mk_ev(8.0, revise=False)
    b = _mk_ev(9.0, revise=False)
    merged = _merge_judge_results([("evaluator", a), ("evaluator_b", b)], 0, "prompt")
    assert merged.judge == "merged"
    assert merged.weighted_score == 8.5  # 各维度 8 与 9 的均值
    assert merged.judge_scores == {"evaluator": 8.0, "evaluator_b": 9.0}
    assert merged.judge_disagreement == 1.0
    assert merged.passed is True


def test_merge_conservative_revise():
    """任一评委要求修订 → merged 也要求修订。"""
    a = _mk_ev(8.0, revise=False)
    b = _mk_ev(8.5, revise=True)
    merged = _merge_judge_results([("evaluator", a), ("evaluator_b", b)], 0, "prompt")
    assert merged.judge == "merged"
    assert merged.should_revise is True


def test_merge_arbitration_on_disagreement():
    """分差 > 阈值：触发仲裁评委；fake dispute 场景仲裁给 6.0。"""
    a = _mk_ev(9.0)
    b = _mk_ev(4.0)
    with testing.fake_backend("dispute"):
        merged = _merge_judge_results([("evaluator", a), ("evaluator_b", b)], 0, "prompt")
    assert merged.judge == "arbiter"
    assert merged.weighted_score == 6.0
    assert merged.judge_scores == {"evaluator": 9.0, "evaluator_b": 4.0}
    assert merged.judge_disagreement == 5.0


def test_merge_single_judge_passthrough():
    a = _mk_ev(8.8)
    merged = _merge_judge_results([("evaluator", a)], 0, "prompt")
    assert merged is a
    assert merged.judge == "evaluator"


# --------------------------------------------------------------------------
# 2.5 模型输出 null 容错（真实 e2e 发现：模型会把代码侧字段填 null）
# --------------------------------------------------------------------------
def test_null_code_side_fields_tolerated():
    """模型按 schema 把 judge_scores/judge/cache_hit 填 null → 恢复默认值，不炸。"""
    ev = EvaluationResult.model_validate(
        {
            "dimension_scores": {
                "task_completion": 8,
                "format_adherence": 7,
                "constraint_compliance": 8,
                "robustness": 6,
                "quality": 7,
            },
            "model_reported_score": 7.5,
            "issues": [],
            "suggestions": [],
            "should_revise": True,
            "test_case_index": 0,
            "judge": None,
            "judge_scores": None,
            "judge_disagreement": None,
            "cache_hit": None,
        }
    )
    assert ev.judge == "evaluator"
    assert ev.judge_scores == {}
    assert ev.judge_disagreement is None
    assert ev.cache_hit is False
    assert ev.should_revise is True  # 核心字段不受影响


# --------------------------------------------------------------------------
# 3. 整图跑通
# --------------------------------------------------------------------------
def test_dual_judge_progress_full_loop():
    """双评委一致（progress 场景）：merged 路径，最终达标。"""
    with testing.fake_backend("progress"):
        app = build_app()
        final = app.invoke(
            initial_state(task="分析销售数据", target_model="fake", n_test_cases=3),
            {"configurable": {"thread_id": "dual-progress"}, "recursion_limit": 100},
        )
    assert final["status"] == "passed"
    evals = [EvaluationResult.model_validate(e) for e in final["evaluations"]]
    assert len(evals) == 3
    assert all(e.judge == "merged" for e in evals)
    assert all(set(e.judge_scores) == {"evaluator", "evaluator_b"} for e in evals)
    # 双评委 + 迭代：LLM 调用数应明显多于单评委场景
    assert final["llm_calls"] >= 18


def test_single_judge_mode_full_loop(monkeypatch):
    """PM_JUDGES=1：走回单评委路径，行为与旧版一致。"""
    monkeypatch.setenv("PM_JUDGES", "1")
    with testing.fake_backend("progress"):
        app = build_app()
        final = app.invoke(
            initial_state(task="分析销售数据", target_model="fake", n_test_cases=3),
            {"configurable": {"thread_id": "single-judge"}, "recursion_limit": 100},
        )
    assert final["status"] == "passed"
    evals = [EvaluationResult.model_validate(e) for e in final["evaluations"]]
    assert all(e.judge == "evaluator" for e in evals)


def test_dual_judge_dispute_full_loop():
    """双评委分歧（dispute 场景）：走仲裁，最终如实判定未达标并兜底交付。"""
    with testing.fake_backend("dispute"):
        app = build_app()
        final = app.invoke(
            initial_state(
                task="分析销售数据", target_model="fake", n_test_cases=3, max_iterations=1
            ),
            {"configurable": {"thread_id": "dual-dispute"}, "recursion_limit": 100},
        )
    assert final["status"] == "max_iterations"
    evals = [EvaluationResult.model_validate(e) for e in final["evaluations"]]
    assert len(evals) == 3
    assert all(e.judge == "arbiter" for e in evals)
    assert all(e.weighted_score == 6.0 for e in evals)
    assert all(e.judge_disagreement == 5.0 for e in evals)
    assert "未达标" in final["final_report"]
