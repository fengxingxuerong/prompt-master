"""
双评委交叉验证 + 仲裁 的回归测试。

覆盖：
- PM_JUDGES 默认 2 / 可降级 1（向后兼容）
- 双评委分差 ≤ 阈值 → merged（各维度取均值）
- 双评委分差 > 阈值 → 第三评委仲裁（arbiter）
- progress / dispute 场景在整条图上都能跑通
"""

from __future__ import annotations

from pm import backend, testing
from pm.graph import build_app
from pm.nodes import _active_judges, _evaluate_one, _merge_judge_results
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


# --------------------------------------------------------------------------
# 4. 全评委失败的系统兜底（真实场景：双评委同时 429 / 端点不可达）
# --------------------------------------------------------------------------
def _judge_always_fail(role, model_cls, system, user, max_retries=3, overrides=None):
    raise RuntimeError("评委端点不可达")


def test_all_judges_fail_yields_system_fallback():
    """两个评委都抛异常：不崩图，落 judge=system 的 1.0 兜底评估，错误逐个记账。"""
    hook = backend.CallHook(structured=_judge_always_fail, plain=None, disable_cache=True)
    errors: list[str] = []
    run = {"test_case_index": 0, "test_input": "x", "output": "y"}
    with backend.use(hook):
        ev, n_calls, cache_hit = _evaluate_one(
            run,
            task="t",
            context="",
            prompt="p",
            judges=["evaluator", "evaluator_b"],
            judge_spec="",
            q_warns=[],
            errors=errors,
        )
    assert ev["judge"] == "system"
    assert ev["model_reported_score"] == 1.0
    assert ev["should_revise"] is False  # 基础设施问题，修订提示词无意义
    assert any("全部评委评估失败" in i for i in ev["issues"])
    assert "PM_EVALUATOR_*" in " ".join(ev["suggestions"])
    assert n_calls == 0 and cache_hit is False
    assert len(errors) == 2, f"两个评委的失败都要记账：{errors}"
    assert "evaluator_b" in errors[-1]


def test_partial_judge_failure_degrades_to_single():
    """evaluator 挂、evaluator_b 活：降级为单评委结果，不崩、错误只记挂掉的那个。

    真实运行（sensenova 429）出现过单评委失败——这条降级路径的分数仍可用，
    但报告应保留 merged 单结果口径而不是编造双评委一致性。
    """

    def flaky(role, model_cls, system, user, max_retries=3, overrides=None):
        if role == "evaluator":
            raise RuntimeError("evaluator 端点限流")
        return (
            _mk_ev(8.0),
            {"model": "f", "channel": "fake", "attempts": 1, "latency_ms": 1},
        )

    hook = backend.CallHook(structured=flaky, plain=None, disable_cache=True)
    errors: list[str] = []
    run = {"test_case_index": 0, "test_input": "x", "output": "y"}
    with backend.use(hook):
        ev, n_calls, cache_hit = _evaluate_one(
            run,
            task="t",
            context="",
            prompt="p",
            judges=["evaluator", "evaluator_b"],
            judge_spec="",
            q_warns=[],
            errors=errors,
        )
    assert ev["judge"] == "evaluator"  # 单结果 passthrough，不冒充 merged
    assert ev["model_reported_score"] == 8.0
    assert n_calls == 1 and cache_hit is False
    assert len(errors) == 1
    assert errors[0].startswith("evaluate#0[evaluator]:")


# --------------------------------------------------------------------------
# 2.7 仲裁的出处账与失败回退（发现 13：35% 的判定轮出自仲裁者一人）
# --------------------------------------------------------------------------
def _arbiter_failing_structured():
    """只让仲裁这一路抛错，其余角色仍走假后端。"""

    def hook(role, model_cls, system, user, max_retries=3, overrides=None):
        if role == "arbiter":
            raise RuntimeError("engine is not available temporarily")
        return testing._fake_structured(role, model_cls, system, user, max_retries, overrides)

    return hook


def test_arbiter_failure_falls_back_to_stricter_judge_with_full_provenance():
    """仲裁调用失败：取较低分（安全侧），但出处账要和仲裁成功时一样齐。

    报告新增的那行会告诉读者"逐评委的原始分在 `judge_scores`"——回退路径要是把它留空，
    那句话就成了假的（真跑里端点抖动是常态，回退不是理论分支）。
    """
    a = _mk_ev(9.0)
    b = _mk_ev(4.0)
    with testing.scope("dispute", structured=_arbiter_failing_structured()):
        merged = _merge_judge_results([("evaluator", a), ("evaluator_b", b)], 0, "P")
    assert merged.judge == "conservative"
    assert merged.weighted_score == 4.0, "安全侧=取较低分，不是均值"
    assert merged.judge_scores == {"evaluator": 9.0, "evaluator_b": 4.0}
    assert merged.judge_disagreement == 5.0


def test_arbiter_is_shown_both_scores_only_because_it_is_told_to_ignore_them():
    """把"给看分数 + 要求忽略"这对共生条件钉住（发现 13 第 3 条的锚定怀疑）。

    只给分不要求独立 = 纯锚定，比现状更糟；只要求忽略却已经给了分 = 现状，可疑但至少声明了意图。
    将来若按处置意见改成"不把两个分数给仲裁"，前半段断言随之反转，
    但"仲裁仍拿到完整原始评审 prompt"这条不许跟着删——那是它能独立复核的前提。
    """
    seen: dict[str, str] = {}

    def hook(role, model_cls, system, user, max_retries=3, overrides=None):
        out = testing._fake_structured(role, model_cls, system, user, max_retries, overrides)
        if role == "arbiter":
            seen["user"] = user
        return out

    with testing.scope("dispute", structured=hook):
        merged = _merge_judge_results(
            [("evaluator", _mk_ev(9.0)), ("evaluator_b", _mk_ev(4.0))], 0, "<原始评审全文>"
        )
    assert merged.judge == "arbiter"
    u = seen["user"]
    assert "给出 9.0" in u and "给出 4.0" in u, "分数确实被摊给仲裁看了"
    assert "忽略前两位评委的分数" in u, "既然给了分，就必须同时要求独立复核"
    assert u.startswith("<原始评审全文>"), "仲裁拿到的原始评审内容不能因仲裁块而残缺"


def test_provenance_literals_stay_consistent_across_judge_and_report():
    """报告按字符串认"分数出处"，评委侧改了值就会静默不报——两边必须同源。

    同一类缺陷在质量门 marker 上栽过（P9）：消费侧手抄字面量，生产侧改名后无人变红。
    顺带检查 schema 里给模型看的那份枚举，别说漏了代码真会写的值。
    """
    import inspect
    import re

    from pm import report
    from pm.nodes import judge as judge_mod
    from pm.schemas import EvaluationResult

    written = set(re.findall(r'\.judge = "([a-z_]+)"', inspect.getsource(judge_mod)))
    written |= set(re.findall(r'judge="([a-z_]+)"', inspect.getsource(judge_mod)))
    assert {"merged", "arbiter", "conservative"} <= written

    counted = set(re.findall(r'e\.get\("judge"\) == "([a-z_]+)"', inspect.getsource(report)))
    assert counted, "报告里数出处的那段没了就该改这条测试，而不是留着当摆设"
    assert counted <= written, f"报告在数评委侧不会写的值：{counted - written}"

    desc = EvaluationResult.model_fields["judge"].description
    for v in sorted(written):
        assert v in desc, f"给模型看的 judge 枚举漏了 {v}"
