"""`pm/report.py` 的回归：报告是从状态字典渲染出来的纯函数，可以直接喂 dict 断言措辞。

这些用例锁定的是"不许粉饰"那部分：基线缺失要写出来、Δ ≤ 0 要警告、
下界而不是点估计、断言与噪声要进报告。渲染逻辑原先埋在 nodes.py 里时是没法这样单测的。
"""

from __future__ import annotations

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


def test_unstable_cases_and_judge_bias_are_surfaced():
    state = _state(aggregate=_agg(unstable_cases=[0, 2], judge_bias=1.4, judge_bias_warning=True))
    text, _ = render_report(state)
    assert "#0, #2" in text
    assert "评委在放水" in text


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
                },
            }
        ]
    )
    text, _ = render_report(state)
    assert "## 事实断言（ground-truth 校验）" in text
    assert "❌ 未通过" in text and "缺少「数据缺失」标注" in text


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
