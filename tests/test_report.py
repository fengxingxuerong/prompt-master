"""`pm/report.py` 的回归：报告是从状态字典渲染出来的纯函数，可以直接喂 dict 断言措辞。

这些用例锁定的是"不许粉饰"那部分：基线缺失要写出来、Δ ≤ 0 要警告、
下界而不是点估计、断言与噪声要进报告。渲染逻辑原先埋在 nodes.py 里时是没法这样单测的。
"""

from __future__ import annotations

import pytest
from pm.report import pick_best, render_report


def _agg(**over):
    base = {
        "avg_score": 7.0,
        "min_score": 6.0,
        "max_score": 8.0,
        "n_cases": 3,
        "n_cases_expected": 3,
        "cases_complete": True,
        "n_passed": 0,
        "passed": False,
        "ci_lower": 6.5,
        "n_samples": 2,
        "sem": 0.2,
        "noise": 0.3,
        "unstable_cases": [],
        "judge_bias": 0.0,
        "judge_bias_warning": False,
        "n_assertions": 0,
        "n_assertions_failed": 0,
        "all_issues": [],
    }
    base.update(over)
    return base


def _state(**over):
    base = {
        "run_id": "r-1",
        "status": "max_iterations",
        "iteration": 1,
        "max_iterations": 3,
        "target_model": "some-model",
        "llm_calls": 12,
        "aggregate": _agg(),
        "prompt_versions": [
            {"iteration": 0, "prompt": "P0", "avg_score": 7.0, "min_score": 6.0, "note": "初版"},
            {"iteration": 1, "prompt": "P1", "avg_score": 6.4, "min_score": 6.0, "note": "修订"},
        ],
        "test_runs": [],
    }
    base.update(over)
    return base


def test_verdict_conflict_is_surfaced_when_signals_disagree():
    """均分说变好（Δ>0）但盲评说基线胜 → 必须显式提示冲突，不能让读者自己猜。"""
    state = _state(
        aggregate=_agg(avg_score=8.67, min_score=7.45),
        baseline_aggregate=_agg(avg_score=7.54, min_score=3.35),
        pairwise={
            "verdict": "worse",
            "votes": {"better": 0, "worse": 4, "tie": 0},
            "n_compared": 4,
        },
    )
    text, _ = render_report(state)
    assert "结论冲突" in text
    assert "人工裁定" in text
    assert "Δ=+1.13" in text
    assert "事实断言" in text  # 建议动作里必须指向硬证据


def test_verdict_conflict_reverse_direction():
    """均分没提升（Δ≤0）但盲评说优化版胜 → 同样要提示。"""
    state = _state(
        aggregate=_agg(avg_score=6.0, min_score=5.0),
        baseline_aggregate=_agg(avg_score=7.0, min_score=6.0),
        pairwise={"verdict": "better", "votes": {"better": 3, "worse": 1}, "n_compared": 4},
    )
    text, _ = render_report(state)
    assert "结论冲突" in text


def test_no_conflict_when_signals_agree():
    """两个信号一致时不要虚报冲突（否则冲突提示会变成噪声）。"""
    state = _state(
        aggregate=_agg(avg_score=8.67, min_score=7.45),
        baseline_aggregate=_agg(avg_score=7.54, min_score=3.35),
        pairwise={"verdict": "better", "votes": {"better": 4, "worse": 0}, "n_compared": 4},
    )
    text, _ = render_report(state)
    assert "结论冲突" not in text


def test_judgement_uses_ci_lower_not_point_estimate():
    text, _ = render_report(_state())
    assert "均分保守下界：**6.5**（判定看这个而不是看点估计 7.0）" in text
    assert "未达标" in text


def test_missing_baseline_is_admitted_in_words():
    """没跑基线就必须写"无法回答比不优化好多少"，不能让报告看起来有对照。"""
    text, _ = render_report(_state())
    assert "## 与基线对比" in text
    assert "未跑基线" in text and "比不优化好多少" in text


def test_non_positive_delta_warns():
    state = _state(baseline_aggregate=_agg(avg_score=7.0, min_score=6.0))
    text, _ = render_report(state)
    assert "## 与基线对比（原始需求直喂 target，同口径采样与评分）" in text
    assert "没有正向提升" in text


def test_high_baseline_flags_no_discrimination():
    """用例区分度自检：基线（原始需求直喂）都接近满分 = 用例太简单，必须点名。"""
    state = _state(baseline_aggregate=_agg(avg_score=8.6, min_score=8.0))
    text, _ = render_report(state)
    assert "用例区分度不足" in text
    assert "8.6" in text
    assert "不能证明优化版相对更好" in text
    assert "case_templates" in text  # 给出对症去处


