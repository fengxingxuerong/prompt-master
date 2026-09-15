"""测量层回归测试：重复采样、不确定度判定、基线对比、成对盲评、噪声感知早停。

对应"顶级提示词工程师视角"评审里的三个方法学缺口：没有基线、单次采样当结论、
只有 pointwise 绝对分。这些测试验证的是**测量机制**，不是优化效果。
"""

from __future__ import annotations

from pm import backend, testing
from pm.graph import build_app
from pm.nodes import (
    _collapse_samples,
    _collect_assertions,
    _feature_enabled,
    _model_profile,
    _samples_per_case,
)
from pm.schemas import (
    AggregateScore,
    DimensionScores,
    EvaluationResult,
    PreferenceResult,
    early_stop_reason,
    noise_margin,
)
from pm.state import initial_state

CFG = {"recursion_limit": 80}


def _ev(score: float, *, reported: float | None = None, spread: float = 0.0, idx: int = 0):
    e = EvaluationResult(
        dimension_scores=DimensionScores(
            task_completion=score,
            format_adherence=score,
            constraint_compliance=score,
            robustness=score,
            quality=score,
        ),
        model_reported_score=reported if reported is not None else score,
        issues=[],
        suggestions=[],
        should_revise=False,
        test_case_index=idx,
    )
    e.finalize()
    e.score_spread = spread
    return e


# --------------------------------------------------------------------------
# 默认口径
# --------------------------------------------------------------------------
def test_defaults_enable_repeated_sampling_and_baseline(monkeypatch):
    """新默认值：每条用例采 2 次、跑基线、跑成对盲评。"""
    monkeypatch.delenv("PM_SAMPLES_PER_CASE", raising=False)
    monkeypatch.delenv("PM_BASELINE", raising=False)
    monkeypatch.delenv("PM_PAIRWISE", raising=False)
    assert _samples_per_case() == 2
    assert _feature_enabled("PM_BASELINE", True) is True
    assert _feature_enabled("PM_PAIRWISE", True) is True
    # 非法值不得炸流程
    monkeypatch.setenv("PM_SAMPLES_PER_CASE", "abc")
    assert _samples_per_case() == 2
    monkeypatch.setenv("PM_SAMPLES_PER_CASE", "99")
    assert _samples_per_case() == 5, "上限封顶，避免一次请求把预算烧穿"


# --------------------------------------------------------------------------
# 采样聚合：中位数定分、极差当噪声
# --------------------------------------------------------------------------
def test_collapse_samples_uses_median_and_spread():
    evals = [
        {"test_case_index": 0, "weighted_score": 9.0, "passed": True, "issues": ["x"]},
        {"test_case_index": 0, "weighted_score": 5.0, "passed": False, "issues": ["y"]},
        {"test_case_index": 0, "weighted_score": 7.0, "passed": True, "issues": ["z"]},
        {"test_case_index": 1, "weighted_score": 8.0, "passed": True, "issues": []},
    ]
    out = _collapse_samples(evals)
    assert len(out) == 2
    c0 = out[0]
    assert c0["weighted_score"] == 7.0, "取中位数而不是均值或极值"
    assert c0["n_samples"] == 3
    assert c0["score_spread"] == 4.0
    assert c0["sample_scores"] == [9.0, 5.0, 7.0]
    assert c0["passed"] is False, "中位数 7.0 < 阈值，一次走运的高分样本救不回来"
    assert out[1]["score_spread"] == 0.0, "单样本没有极差"


def test_ci_lower_and_unstable_cases_gate_the_verdict():
    """均分达标但噪声过大 → 保守下界不足，不判达标，并点名不稳用例。"""
    agg = AggregateScore.from_evaluations(
        [_ev(9.0, spread=5.0, idx=0), _ev(9.0, spread=5.0, idx=1)],
        n_expected=2,
    )
    assert agg.avg_score == 9.0
    assert agg.noise == 5.0
    assert agg.ci_lower < 8.0, "下界必须被噪声压低"
    assert agg.passed is False
    assert agg.unstable_cases == [0, 1]
    assert any("不确定度" in i for i in agg.all_issues)


def test_single_sample_keeps_pointwise_behaviour():
    """k=1 且多用例时退化为原判定（向后兼容，不额外卡人）。"""
    agg = AggregateScore.from_evaluations([_ev(8.4), _ev(8.2)], n_expected=2)
    assert agg.noise == 0.0
    assert agg.passed is True


