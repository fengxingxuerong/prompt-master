"""判定式评分协议（PM_SCORING_MODE=checklist）的纯代码回归。

分数由代码从二值判定算出来，所以这套映射必须确定性可测——不接任何 LLM。
每条断言都问同一个问题：**什么输入会让它红**。只测"全满足→高分"是不够的，
那和只测 happy path 的旧评估器一样；真正咬人的是漏答、漏报无来源数字、
清单薄到没东西可查这三种"看起来没事其实把分数发出去了"的形态。
"""

from __future__ import annotations

import pytest
from pm import scoring as S
from pm.schemas import (
    ChecklistEvaluation,
    CheckVerdict,
    DimensionScores,
    EvaluationResult,
    UnsourcedClaim,
)

_CHECKLIST = [
    {"item": "[约束] 每个结论必须附具体数值", "bucket": "constraint"},
    {"item": "[约束] 缺失必须标注「数据缺失」", "bucket": "constraint"},
    {"item": "[输出格式] Markdown 表格 + 3 条结论", "bucket": "format"},
    {"item": "[任务] 1. 趋势结论", "bucket": "deliverable"},
    {"item": "通用：核心交付物是否真的给出", "bucket": "deliverable"},
]


def _ev(
    items: dict[str, bool], *, unsourced: list[UnsourcedClaim] | None = None, band: int = 4
) -> ChecklistEvaluation:
    """按 `items` 里的映射出判定；没出现在 `items` 里的清单条目视为"评委没答"。"""
    verdicts = [CheckVerdict(item=k, satisfied=v, evidence="x") for k, v in items.items()]
    return ChecklistEvaluation(verdicts=verdicts, unsourced=unsourced or [], quality_band=band)


def _all_ok(*, unsourced: list[UnsourcedClaim] | None = None, band: int = 4) -> ChecklistEvaluation:
    return _ev({r["item"]: True for r in _CHECKLIST}, unsourced=unsourced, band=band)


# --------------------------------------------------------------------------
# 清单构造
# --------------------------------------------------------------------------
def test_build_checklist_extracts_sections_and_injects_generics() -> None:
    prompt = "[任务]\n1. 给出趋势结论\n[约束]\n  - 不得编造数值\n[输出格式]\n1. Markdown 表格"
    cl = S.build_checklist(prompt)
    items = {r["item"]: r["bucket"] for r in cl}
    assert any("不得编造数值" in k and v == "constraint" for k, v in items.items())
    assert any("Markdown 表格" in k and v == "format" for k, v in items.items())
    # 破折号条目也要收：判定式多问一个是非题没有代价，漏掉一半约束才有
    assert any(k.startswith("[约束] 不得编造数值") for k in items)
    generics = [r for r in cl if r["item"].startswith("通用：")]
    assert len(generics) == 3, "注入项保证基线臂与最终臂至少在同一批问题上可比"


def test_build_checklist_keeps_same_text_from_different_sections() -> None:
    """段名不同就视为不同条目是有意为之：核对结果要能回到提示词的哪一段。

    反过来说，一份把同一条约束抄在两段的提示词会吃到两次违规计数——这也不是 bug，
    重复段落本身就是被 `pm.quality` 记过账的真实缺陷（[关键约束] 与 [约束] 大面积语义重复）。
    """
    prompt = "[约束]\n1. 不得编造\n[规则]\n1. 不得编造"
    texts = [r["item"] for r in S.build_checklist(prompt)]
    assert len(texts) == 5  # 2 条提示词条目 + 3 条注入项
    assert sum(1 for t in texts if t.startswith("通用：")) == 3


# --------------------------------------------------------------------------
# 分数映射
# --------------------------------------------------------------------------
def test_all_satisfied_scores_in_the_top_band() -> None:
    dims, detail = S.score_checklist(_all_ok(), _CHECKLIST)
    assert dims.constraint_compliance == 9.5 and dims.format_adherence == 9.5
    assert detail["violations"] == [] and detail["caps"] == []


def test_violation_steps_follow_the_rubric_bands() -> None:
    """台阶位置对齐 rubric 自己的区间：1 条仍在 8 档、2 条掉到 7 档（不可用）。"""
    cl = [{"item": f"[约束] 第{i}条", "bucket": "constraint"} for i in range(1, 5)]
    for n, expect in ((1, 8.0), (2, 6.5), (3, 5.0), (4, 3.0)):
        items = {r["item"]: i >= n for i, r in enumerate(cl)}  # 前 n 条判不满足
        dims, detail = S.score_checklist(_ev(items), cl)
        assert len(detail["violations"]) == n
        assert dims.constraint_compliance == expect, f"{n} 条违规应为 {expect}"
    # 第 5 条违规（超出台阶表）落到底档，不会变成负分
    dims, _ = S.score_checklist(_ev({r["item"]: False for r in cl}), cl)
    assert dims.constraint_compliance == 3.0


