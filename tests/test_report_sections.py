"""`pm/report_sections.py` 的分支级专项守卫（2026-10-23 补）。

为什么单独立这个文件：report_sections.py 490 语句此前没有专属用例，覆盖只靠
报告相关用例顺带跑（88%），「原稿体检」段与冲突章的风格归因渲染从未被直接
钉住。本轮修「结论句事实错误」时顺带收口——渲染层每个分支都要有对应用例。

被钉住的分支（每条都对着一次真实缺陷或一次实测空白）：
1. 结论行四种未达标措辞——2026-10-23 的真 bug：均分 8.5 过线却印「< 阈值 8.0」；
2. 下界过容忍线时旧版会追加「也没过线」的反话（7.2 ≥ 7 也说没过）；
3. 头部披露假后端演示轮与 memory 参考注入（此前报告与真跑长得一模一样）；
4. 评分协议行（纯印象式 / 纯判定式 / 同轮混用——judge 的回退只写过 stderr）；
5. 「原稿体检」两分支（有命中 / 零命中）与字符数对照行；
6. 冲突章的风格归因三行（此前 0 覆盖，坏了整段静默消失）。
"""

from __future__ import annotations

from typing import Any

from pm.report import render_report
from pm.report_sections import _verdict_head


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
        "assertion_veto": False,
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
        ],
        "test_runs": [],
    }
    base.update(over)
    return base


def _head(text: str) -> str:
    """结论 + 头部（到「评分总览」为止）。"""
    return text.split("## 评分总览", 1)[0]


def _eval(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "test_case_index": 0,
        "weighted_score": 7.0,
        "model_reported_score": 7.0,
        "dimension_scores": {},
        "scoring_mode": "impression",
    }
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# 1. 结论行：未达标的措辞必须与挡路的闸一致
# ---------------------------------------------------------------------------


def test_verdict_avg_above_threshold_does_not_claim_below() -> None:
    """2026-10-23 真 bug 回归：均分 8.5 已过线、卡在短板容忍带，
    旧版结论行印「均分 8.5 < 阈值 8.0」——事实错误，而这行是读者唯一必读的。"""
    agg = _agg(avg_score=8.5, min_score=6.5, ci_lower=6.24, passed=False)
    head = _head(render_report(_state(aggregate=agg))[0])
    assert "未达标" in head
    assert "8.5 < 阈值" not in head, "均分已过线，不许再印「< 阈值」"
    assert "8.5" in head, "均分本身还是要写出来"
    assert "已达阈值" in head
    assert "6.24" in head and "6.5" in head, "挡路的两个读数都要点名"
    assert "容忍线" in head


def test_verdict_head_names_the_tolerance_line() -> None:
    """容忍线要写出数（阈值 − 1），并带出两个挡路读数的顺序（下界在前）。"""
    head = _verdict_head(_agg(avg_score=8.5, min_score=6.5, ci_lower=6.24, passed=False))
    assert "低于容忍线 7（阈值 − 1）" in head
    assert "保守下界 6.24 与 最低分 6.5" in head


def test_verdict_avg_below_threshold_keeps_classic_sentence() -> None:
    """均分真的没过线时，经典措辞照旧（含阈值与下界，读者可自查）。"""
    agg = _agg(avg_score=7.33, min_score=6.0, ci_lower=6.68, passed=False)
    head = _head(render_report(_state(aggregate=agg))[0])
    assert "均分 7.33 < 阈值 8.0" in head
    assert "6.68" in head


def test_verdict_no_false_shortfall_claim_when_gates_pass() -> None:
    """第二个反话回归：均分 7.8 未过线，但下界 7.2/最低分 7.5 都在容忍带内，
    旧版会追加「保守下界 7.2 也没过线」——它恰恰过了。"""
    agg = _agg(avg_score=7.8, min_score=7.5, ci_lower=7.2, passed=False)
    head = _head(render_report(_state(aggregate=agg))[0])
    assert "均分 7.8 < 阈值 8.0" in head
    assert "也没过线" not in head
    assert "低于容忍线" not in head, "下界与最低分都过容忍线，不该被点名"


def test_verdict_assertion_veto_is_the_named_reason() -> None:
    """分数全过线、断言一票否决：结论行必须说是断言，而不是含糊的「未达标」。"""
    agg = _agg(
        avg_score=9.0,
        min_score=9.0,
        max_score=9.0,
        ci_lower=8.8,
        n_assertions=2,
        n_assertions_failed=1,
        assertion_veto=True,
        passed=False,
    )
    head = _head(render_report(_state(aggregate=agg))[0])
    assert "事实断言未全过（1/2），一票否决" in head
    assert "已达阈值" in head


def test_verdict_incomplete_cases_is_the_named_reason() -> None:
    """用例数不足（C1）也要在结论行点名——它和分数高低无关，修分数修不好它。"""
    agg = _agg(
        avg_score=9.0,
        min_score=9.0,
        max_score=9.0,
        ci_lower=8.8,
        n_cases=2,
        n_cases_expected=4,
        cases_complete=False,
        passed=False,
    )
    head = _head(render_report(_state(aggregate=agg))[0])
    assert "用例数不完整（2/4）" in head
    assert "不予判定达标" in head


def test_verdict_passed_head_unchanged() -> None:
    """达标分支的措辞保持原样（不引入新的夸口）。"""
    assert _verdict_head(_agg(avg_score=9.0, passed=True)) == "✅ **达标**：均分 9.0 ≥ 阈值 8.0"


# ---------------------------------------------------------------------------
# 2. 头部披露：假后端演示轮 / memory 参考注入
# ---------------------------------------------------------------------------