def test_judge_bias_is_surfaced():
    """评委自报分系统性高于代码加权分 → 报告要能看出它在放水。"""
    agg = AggregateScore.from_evaluations([_ev(6.0, reported=9.0), _ev(6.0, reported=9.0)])
    assert agg.judge_bias == 3.0
    assert agg.judge_bias_warning is True
    assert any("评委可靠性" in i for i in agg.all_issues)


def test_assertions_any_sample_failure_vetoes_case():
    """同一用例的任一样本断言失败，就要按未通过处理（保守侧）。"""
    runs = [
        {"test_case_index": 0, "sample_index": 0, "assertion": {"passed": True}},
        {"test_case_index": 0, "sample_index": 1, "assertion": {"passed": False}},
        {"test_case_index": 1, "sample_index": 0, "assertion": {"passed": True}},
    ]
    got = _collect_assertions(runs)
    assert got[0]["passed"] is False
    assert got[1]["passed"] is True


# --------------------------------------------------------------------------
# 噪声感知的早停余量
# --------------------------------------------------------------------------
def test_noise_widens_the_plateau_margin():
    # 无噪声：+0.25 的增益超过 0.2 余量 → 继续修订
    assert early_stop_reason([8.0, 8.25, 8.5]) is None
    # 噪声 0.3 → 余量抬到 0.6，同一轨迹判为平台期，省下无效迭代预算
    reason = early_stop_reason([8.0, 8.25, 8.5], noise=0.3)
    assert reason and "平台期" in reason
    assert noise_margin(0.3) == 0.6
    assert noise_margin(0.0) == 0.2


# --------------------------------------------------------------------------
# 模型档案：把"适配目标模型"从口号变成按模型注入的具体档案
# --------------------------------------------------------------------------
def test_model_profile_is_model_specific():
    claude = _model_profile("claude-sonnet-4.5")
    deepseek = _model_profile("deepseek-v4-flash")
    unknown = _model_profile("未指定")
    assert "XML" in claude
    assert "短、直、显式" in deepseek
    assert claude != deepseek, "不同家族必须给不同档案，否则等于没写"
    assert "通用最佳实践" in unknown


# --------------------------------------------------------------------------
# 基线与成对盲评：走完整图
# --------------------------------------------------------------------------
def _run_loop(monkeypatch, *, comparator=None):
    monkeypatch.setenv("PM_SAMPLES_PER_CASE", "2")
    monkeypatch.setenv("PM_BASELINE", "1")
    monkeypatch.setenv("PM_PAIRWISE", "1")
    structured = testing._fake_structured if comparator is None else comparator

    with testing.scope("progress", structured=structured):
        return build_app().invoke(
            initial_state(
                task="让AI分析销售数据", target_model="fake", n_test_cases=3, max_iterations=1
            ),
            {"configurable": {"thread_id": f"meas-{comparator is None}"}, **CFG},
        )


def test_baseline_and_pairwise_produce_a_comparable_delta(monkeypatch):
    final = _run_loop(monkeypatch)
    base = final.get("baseline_aggregate") or {}
    agg = final.get("aggregate") or {}
    assert base, "基线必须产出聚合分，否则报告无法回答'比不优化好多少'"
    assert base["n_cases"] == agg["n_cases"] == 3, "基线与主路必须同口径（同用例、同采样数）"
    assert base["n_samples"] == 2 and agg["n_samples"] == 2
    report = final["final_report"]
    assert "与基线对比" in report and "Δ" in report
    assert "成对盲评" in report
    assert "置信度与采样噪声" in report
    pw = final.get("pairwise") or {}
    assert pw.get("verdict") == "tie", "假后端统一判持平（它不知道哪侧是优化版）"
    assert pw.get("votes", {}).get("tie") == 3
    # 基线跑过 target + 评委，调用数应显著高于"只有主路"的量级
    assert final["llm_calls"] > 20