def test_low_baseline_does_not_flag_discrimination():
    """基线分数正常（低于阈值）时不误报区分度问题。"""
    state = _state(baseline_aggregate=_agg(avg_score=6.2, min_score=5.5))
    text, _ = render_report(state)
    assert "用例区分度不足" not in text


def test_unstable_cases_and_judge_bias_are_surfaced():
    state = _state(aggregate=_agg(unstable_cases=[0, 2], judge_bias=1.4, judge_bias_warning=True))
    text, _ = render_report(state)
    assert "#0, #2" in text
    assert "评委在放水" in text


# ---- 校准披露行（2026-09-29）：账本里量得的评委偏差必须折进报告读数语境 ----
# 依据 §十五：48 锚点基线 bias +3.64 坐实评委系统性偏松，裸读数会被误读。
# conftest 的 seal_calibration_ledger 已把 PM_LOG_DIR 钉进 tmp_path，所以
# "无账本→不披露"是默认态；有账本的用例自己往 tmp 里写 fixture。


def _write_ledger(monkeypatch: pytest.MonkeyPatch, tmp_path, items: list[dict]) -> None:
    """往被密封的产物目录里写一份只含一轮的校准账本。"""
    import json

    ledger = tmp_path / "judge_calibration_history.json"
    ledger.write_text(
        json.dumps([{"ts": "2026-09-29 10:00:00", "judge": "evaluator", "items": items}]),
        encoding="utf-8",
    )


def test_calibration_disclosure_folds_known_bias(monkeypatch, tmp_path):
    """bias ≥ 告警线：披露行必须给出折算读数，并声明裸判定不可采信。"""
    _write_ledger(
        monkeypatch,
        tmp_path,
        [{"id": f"a{i}", "human": 2.0, "judge": 6.0} for i in range(6)],  # bias = +4.0
    )
    text, _ = render_report(_state(aggregate=_agg(avg_score=8.2)))
    assert "评委校准披露" in text
    assert "系统性偏松" in text
    assert "折算约 **4.2**" in text, "8.2 − 4.0：不折算，读者会把 8.2 读成接近满分"
    assert "不可采信" in text, "达标判定行还在渲染，但必须声明它不可采信"


def test_calibration_disclosure_quiet_when_bias_small(monkeypatch, tmp_path):
    """bias < 告警线：弱措辞，不喊狼来了——未来换合格评委时报告措辞要跟得上。"""
    _write_ledger(
        monkeypatch,
        tmp_path,
        [{"id": f"a{i}", "human": 6.0, "judge": 6.4} for i in range(6)],  # bias = +0.4
    )
    text, _ = render_report(_state(aggregate=_agg(avg_score=8.2)))
    assert "可按面值理解" in text
    assert "不可采信" not in text


def test_calibration_disclosure_absent_without_ledger():
    """conftest 已密封 PM_LOG_DIR（tmp 里无账本）：不披露，渲染不炸。"""
    text, _ = render_report(_state())
    assert "评委校准披露" not in text


def test_cases_short_note_blocks_inflated_confidence():
    state = _state(aggregate=_agg(n_cases=1, n_cases_expected=8, cases_complete=False))
    text, _ = render_report(state)
    assert "不足：期望 8 条，本次不予判定达标" in text


def test_assertion_rows_are_rendered_as_hard_evidence():
    state = _state(
        test_runs=[
            {
                "test_case_index": 0,
                "assertion": {
                    "mode": "contains",
                    "passed": False,
                    "detail": "缺少「数据缺失」标注",
                    "expected": "缺失信息必须写明「数据缺失」",
                },
            }
        ]
    )
    text, _ = render_report(state)
    assert "## 事实断言（ground-truth 校验）" in text
    assert "❌ 未通过" in text and "缺少「数据缺失」标注" in text
    assert "缺失信息必须写明「数据缺失」" in text, "期望片段要进表，否则人无法复核判定"


def test_long_expected_is_clipped_so_the_table_does_not_break():
    state = _state(
        test_runs=[
            {
                "test_case_index": 1,
                "assertion": {"mode": "regex", "passed": True, "expected": "一" * 300},
            }
        ]
    )
    text, _ = render_report(state)
    row = next(ln for ln in text.splitlines() if ln.startswith("| case#1 |"))
    assert row.count("|") == 6, f"表格列数被撑坏了：{row}"


