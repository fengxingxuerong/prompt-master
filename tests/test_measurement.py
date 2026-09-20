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
    _is_empty_output_eval,
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


def test_collapse_samples_drops_empty_output_samples():
    """空输出（推理耗尽预算、未抛异常）是无效样本，不能拉低均值。

    2026-09-16 矩阵 A（客服分诊）实测：基线 8 条里 4 条 output 为空（error=None、
    latency 58~71s），却被当 1.0 分计入 → **基线被系统性低估、Δ 被夸大**。
    只要该用例还有有效采样，就只聚合有效样本。
    """
    evals = [
        {"test_case_index": 0, "weighted_score": 9.0, "issues": []},
        {"test_case_index": 0, "weighted_score": 1.0, "issues": ["目标模型调用失败：empty_output"]},
        {"test_case_index": 0, "weighted_score": 8.0, "issues": []},
    ]
    out = _collapse_samples(evals)
    c0 = out[0]
    assert c0["n_samples"] == 2, "空输出那条应被剔除"
    assert c0["weighted_score"] == 8.5, "只取有效样本 9.0/8.0 的中位数，而不是被 1.0 拖到 8.0"
    assert c0["sample_scores"] == [9.0, 8.0]


def test_collapse_samples_keeps_case_when_all_samples_empty():
    """该用例全部采样都空 → 保留（宁可报低分，也不能让用例凭空消失）。"""
    evals = [
        {"test_case_index": 0, "weighted_score": 1.0, "issues": ["目标模型调用失败：empty_output"]},
        {"test_case_index": 0, "weighted_score": 1.0, "issues": ["目标模型调用失败：empty_output"]},
    ]
    out = _collapse_samples(evals)
    assert len(out) == 1, "全空时用例仍需出现（否则报告会少一条，更危险）"
    assert out[0]["n_samples"] == 2
    assert out[0]["weighted_score"] == 1.0


def test_is_empty_output_eval_detection():
    """空输出标记靠 issues 里的 empty_output 识别（由 execute 节点写入 error）。"""
    assert _is_empty_output_eval({"issues": ["目标模型调用失败：empty_output"]})
    assert not _is_empty_output_eval({"issues": ["普通问题"]})
    assert not _is_empty_output_eval({})
    assert not _is_empty_output_eval({"issues": []})


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
    # 双向评：两序一致才计胜负，所以位置偏置计数必须是 0
    assert pw["position_flips"] == 0
    assert all(d["forward"] == d["swapped"] == "better" for d in pw["details"])


def test_position_biased_judge_is_neutralised(monkeypatch):
    """只会选 A 侧的评委（纯位置偏置）必须被双向评打成全平，而不是白送 6 胜。

    旧协议（随机决定谁放 A、只判一次）对这类评委毫无办法：它的偏向会被
    "运气好的那半"直接计进胜负。两序对照才是可观测的偏置。
    """
    from pm.nodes import compare_node

    monkeypatch.setenv("PM_PAIRWISE", "1")

    def always_a(role, model_cls, system, user, max_retries=3, overrides=None):
        return (
            PreferenceResult(winner="A", reason="A 更完整", decisive=True),
            {"model": "f", "channel": "fake", "attempts": 1, "latency_ms": 1},
        )

    st = initial_state(task="t", target_model="fake", n_test_cases=3)
    st["run_id"] = "bias"
    st["test_cases"] = [f"i{i}" for i in range(3)]
    st["test_runs"] = [
        {
            "test_case_index": i,
            "sample_index": 0,
            "test_input": f"i{i}",
            "prompt": "p",
            "output": "X",
        }
        for i in range(3)
    ]
    st["baseline_runs"] = [
        {
            "test_case_index": i,
            "sample_index": 0,
            "test_input": f"i{i}",
            "prompt": "t",
            "output": "Y",
        }
        for i in range(3)
    ]
    with backend.use(backend.CallHook(structured=always_a, disable_cache=True)):
        out = compare_node(st)  # type: ignore[arg-type]
    pw = out["pairwise"]
    assert pw["votes"] == {"better": 0, "worse": 0, "tie": 3}
    assert pw["position_flips"] == 3
    assert all(d["position_flip"] and d["forward"] != d["swapped"] for d in pw["details"])


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