def test_ab_randomisation_cannot_flip_the_conclusion(monkeypatch):
    """评委始终选“含 OPTIMIZED 的那侧”时，无论 A/B 怎么映射，结论都必须是 better。"

    这同对验证了两件事：映射反推逻辑正确，以及随机化真的在发生（否则位置偏好会污染结论）。
    """
    from pm.nodes import compare_node

    monkeypatch.setenv("PM_PAIRWISE", "1")  # 全局夹具默认关掉新测量层，这里按需打开

    def comparator(role, model_cls, system, user, max_retries=3, overrides=None):
        a_block = user.split("<CANDIDATE_A>")[1].split("</CANDIDATE_A>")[0]
        winner = "A" if "OPTIMIZED" in a_block else "B"
        return (
            PreferenceResult(winner=winner, reason="结构与约束满足度更高", decisive=True),
            {"model": "f", "channel": "fake", "attempts": 1, "latency_ms": 1},
        )

    st = initial_state(task="t", target_model="fake", n_test_cases=6)
    st["run_id"] = "fixed-seed"
    st["test_cases"] = [f"i{i}" for i in range(6)]
    st["test_runs"] = [
        {
            "test_case_index": i,
            "sample_index": 0,
            "test_input": f"i{i}",
            "prompt": "p",
            "output": f"OPTIMIZED 输出 {i}",
        }
        for i in range(6)
    ]
    st["baseline_runs"] = [
        {
            "test_case_index": i,
            "sample_index": 0,
            "test_input": f"i{i}",
            "prompt": "t",
            "output": f"BASELINE 输出 {i}",
        }
        for i in range(6)
    ]

    hook = backend.CallHook(structured=comparator, disable_cache=True)
    with backend.use(hook):
        out = compare_node(st)  # type: ignore[arg-type]

    pw = out["pairwise"]
    assert pw["verdict"] == "better"
    assert pw["votes"] == {"better": 6, "worse": 0, "tie": 0}
    assert {d["side_a"] for d in pw["details"]} == {"cur", "base"}, "A/B 必须被随机映射"


def test_ab_flip_is_balanced_and_reproducible():
    from pm.nodes import _ab_flip

    flips = [_ab_flip("run-x", i) for i in range(24)]
    assert 6 <= sum(flips) <= 18, "随机映射应大致均衡，不能退化成永远同一侧"
    assert flips == [_ab_flip("run-x", i) for i in range(24)], "同一 run 内必须可复现"
    assert [_ab_flip("a", i) for i in range(8)] != [_ab_flip("b", i) for i in range(8)]


def test_baseline_can_be_disabled(monkeypatch):
    monkeypatch.setenv("PM_BASELINE", "0")
    monkeypatch.setenv("PM_PAIRWISE", "0")
    monkeypatch.setenv("PM_SAMPLES_PER_CASE", "1")
    with testing.scope("progress"):
        final = build_app().invoke(
            initial_state(
                task="让AI分析销售数据", target_model="fake", n_test_cases=3, max_iterations=1
            ),
            {"configurable": {"thread_id": "meas-off"}, **CFG},
        )
    assert not final.get("baseline_aggregate")
    assert "未跑基线" in final["final_report"], "关掉基线时，报告要明说无法回答是否变好"


def test_no_baseline_run_without_optimize_success():
    """optimize 失败时 compare 必须安全透传，不能因为缺基线炸掉。"""

    def boom(role, system, user, overrides=None):
        if role in ("optimizer", "reviser"):
            raise RuntimeError("HTTP 503")
        return testing._fake_plain(role, system, user, overrides)

    with testing.scope("progress", plain=boom):
        final = build_app().invoke(
            initial_state(task="t", target_model="fake", n_test_cases=2, max_iterations=1),
            {"configurable": {"thread_id": "meas-fail"}, **CFG},
        )
    assert final["status"] == "failed"
    assert final.get("pairwise") in (None, {}), "没有可比较的两份输出时不应硬凑结论"


# --- M4：噪声不可估时不收窄余量（单次采样不能把"测不出"当成"没有"） ---


def test_unestimable_noise_does_not_declare_plateau():
    """k=1 时极差恒为 0：那是"测不出噪声"，不是"没有噪声"。"""
    from pm.schemas import early_stop_reason

    traj = [8.63, 8.93, 8.8, 8.75]
    est = early_stop_reason(traj, noise=0.0)
    assert est is not None and "平台期" in est, "噪声可估（k≥2 且极差为 0）时仍应保持旧判定"
    assert early_stop_reason(traj, noise=None) is None, "单次采样不该靠平台期把修订提前卡死"


def test_unestimable_noise_widens_the_regression_margin():
    """同一串轨迹：可估时按 0.2 判回退，不可估时要求看得见 0.5 的差距。"""
    from pm.schemas import early_stop_reason

    assert "回退" in str(early_stop_reason([8.5, 8.05], noise=0.0))
    assert early_stop_reason([8.5, 8.05], noise=None) is None
    assert "回退" in str(early_stop_reason([8.5, 7.9], noise=None)), "真掉下去还是要停"