def test_unanswered_item_counts_as_violation() -> None:
    """评委少答一条不该是免费的——否则"回避问题"会稳定抬高分数。"""
    items = {r["item"]: True for r in _CHECKLIST}
    verdicts = [
        CheckVerdict(item=k, satisfied=v, evidence="x")
        for k, v in items.items()
        if k != "[约束] 缺失必须标注「数据缺失」"
    ]
    ev = ChecklistEvaluation(verdicts=verdicts, unsourced=[], quality_band=4)
    dims, detail = S.score_checklist(ev, _CHECKLIST)
    assert detail["unanswered"] == ["[约束] 缺失必须标注「数据缺失」"]
    assert dims.constraint_compliance == 8.0


def test_text_paraphrase_still_matches() -> None:
    """评委照抄条目时会改空格/加句号，回配必须不敏感，否则"答了"被当成"没答"。"""
    items = {r["item"] + "。": True for r in _CHECKLIST}
    verdicts = [
        CheckVerdict(item=r["item"] + " 。", satisfied=True, evidence="x") for r in _CHECKLIST
    ]
    ev = ChecklistEvaluation(verdicts=verdicts, unsourced=[], quality_band=4)
    dims, detail = S.score_checklist(ev, _CHECKLIST)
    assert detail["unanswered"] == [] and detail["violations"] == []
    assert dims.constraint_compliance == 9.5
    assert items


# --------------------------------------------------------------------------
# 事实性封顶：这次改动真正想修的那一条
# --------------------------------------------------------------------------
def test_unexplained_unsourced_number_caps_total_and_fails() -> None:
    """实测那条漏放（人工 7.0 / 印象式评委 8.6）：数字无来源又不解释 → 封顶 6.0 且不过线。

    印象式下"总分别超过 6.0"只是写在 rubric 里的一句话，评委记得就封、不记得就不封；
    这里它由代码执行。
    """
    dims, detail = S.score_checklist(_all_ok(), _CHECKLIST, unsourced_candidates=["22.4%"])
    assert detail["unsourced_unexplained"] == ["22.4%"]
    assert detail["caps"] == [6.0]
    weighted, passed = S.apply_caps(dims, detail)
    assert weighted == 6.0 and passed is False


def test_explained_derivation_is_not_penalised() -> None:
    """派生值完全合法（占比/环比就是要把输入里的两个数算成新数），别把它当编造。"""
    ev = _all_ok(
        unsourced=[UnsourcedClaim(claim="环比 22.4%", basis="(120-98)/98=22.4%，由输入两值相除")]
    )
    dims, detail = S.score_checklist(ev, _CHECKLIST, unsourced_candidates=["22.4"])
    assert detail["caps"] == [] and detail["unsourced_unexplained"] == []
    assert dims.robustness == 9.5


def test_placeholder_basis_counts_as_fabrication() -> None:
    """ "不知道/无/数据缺失"这类解释等于没解释。"""
    ev = _all_ok(unsourced=[UnsourcedClaim(claim="均值 893.33", basis="无")])
    _dims, detail = S.score_checklist(ev, _CHECKLIST, unsourced_candidates=["893.33"])
    assert detail["caps"] == [6.0]


def test_fabrication_lands_on_constraint_and_robustness_together() -> None:
    """rubric 明写两维同档，避免"一边 3 一边 8"的自相矛盾形态。"""
    dims, _detail = S.score_checklist(_all_ok(), _CHECKLIST, unsourced_candidates=["7.8"])
    assert dims.constraint_compliance == dims.robustness
    assert dims.format_adherence == 9.5, "格式确实全对——编造不该把格式一起抹掉"


# --------------------------------------------------------------------------
# 适用性门禁与协议开关
# --------------------------------------------------------------------------
def test_checklist_usable_rejects_thin_prompts() -> None:
    """基线臂（原始需求直喂）没有标签段：清单只剩注入项时不许走判定式，
    否则空桶拿 9.5 会把基线抬高、Δ 就测不出优化有没有变好。"""
    assert S.checklist_usable(S.build_checklist("帮我分析销售数据")) is False
    assert S.checklist_usable(S.build_checklist("[约束]\n1. 不得编造\n[输出格式]\n1. 表格")) is True


