"""盲评冲突自动归因（风格统计）回归。

历史真实运行发现 comparator 偏好「保守、逐项枚举缺失」的输出，与 pointwise
偏好具体产出冲突时只能"请人工裁定"。归因机制用确定性风格统计给裁定一个起点：
- 保守枚举标记（数据缺失/未提供等）计数
- 具体数值引用（带万/千/亿/% 的数字）计数
- 三种归因假设：优化版更保守（疑似真回退）/ 基线更保守（评委偏好假设）/ 无法区分
"""

from __future__ import annotations

from pm.nodes.baseline import _conflict_attribution, _style_stats
from pm.report import render_report


def test_style_stats_counts_markers_and_numbers():
    text = "- 华东销售额最高（120 万）\n- 西南数据缺失，无法判断趋势\n- 同比增长 15%"
    s = _style_stats(text)
    assert s["conservative_markers"] == 2  # 数据缺失 + 无法判断
    assert s["data_points"] == 2  # 120 万、15%（"最高"无单位不计）
    assert s["chars"] == len(text)


def test_attribution_baseline_conservative_hypothesis():
    """基线全是枚举缺失、优化版全是具体产出 → 评委偏好假设。"""
    base = {
        0: {"output": "数据缺失：无输入数据。无法判断趋势。信息不足，无法确定异常点。"},
        1: {"output": "数据缺失：未提供字段。"},
    }
    cur = {
        0: {"output": "华东 120 万领跑，华南 98 万次之，建议加大华东投入。"},
        1: {"output": "华北 45 万，占三区合计的 23%。"},
    }
    a = _conflict_attribution(base, cur)
    assert "评委偏好保守逐项枚举" in a["hypothesis"]
    assert a["base"]["conservative_markers"] >= 4
    assert a["cur"]["conservative_markers"] == 0
    assert a["cur"]["data_points"] >= 3


def test_attribution_cur_more_conservative_is_regression_signal():
    """优化版比基线更保守 → 冲突可能是真实回退，必须警告而非甩给偏好假设。"""
    base = {0: {"output": "华东 120 万，华南 98 万，两区差距明显，建议关注。"}}
    cur = {0: {"output": "数据缺失：无输入数据。信息不足，无法判断。"}}
    a = _conflict_attribution(base, cur)
    assert "更保守" in a["hypothesis"]
    assert "真实质量回退" in a["hypothesis"]


def test_attribution_indistinguishable():
    base = {0: {"output": "华东 120 万，华南 98 万。"}}
    cur = {0: {"output": "华东 120 万，华南 98 万。"}}
    a = _conflict_attribution(base, cur)
    assert "无法区分" in a["hypothesis"]


def test_report_renders_attribution_on_conflict():
    """pointwise 达标 + 盲评判基线胜 → 冲突段出现风格归因统计。"""
    state = {
        "run_id": "r-attr",
        "status": "passed",
        "iteration": 1,
        "max_iterations": 2,
        "target_model": "m",
        "llm_calls": 10,
        "aggregate": {
            "avg_score": 8.5,
            "min_score": 8.0,
            "max_score": 9.0,
            "n_cases": 2,
            "n_cases_expected": 2,
            "cases_complete": True,
            "n_passed": 2,
            "passed": True,
            "ci_lower": 8.0,
            "n_samples": 1,
            "sem": 0.0,
            "noise": 0.0,
            "unstable_cases": [],
            "judge_bias": 0.0,
            "judge_bias_warning": False,
            "n_assertions": 0,
            "n_assertions_failed": 0,
            "all_issues": [],
        },
        "prompt_versions": [{"iteration": 0, "prompt": "P0", "note": "初版"}],
        "test_runs": [],
        # 仲裁段要求 Δ 与盲评方向相反：基线 6.0 < 优化 8.5（Δ>0）而盲评判基线胜
        "baseline_aggregate": {
            "avg_score": 6.0,
            "min_score": 5.5,
            "max_score": 6.5,
            "n_cases": 2,
            "n_cases_expected": 2,
            "cases_complete": True,
            "n_passed": 0,
            "passed": False,
            "ci_lower": 5.5,
            "n_samples": 1,
            "sem": 0.0,
            "noise": 0.0,
            "unstable_cases": [],
            "judge_bias": 0.0,
            "judge_bias_warning": False,
            "n_assertions": 0,
            "n_assertions_failed": 0,
            "all_issues": [],
        },
        "pairwise": {
            "verdict": "worse",
            "votes": {"better": 0, "worse": 3, "tie": 0},
            "n_compared": 3,
            "conflict": "pointwise 判达标，但成对盲评多数倾向基线更好",
            "attribution": {
                "base": {"chars": 500, "conservative_markers": 6, "data_points": 2},
                "cur": {"chars": 620, "conservative_markers": 1, "data_points": 9},
                "hypothesis": "与「评委偏好保守逐项枚举」假设一致：建议人工以产出价值优先裁定",
            },
            "details": [],
        },
    }
    text, _ = render_report(state)
    assert "风格归因（自动统计，供裁定参考）" in text
    assert "基线 6 处 vs 优化版 1 处" in text
    assert "评委偏好保守逐项枚举" in text


def test_report_no_attribution_without_conflict():
    """无冲突时不得渲染归因段（归因只在冲突时计算）。"""
    state = {
        "run_id": "r-plain",
        "status": "passed",
        "iteration": 0,
        "max_iterations": 1,
        "target_model": "m",
        "llm_calls": 5,
        "aggregate": None,
        "prompt_versions": [{"iteration": 0, "prompt": "P0", "note": "初版"}],
        "test_runs": [],
        "pairwise": {
            "verdict": "better",
            "votes": {"better": 2, "worse": 0, "tie": 0},
            "n_compared": 2,
            "conflict": "",
            "attribution": {"base": {}, "cur": {}, "hypothesis": "不该出现"},
            "details": [],
        },
    }
    text, _ = render_report(state)
    assert "风格归因" not in text
    assert "不该出现" not in text