def test_empty_target_output_is_marked_not_silently_scored(monkeypatch):
    """target 返回空内容时必须标记 error，不能当成"1 分的真实结果"。

    实测特征（矩阵 A）：不抛异常、latency 58~71s、len=0，
    根因是 reasoning 吃满 max_tokens（对照：4000 → 0/3 非空，8000 → 3/3）。
    """
    import pm.llm as L
    import pm.nodes.execute as EX

    # 让 plain_call 返回空内容（不抛异常，正是真实故障形态）
    monkeypatch.setattr(L, "plain_call", lambda *a, **k: ("", {"model": "m", "latency_ms": 60000}))
    monkeypatch.setattr(EX.llm, "plain_call", L.plain_call)
    monkeypatch.setattr(EX, "target_cache", lambda: None)

    run = EX._run_one_target(
        idx=0,
        case="测试输入",
        prompt="测试提示词",
        target_model="m",
        expected="",
        assert_mode="",
        sample=0,
    )
    assert run.output == ""
    assert run.error == "empty_output", "空输出必须被标记，否则会被静默计入均分"


# ---------------------------------------------------------------------------
# 评委复现性进不确定度（PM_JUDGE_JITTER）
# ---------------------------------------------------------------------------
def test_measurement_noise_combines_in_quadrature(monkeypatch):
    import math

    from pm import schemas

    monkeypatch.setattr(schemas, "JUDGE_JITTER", 0.0)
    assert schemas.measurement_noise(1.0) == 1.0, "未填抖动时完全等于旧口径"
    assert schemas.measurement_noise(None) is None, "噪声不可估的语义不许被改写"

    monkeypatch.setattr(schemas, "JUDGE_JITTER", 2.0)
    assert schemas.measurement_noise(None) == 2.0, "只有评委抖动时也进带"
    got = schemas.measurement_noise(1.5)
    assert got == round(math.hypot(1.5, 2.0), 3)
    assert got > max(1.5, 2.0), "合成必须比任何单独一项更宽（保守方向）"


def test_effective_gate_never_relaxes_and_rises_with_jitter(monkeypatch):
    import math

    from pm import schemas

    monkeypatch.setenv("PM_JUDGE_DISAGREEMENT", "2.0")
    monkeypatch.setattr(schemas, "JUDGE_JITTER", 0.0)
    assert schemas.effective_disagreement_threshold() == 2.0

    monkeypatch.setattr(schemas, "JUDGE_JITTER", 2.6)
    gate = schemas.effective_disagreement_threshold()
    # 2·√2·极差/d2(3)：两位各自抖 2.6 的评委，其差要越过这条线才算真分歧
    assert gate > 2.6 and abs(gate - round(2.0 * math.sqrt(2.0) * 2.6 / 1.693, 2)) < 0.01

    # 抖动很小时不许把已配置的线放松
    monkeypatch.setenv("PM_JUDGE_DISAGREEMENT", "9.0")
    monkeypatch.setattr(schemas, "JUDGE_JITTER", 0.3)
    assert schemas.effective_disagreement_threshold() == 9.0


def test_ci_lower_widens_with_judge_jitter(monkeypatch):
    from pm import schemas

    evals = [_ev(8.0, spread=0.4), _ev(8.4, spread=0.6), _ev(7.6, spread=0.2)]
    monkeypatch.setattr(schemas, "JUDGE_JITTER", 0.0)
    tight = AggregateScore.from_evaluations(evals, n_expected=3)
    monkeypatch.setattr(schemas, "JUDGE_JITTER", 2.6)
    wide = AggregateScore.from_evaluations(evals, n_expected=3)

    assert wide.ci_lower < tight.ci_lower, "评委抖动必须让下界更保守"
    assert wide.noise_total > wide.noise and wide.judge_jitter == 2.6
    assert tight.noise_total == tight.noise, "未测抖动时不引入任何新数值"


def test_plateau_margin_uses_the_combined_noise(monkeypatch):
    """回退/平台期余量必须看合成噪声：否则"评委这次手抖"会被当成版本回退而提前停。"""
    from pm import schemas

    monkeypatch.setattr(schemas, "JUDGE_JITTER", 0.0)
    assert schemas.noise_margin(schemas.measurement_noise(0.5)) == 1.0
    monkeypatch.setattr(schemas, "JUDGE_JITTER", 2.6)
    combined = schemas.measurement_noise(0.5)
    assert schemas.noise_margin(combined) > 1.0