def test_pick_best_backtracks_to_historical_high():
    best, note = pick_best(_state())
    assert best["iteration"] == 0, "未达标时应交付历史最高分版本"
    assert "未达标，取历史最高分" in note


def test_pick_best_honest_about_no_score():
    state = _state(prompt_versions=[{"iteration": 0, "prompt": "P0"}], aggregate={})
    best, note = pick_best(state)
    assert best["prompt"] == "P0" and "未获得评分" in note
    empty, note2 = pick_best(_state(prompt_versions=[], aggregate={}))
    assert note2 == "无可用版本" and empty["prompt"] == ""
    assert "## 评分总览" in render_report(_state(prompt_versions=[], aggregate={}))[0]


def test_delta_marked_untrustworthy_when_arm_has_no_valid_samples():
    """整臂有"零有效样本"的用例时，Δ 必须被标注为不可采信（端点抖动会冒充质量差）。"""
    from pm.report import _infra_invalid_cases, render_report
    from pm.state import initial_state

    state = initial_state(task="t", target_model="fake", n_test_cases=3)
    state["test_runs"] = [
        {"test_case_index": 0, "sample_index": 0, "output": "", "error": "empty_output"},
        {"test_case_index": 0, "sample_index": 1, "output": "", "error": "empty_output"},
        {"test_case_index": 1, "sample_index": 0, "output": "正常输出", "error": None},
        {"test_case_index": 2, "sample_index": 0, "output": "", "error": "timeout"},
    ]
    assert _infra_invalid_cases(state) == 2  # case0 与 case2 没有任何有效样本
    state["prompt_versions"] = [{"iteration": 0, "prompt": "P", "note": ""}]
    state["aggregate"] = {
        "avg_score": 2.9,
        "min_score": 1.0,
        "max_score": 2.9,
        "n_cases": 3,
        "n_passed": 0,
        "passed": False,
        "ci_lower": 1.0,
        "sem": 1.9,
        "noise": 0.0,
        "n_samples": 2,
        "judge_bias": 0.0,
        "all_issues": [],
        "all_suggestions": [],
        "cases_complete": True,
        "n_cases_expected": 3,
    }
    state["baseline_aggregate"] = {
        "avg_score": 7.71,
        "min_score": 6.03,
        "max_score": 8.0,
        "n_cases": 3,
        "n_passed": 3,
        "passed": True,
        "ci_lower": 6.5,
    }
    text, _ = render_report(state)
    assert "本轮 Δ 不可采信" in text
    assert "2 条用例" in text


def test_self_report_equals_weighted_is_disclosed_as_inert():
    """自报分与代码加权分**完全相等**时，那行"平均偏差"就不算独立证据，必须说出来。

    实测 109 次原始评委调用：评委 A 80%、仲裁 88% 逐字复述加权结果——因为提示词给了
    权重、示例示范怎么求加权，而该字段排在维度分之后生成。
    """
    ev = [
        {"test_case_index": 0, "model_reported_score": 8.0, "weighted_score": 8.0, "issues": ["x"]},
        {"test_case_index": 1, "model_reported_score": 7.5, "weighted_score": 9.0, "issues": ["y"]},
    ]
    text, _ = render_report(_state(evaluations=ev))
    assert "1/2 条自报分与代码加权分**完全相等**" in text
    assert "没放水" in text


def test_no_inertness_warning_when_self_reports_diverge():
    """两数真的分开时不许凭空印警告。"""
    ev = [
        {"test_case_index": 0, "model_reported_score": 6.0, "weighted_score": 9.0, "issues": ["x"]},
        {"test_case_index": 1, "model_reported_score": 7.5, "weighted_score": 9.0, "issues": ["y"]},
    ]
    text, _ = render_report(_state(evaluations=ev))
    assert "完全相等" not in text


def test_perfect_scores_with_issues_flagged_as_contradiction():
    """五维全 ≥9.5 却还列问题 = 自相矛盾，得在报告里点名。"""
    ev = [
        {
            "test_case_index": 2,
            "weighted_score": 9.8,
            "model_reported_score": 9.8,
            "dimension_scores": {
                "task_completion": 10,
                "format_adherence": 9.5,
                "constraint_compliance": 10,
                "robustness": 10,
                "quality": 10,
            },
            "issues": ["表格里 Q4 数字无来源"],
        }
    ]
    text, _ = render_report(_state(evaluations=ev))
    assert "五维全 ≥9.5 却仍列出了问题" in text and "#2" in text


