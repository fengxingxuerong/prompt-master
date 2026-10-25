"""交付报告的「一句话结论」守卫（2026-10-22）。

用户视角（P2-9）：判定与小字警告散在中段，读者要翻到第三屏才知道
"未达标 / 这个 Δ 不可采信"。`_verdict_lines` 把它们搬到报告最前。

三条纪律在这里被钉住：
1. **位置**：结论必须在「评分总览」之前（在头部元信息之后）；
2. **不粉饰**：未达标就说未达标，并把阈值与保守下界一起写出来；
3. **警告只在真的成立时出现**，且不产生新判据（数字与其它章节同源）。
"""

from __future__ import annotations

from typing import Any

from pm.report import render_report


def _agg(**over: Any) -> dict[str, Any]:
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


def _state(**over: Any) -> dict[str, Any]:
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


def _head(text: str) -> str:
    """结论 + 它前面的头部（到「评分总览」为止）。"""
    return text.split("## 评分总览", 1)[0]


def test_verdict_comes_before_the_score_overview() -> None:
    text, _ = render_report(_state())
    head = _head(text)
    assert "## 结论（先看这一行）" in head, "结论必须在「评分总览」之前"
    # 头部身份信息仍在结论之前
    assert head.index("run_id") < head.index("## 结论（先看这一行）")


def test_undelivered_verdict_states_the_numbers() -> None:
    text, _ = render_report(_state(aggregate=_agg(avg_score=7.33, ci_lower=6.68, passed=False)))
    head = _head(text)
    assert "未达标" in head
    assert "7.33" in head and "8.0" in head, "要把均分与阈值一起写出来"
    assert "6.68" in head, "保守下界也要写（判定看的是它）"


def test_passed_verdict_says_passed() -> None:
    text, _ = render_report(
        _state(status="passed", aggregate=_agg(avg_score=9.0, ci_lower=8.6, passed=True))
    )
    assert "达标" in _head(text)
    assert "未达标" not in _head(text)
    assert "| 判定 | 通过（≥8.0）" in text, "无门禁拦截时总览仍应判通过"


def test_archive_without_threshold_discloses_that_the_number_is_current_config() -> None:
    """老归档没有冻住的阈值时，必须说清那个数是"当前配置"而不是当轮判定依据。

    实测缺陷（2026-10-31 审计）：`passed` 是计算时用当时的 `PM_PASS_THRESHOLD` 定死的，
    而报告在渲染时再读一次环境变量。`run_0792cd9acf75`（当时阈值 8.5、avg 8.25
    ⇒ passed=False）按后来的默认 8.0 渲染时，总览每道闸都过、首行却写"未达标"，
    报告解释不了自己的判定；反过来会印出"✅ 达标：均分 7.8 ≥ 阈值 8.0"这种假命题。
    """
    text, _ = render_report(_state(status="passed", aggregate=_agg(avg_score=9.0, passed=True)))
    assert "本轮归档未记录阈值" in text, "没说明这个 8.0 是现在读的配置"


def test_frozen_threshold_wins_over_the_current_env() -> None:
    """归档里冻住了阈值（8.5）时，渲染必须用它，而不是当前默认 8.0。"""
    agg = _agg(avg_score=8.25, ci_lower=8.25, passed=False)
    agg["threshold"] = 8.5
    text, _ = render_report(_state(aggregate=agg))
    head = _head(text)
    assert "8.5" in head, f"用了当前配置的阈值而不是当轮的：{head}"
    assert "阈值 8.0" not in head


def test_failed_run_says_there_is_nothing_to_deliver() -> None:
    text, _ = render_report(_state(status="failed", aggregate={}))
    assert "本轮失败" in _head(text)


def test_injection_gate_blocks_passed_headline() -> None:
    """分数过线但注入被劫持：首行必须说未达标，不能与文末门禁结论打架。

    真实跑批（2026-10-24，run_422320d9e62e）暴露：agg.passed=True、status 正确
    落到 max_iterations，但首行仍印「✅ 达标」，文末却写「注入门禁已拦截」。
    """
    state = _state(
        status="max_iterations",
        aggregate=_agg(avg_score=8.24, min_score=8.07, ci_lower=8.0, passed=True),
        injection_gate={"blocked": True, "total": 1, "hijacked": 1},
    )
    text, _ = render_report(state)
    head = _head(text)
    assert "✅ **达标**" not in head
    assert "未达标" in head
    assert "注入门禁" in head and "1/1" in head
    assert "| 判定 | 未达标" in text, "总览的判定行也不能与首行打架"


def test_missing_baseline_is_flagged_up_front() -> None:
    """没有基线读数 = Δ 不可采信 —— 这必须在首屏就说，不能只写在中段。"""
    text, _ = render_report(_state())
    head = _head(text)
    assert "没有基线读数" in head
    assert "比不优化好多少" in head


def test_judge_seat_absent_is_flagged_up_front() -> None:
    state = _state(
        judge_health={"panel_size": 2},
        errors=["evaluate#0[evaluator_b] 墙钟耗尽", "evaluate#1[evaluator_b] 墙钟耗尽"],
    )
    head = _head(render_report(state)[0])
    assert "评委席降级" in head
    assert "2/3" in head, "要给出「几条用例有缺席」，而不是只说一句「降级」"


def test_single_sample_noise_is_flagged_up_front() -> None:
    head = _head(render_report(_state(aggregate=_agg(n_samples=1)))[0])
    assert "噪声不可估" in head


def test_clean_run_has_no_caveat_block() -> None:
    """都正常时不许堆警告（否则"狼来了"，真警告就没人看了）。"""
    state = _state(baseline_aggregate=_agg(avg_score=6.5))
    head = _head(render_report(state)[0])
    assert "先知道的几件事" not in head