def test_merge_takes_union_of_violations_not_average() -> None:
    """A 漏判、B 判到的那条必须仍然算违规。

    取维度分平均会把它稀释成 8.75 继续放行 —— 而双评委交叉验证要的就是
    "任一评委抓到就算抓到"。
    """
    cl = [{"item": "[约束] 不得编造", "bucket": "constraint"}]
    dims_ok = DimensionScores(
        task_completion=9.5,
        format_adherence=9.5,
        constraint_compliance=9.5,
        robustness=9.5,
        quality=8.5,
    )
    dims_viol = dims_ok.model_copy(update={"constraint_compliance": 8.0})
    a = EvaluationResult(
        dimension_scores=dims_ok,
        model_reported_score=9.35,
        judge="evaluator",
        scoring_mode="checklist",
        checklist_detail={
            "violations": [],
            "unanswered": [],
            "unsourced_unexplained": [],
            "caps": [],
        },
    ).finalize()
    b = EvaluationResult(
        dimension_scores=dims_viol,
        model_reported_score=8.97,
        judge="evaluator_b",
        scoring_mode="checklist",
        checklist_detail={
            "violations": ["[约束] 不得编造"],
            "unanswered": [],
            "unsourced_unexplained": [],
            "caps": [],
        },
    ).finalize()
    merged = S.merge_checklist_results(a, b, cl)
    assert merged.checklist_detail["violations"] == ["[约束] 不得编造"]
    assert merged.dimension_scores.constraint_compliance == 8.0
    assert merged.weighted_score == 8.97, "必须等于 B 的分，而不是 (9.35+8.97)/2=9.16"
    assert merged.judge == "merged"


def test_merge_union_of_unsourced_keeps_the_cap() -> None:
    """只有一侧抓到无来源数字时，封顶也必须保留。"""
    cl = [{"item": "[约束] 不得编造", "bucket": "constraint"}]
    base = EvaluationResult(
        dimension_scores=DimensionScores(
            task_completion=9.5,
            format_adherence=9.5,
            constraint_compliance=9.5,
            robustness=9.5,
            quality=9.5,
        ),
        model_reported_score=9.5,
        judge="evaluator",
        checklist_detail={"violations": [], "caps": []},
    ).finalize()
    flagged = base.model_copy(
        update={
            "judge": "evaluator_b",
            "checklist_detail": {
                "violations": [],
                "caps": [6.0],
                "unsourced_unexplained": ["22.4"],
            },
        }
    )
    merged = S.merge_checklist_results(base, flagged, cl)
    assert merged.checklist_detail["caps"] == [6.0]
    assert merged.weighted_score == 6.0 and merged.passed is False


def test_merge_quality_takes_the_lower_band() -> None:
    cl = [{"item": "[约束] a", "bucket": "constraint"}]
    hi = EvaluationResult(
        dimension_scores=DimensionScores(
            task_completion=9.5,
            format_adherence=9.5,
            constraint_compliance=9.5,
            robustness=9.5,
            quality=9.5,
        ),
        model_reported_score=9.5,
        judge="evaluator",
        checklist_detail={},
    ).finalize()
    lo = hi.model_copy(
        update={
            "judge": "evaluator_b",
            "dimension_scores": hi.dimension_scores.model_copy(update={"quality": 6.5}),
        }
    )
    assert S.merge_checklist_results(hi, lo, cl).dimension_scores.quality == 6.5


def test_scoring_mode_defaults_to_impression(monkeypatch: pytest.MonkeyPatch) -> None:
    """缺省必须是旧行为：这一步改动在测量出结果之前不能影响主管道。"""
    monkeypatch.delenv("PM_SCORING_MODE", raising=False)
    assert S.scoring_mode() == "impression"
    monkeypatch.setenv("PM_SCORING_MODE", "checklist")
    assert S.scoring_mode() == "checklist"
    monkeypatch.setenv("PM_SCORING_MODE", "cheklist")  # 打错字不回退到判定式
    assert S.scoring_mode() == "impression"


def test_unsourced_numbers_rule_scope() -> None:
    """整数只收 ≥100：1~99 大多是序号与条目计数，而实测编造形态都是大数。"""
    nums = S.unsourced_numbers(
        "华东 120 万，华南 98 万",
        "3. 华东 1,200,000 元居首，环比 22.4%，Q1 合计 275 万，两条结论",
    )
    assert "22.4" in nums, "比率最常被编造"
    assert "1200000" in nums, "千分位归一后按大数收录（旧写法会把它切成 1,200 + ,000）"
    assert "275" in nums, "「合计 275 万」就是 anchor-fab-trend 那条编造形态"
    assert "3" not in nums and "2" not in nums, "序号不报"
    assert "120" not in nums and "98" not in nums, "输入里有的不报"