def test_flawless_claim_is_surfaced_separately():
    """五维全 ≥9.5 且 issues 为空：是"无可指摘"的强声明，措辞与上面那种不同。"""
    ev = [
        {
            "test_case_index": 0,
            "weighted_score": 9.6,
            "model_reported_score": 9.0,
            "dimension_scores": {
                "task_completion": 9.5,
                "format_adherence": 9.5,
                "constraint_compliance": 9.5,
                "robustness": 9.5,
                "quality": 9.5,
            },
            "issues": [],
        }
    ]
    text, _ = render_report(_state(evaluations=ev))
    assert "宣称「无可指摘」" in text
    assert "却仍列出了问题" not in text


def test_normal_scores_trigger_neither_ceiling_warning():
    ev = [
        {
            "test_case_index": 0,
            "weighted_score": 7.9,
            "model_reported_score": 7.0,
            "dimension_scores": {
                "task_completion": 8,
                "format_adherence": 9,
                "constraint_compliance": 7,
                "robustness": 7,
                "quality": 8,
            },
            "issues": ["缺一个来源"],
        }
    ]
    text, _ = render_report(_state(evaluations=ev))
    assert "无可指摘" not in text and "却仍列出了问题" not in text


def test_arbitrated_cases_disclose_that_the_score_is_single_judge():
    """分差超阈值时采信的是第三方**一人**的分，报告必须说这不是双评委共识。

    真跑实测 9/26 轮走了仲裁（两个已完成 run 的冻结口径），比例不低；在此之前 n_arbitrated 只进 trace，
    交付报告里读者看到的仍是"双评委"口径的表。
    """
    state = _state(
        evaluations=[
            {"test_case_index": 0, "judge": "merged"},
            {"test_case_index": 1, "judge": "arbiter"},
            {"test_case_index": 2, "judge": "arbiter"},
            {"test_case_index": 3, "judge": "conservative"},
        ]
    )
    text, _ = render_report(state)
    assert "2/4 例出自**仲裁者一人**" in text
    assert "1/4 例因仲裁调用失败取了两评委较低分" in text
    assert "不是**双评委共识" in text


def test_no_provenance_line_when_every_case_is_a_consensus():
    """全走合并路径时不要凭空多一行警告（没有的事不许写）。"""
    state = _state(
        evaluations=[
            {"test_case_index": 0, "judge": "merged"},
            {"test_case_index": 1, "judge": "merged"},
        ]
    )
    text, _ = render_report(state)
    assert "分数出处" not in text


def test_report_shows_combined_noise_and_the_effective_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """填了评委抖动，报告必须说清"下界用的是合成噪声"和"仲裁触发线被抬到哪"。

    后半句尤其重要：抬线之后**双评委分歧会变少**，那是噪声感知的结果，
    不能让人误读成"交叉验证变强了"。
    """
    from pm import schemas

    for seat in ("PM_EVALUATOR_JITTER", "PM_EVALUATOR_B_JITTER", "PM_ARBITER_JITTER"):
        monkeypatch.delenv(seat, raising=False)
    monkeypatch.setattr(schemas, "JUDGE_JITTER", 2.6)
    st = _state(aggregate=_agg(noise=0.5, noise_total=2.65, judge_jitter=2.6, ci_lower=6.1))
    text, _ = render_report(st)
    assert "评委复现性抖动 2.6" in text and "合成噪声带 2.65" in text
    assert "仲裁有效触发线" in text and "不是交叉验证变强了" in text
    assert "两位座位各自的实测极差" not in text, "只有一个全局值时不许凭空造出按座位的读数"


def test_report_names_each_seat_when_their_jitter_differs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """按座位填了抖动，触发线那一行必须交代它用的是哪两个数——
    否则读者看到一个没填过的 3.18，会以为代码自己估了个新噪声。
    """
    from pm import schemas

    monkeypatch.setattr(schemas, "JUDGE_JITTER", 2.6)
    monkeypatch.setenv("PM_EVALUATOR_JITTER", "0.7")
    monkeypatch.setenv("PM_EVALUATOR_B_JITTER", "2.6")
    st = _state(aggregate=_agg(noise=0.5, noise_total=2.65, judge_jitter=2.6, ci_lower=6.1))
    text, _ = render_report(st)
    assert "两位座位各自的实测极差 0.7/2.6" in text


def test_report_stays_silent_about_jitter_when_it_was_never_measured() -> None:
    """没测过抖动就不许凭空造一个数出来（默认 0 = 与旧口径逐字一致）。"""
    text, _ = render_report(_state(aggregate=_agg(noise=0.5)))
    assert "评委复现性抖动" not in text and "仲裁有效触发线" not in text