def test_demo_run_is_disclosed_in_header() -> None:
    """假后端轮的报告必须自带「不是真实读数」——history/library 按 demo 剔除它，
    报告此前却长得和真跑一模一样，「达标 8.x」会被原样截图引用。"""
    state = _state(trace=[{"event": "optimize_done", "channel": "fake"}])
    head = _head(render_report(state)[0])
    assert "假后端（PM_FAKE_BACKEND）" in head
    assert "不是真实评测读数" in head


def test_real_run_has_no_demo_disclosure() -> None:
    """真跑（channel=plain 等）不许出现演示字样——狼来了的警告等于没有警告。"""
    state = _state(trace=[{"event": "optimize_done", "channel": "plain"}])
    assert "假后端" not in render_report(state)[0]


def test_memory_hint_is_disclosed_in_header() -> None:
    """memory 注入是隐藏输入：同一 task 在有/无历史资产的机器上产出不同，
    不披露就等于让读者以为产物只由本轮输入决定。"""
    state = _state(
        trace=[{"event": "optimize_done", "memory_hint": {"run_id": "run_x", "similarity": 0.83}}]
    )
    head = _head(render_report(state)[0])
    assert "优化参考" in head
    assert "`run_x`" in head
    assert "0.83" in head


def test_no_memory_hint_no_disclosure_line() -> None:
    assert "优化参考" not in render_report(_state())[0]


# ---------------------------------------------------------------------------
# 3. 评分协议行：纯印象式 / 纯判定式 / 同轮混用
# ---------------------------------------------------------------------------


def test_protocol_line_defaults_to_impression() -> None:
    text = render_report(_state(evaluations=[_eval(), _eval(scoring_mode="impression")]))[0]
    assert "评分协议：印象式" in text
    assert "混用" not in text


def test_protocol_line_pure_checklist() -> None:
    text = render_report(_state(evaluations=[_eval(scoring_mode="checklist")]))[0]
    assert "评分协议：判定式" in text
    assert "混用" not in text


def test_protocol_line_flags_mixed_rounds() -> None:
    """判定式清单太薄时逐例回退印象式（judge 只写过 stderr warning）：
    同轮两种口径的平均必须在报告里可见，否则 Δ 读不出东西也没人知道为什么。"""
    text = render_report(
        _state(
            evaluations=[
                _eval(test_case_index=0, scoring_mode="checklist"),
                _eval(test_case_index=1, scoring_mode="checklist"),
                _eval(test_case_index=2, scoring_mode="impression"),
            ]
        )
    )[0]
    assert "评分协议混用：2 例判定式／1 例印象式" in text
    assert "两种口径的平均" in text


def test_protocol_line_absent_without_evaluations() -> None:
    assert "评分协议" not in render_report(_state())[0]


# ---------------------------------------------------------------------------
# 4. 原稿体检（_seed_lines）：有命中 / 零命中 / 无原稿
# ---------------------------------------------------------------------------


def _seeded(**over: Any) -> dict[str, Any]:
    base = _state(
        seed_prompt="ABCD",
        prompt_versions=[
            {"iteration": 0, "prompt": "P0", "avg_score": 7.0, "min_score": 6.0, "note": "改进版"}
        ],
    )
    base.update(over)
    return base


def test_seed_section_lists_findings() -> None:
    text = render_report(_seeded(seed_quality_findings=["[约束] 编号条目超载：12 条"]))[0]
    assert "## 原稿体检（代码侧确定性规则，未跑模型）" in text
    assert "命中 1 条规则" in text
    assert "[约束] 编号条目超载：12 条" in text


def test_seed_section_zero_findings_branch() -> None:
    """零命中分支必须说明「不等于原稿够用」——否则读者把 0 命中当成体检通过。"""
    text = render_report(_seeded())[0]
    assert "原稿未命中任何确定性规则" in text
    assert "不等于原稿已经够用" in text


def test_seed_section_character_accounting() -> None:
    """字符对照行：4 字符原稿 → 2 字符交付版（数字取自 state，可核）。"""
    text = render_report(_seeded(seed_quality_findings=["x"]))[0]
    assert "原稿 4 字符 → 交付版 2 字符" in text


def test_seed_section_absent_without_seed() -> None:
    assert "## 原稿体检" not in render_report(_state())[0]


# ---------------------------------------------------------------------------
# 5. 冲突章的风格归因渲染（此前 0 覆盖）
# ---------------------------------------------------------------------------


def _conflict_state() -> dict[str, Any]:
    return _state(
        aggregate=_agg(avg_score=8.67, min_score=7.45),
        baseline_aggregate=_agg(avg_score=7.54, min_score=3.35),
        pairwise={
            "verdict": "worse",
            "votes": {"better": 0, "worse": 4, "tie": 0},
            "n_compared": 4,
            "attribution": {
                "hypothesis": "优化版更保守、枚举更少",
                "base": {"conservative_markers": 3, "data_points": 1},
                "cur": {"conservative_markers": 0, "data_points": 5},
            },
        },
    )


def test_conflict_section_renders_style_attribution() -> None:
    text = render_report(_conflict_state())[0]
    assert "## ⚠️ 结论冲突（需人工裁定）" in text
    assert "风格归因（自动统计，供裁定参考）" in text
    assert "保守枚举标记（数据缺失/未提供等）：基线 3 处 vs 优化版 0 处" in text
    assert "具体数值引用：基线 1 处 vs 优化版 5 处" in text
    assert "归因假设：优化版更保守、枚举更少" in text


def test_no_attribution_renders_no_attribution_block() -> None:
    state = _conflict_state()
    state["pairwise"] = {k: v for k, v in state["pairwise"].items() if k != "attribution"}
    text = render_report(state)[0]
    assert "结论冲突" in text  # 冲突本体照常
    assert "风格归因" not in text  # 没有归因数据就不许印那一块