def test_estimable_noise_widens_regression_margin_k2():
    """k=2 实测噪声（第 12 轮真实运行 noise=0.39）下回退余量放宽到 2×noise。

    "看起来掉了 0.3"在余量 0.78 内 → 继续修订；真掉 1.0 → 仍要停。
    """
    from pm.schemas import early_stop_reason

    assert early_stop_reason([9.2, 8.9], noise=0.39) is None, (
        "0.3 的落差在 2×noise 余量内，是噪声不是回退"
    )
    assert early_stop_reason([9.2, 8.9], noise=None) is None
    assert "回退" in str(early_stop_reason([9.2, 8.2], noise=0.39)), (
        "1.0 的落差超过放宽后的余量，是真回退"
    )


def test_estimable_noise_keeps_plateau_enabled_k2():
    """k=2 平台期保持启用，k=1 关闭：同一轨迹不同噪声估计给出相反决策。

    轨迹 [9.2, 8.6, 8.7]：历史最佳 9.2 的门槛放宽到 9.98，连续两轮未超过 → 停；
    k=1 时平台期整体关闭且回退在余量内 → 继续。M4 的完整语义就是这一对对比。
    """
    from pm.schemas import early_stop_reason

    traj = [9.2, 8.6, 8.7]
    est = early_stop_reason(traj, noise=0.39)
    assert est is not None and "平台期" in est, "k=2 下连续两轮未超过历史最佳（含余量）应判平台期"
    assert early_stop_reason(traj, noise=None) is None, (
        "k=1 平台期规则关闭，且 8.6→8.7 在回升，应继续修订"
    )


def test_noise_margin_floors_and_defaults():
    from pm import schemas
    from pm.schemas import noise_margin

    assert noise_margin(0.0) == schemas.PLATEAU_MARGIN
    assert abs(noise_margin(0.35) - 0.7) < 1e-9
    assert noise_margin(-5) == schemas.PLATEAU_MARGIN, "负噪声不能把余量压到 0.2 以下"
    assert noise_margin(None) == max(schemas.PLATEAU_MARGIN, schemas.UNESTIMATED_MARGIN)


def test_env_float_is_tolerant(monkeypatch):
    """一个手抖的 .env 不该炸掉整条判定链（A2 同源问题，这次覆盖 schemas 侧常量）。"""
    from pm.schemas import _env_float

    monkeypatch.setenv("PM_TEST_VAL", "")
    assert _env_float("PM_TEST_VAL", 0.5) == 0.5
    monkeypatch.setenv("PM_TEST_VAL", "  ")
    assert _env_float("PM_TEST_VAL", 0.5) == 0.5
    monkeypatch.setenv("PM_TEST_VAL", "abc")
    assert _env_float("PM_TEST_VAL", 0.5) == 0.5
    monkeypatch.setenv("PM_TEST_VAL", "0")
    assert _env_float("PM_TEST_VAL", 0.5, positive_only=True) == 0.5
    monkeypatch.setenv("PM_TEST_VAL", "0.7")
    assert _env_float("PM_TEST_VAL", 0.5, positive_only=True) == 0.7
    monkeypatch.setenv("PM_TEST_VAL", "-1")
    assert _env_float("PM_TEST_VAL", 0.5) == -1.0, "不要求正数时就该照收（保留显式覆盖能力）"


def test_single_sample_loop_is_not_stopped_by_unmeasurable_noise(monkeypatch):
    """整图验证 M4：k=1 的持平轨迹不该被"平台期"提前判停（旧口径会）。"""
    from pm.schemas import early_stop_reason

    monkeypatch.setenv("PM_SAMPLES_PER_CASE", "1")
    monkeypatch.setenv("PM_BASELINE", "0")
    monkeypatch.setenv("PM_PAIRWISE", "0")

    with testing.scope("stall"):
        final = build_app().invoke(
            initial_state(
                task="让AI分析销售数据",
                target_model="fake",
                n_test_cases=3,
                max_iterations=3,
            ),
            {"configurable": {"thread_id": "m4-k1"}, **CFG},
        )
    traj = [
        v["avg_score"] for v in final.get("prompt_versions", []) if v.get("avg_score") is not None
    ]
    assert len(traj) >= 3, f"没跑出可比轨迹：{traj}"
    assert early_stop_reason(traj, noise=0.0) is not None, (
        "前提失效：旧口径本来也不会判停，这条用例没测到东西"
    )
    assert not final.get("early_stop_reason"), "单次采样仍被提前判停了"
    assert final.get("status") == "max_iterations", final.get("status")
