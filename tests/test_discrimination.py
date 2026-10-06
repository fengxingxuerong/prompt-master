"""判别力与可估性（discrimination_stats / render_discrimination）的回归测试。零 LLM、零出网。

被测的承诺（竞品那一层"先证明量具有信息量，再谈优化"的做法，本仓库此前没有）：
1. **AUC 只跟 0.5 比，而且比的是重抽 CI**：点值 0.6 在 22 条考卷上离 0.5 不到一个 CI，
   拿它说"评委有判别力"或"评委没判别力"都是自欺。
2. **任何"调阈值有效"的说法必须先赢过平凡基线**：一律判多数那一侧就有 93% 正确率
   （2026-09-29 那轮 n=45、人工达标只有 3 条）。同批拟合的最优阈值恒 ≥ 基线，
   所以只有留一折的增益算数。
3. **阈值不许逃到可表达区间之外**：cut 取到 max 以上等于"什么都不判达标"，
   那是一条输出不是判据，会被误读成"评委被校准好了"。
4. **格子没填满就如实说答不了**：κ 的 95% CI 半宽超过门槛时报"不可估"，
   不再印一个 κ=0.026 让人以为量具坏了（也可能只是考卷没有正例）。
"""

from __future__ import annotations

import json
import math
from typing import Any

from pm.calibration import (
    DISCRIMINATION_SEED,
    KAPPA_CI_HALF_WIDTH_MAX,
    _auc_vs_label,
    _best_cut,
    _cuts_in_range,
    _loo_cut_accuracy,
    aggregate_history,
    analyze,
    discrimination_stats,
    render_aggregate,
    render_discrimination,
    render_report,
)

LINE = 8.0


def _sep(n: int = 10) -> tuple[list[float], list[float]]:
    """完全可分：达标的人工分对应评委 9.0+，不达标对应 7.0-。"""
    humans = [9.0] * n + [4.0] * n
    judges = [9.4] * n + [6.2] * n
    return humans, judges


# ---------------------------------------------------------------- 1. AUC 本体


def test_auc_is_one_when_perfectly_separable() -> None:
    humans, judges = _sep()
    assert _auc_vs_label(judges, [h >= LINE for h in humans]) == 1.0


def test_auc_is_zero_when_perfectly_reversed() -> None:
    humans, judges = _sep()
    flipped = [10.0 - j for j in judges]
    assert _auc_vs_label(flipped, [h >= LINE for h in humans]) == 0.0


def test_auc_counts_ties_as_half() -> None:
    # 全部并列时既不是 0 也不是 1，而是 0.5 —— 这是 AUC 退化成"没有信息"的定义性检查
    assert _auc_vs_label([7.0] * 6, [True] * 3 + [False] * 3) == 0.5


def test_auc_none_when_a_label_side_is_empty() -> None:
    """一侧为空不是"判别力差"，是"根本没测"——必须返回 None 而不是 0.0。"""
    assert _auc_vs_label([9.0, 9.2], [True, True]) is None


# ---------------------------------------------------------- 2/3. 阈值扫描的陷阱


def test_cut_scan_stays_inside_observable_range() -> None:
    judges = [6.0, 7.0, 8.5]
    cuts = _cuts_in_range(judges)
    assert min(cuts) <= min(judges) + 1e-9
    assert max(cuts) >= max(judges)  # 需要能表达"刚好把最高那条切掉"
    assert max(cuts) <= max(judges) + 0.1  # 但不许跑到 max 之上变成"全部判不达标"


def test_best_cut_cannot_reach_the_trivial_always_fail_solution() -> None:
    """22 条里只有 1 条人工达标、评委给这一条 7.9：
    允许 cut>max 就能"零漏放 100% 正确"，而那等于宣布什么都不上线。"""
    judges = [7.9] + [8.6] * 21
    humans = [9.0] + [3.0] * 21
    cut, acc = _best_cut(judges, [h >= LINE for h in humans])
    assert cut is not None and cut <= max(judges) + 1e-9
    assert acc < 1.0, "同批拟合的正确率不该出现 100%"


def test_loo_never_exceeds_in_sample_bound() -> None:
    humans, judges = _sep()
    labels = [h >= LINE for h in humans]
    _, bound = _best_cut(judges, labels)
    assert _loo_cut_accuracy(judges, labels) <= bound + 1e-9


def test_loo_returns_none_when_it_would_be_meaningless() -> None:
    assert _loo_cut_accuracy([9.0, 6.0], [True, False]) is None


# ---------------------------------------------------------------- 4. 聚合读数


def test_real_exam_shape_reports_negative_gain_instead_of_ninety_percent() -> None:
    """复刻 2026-09-29 那轮的形状（n=22、人工达标 1 条、评委几乎全给 8.5+）。

    那一轮"同批最优阈值正确率 93%"看起来像调阈值调出来了，实际是一律判不达标。
    这条断言钉的就是：报告给的净增益必须是**负或零**，不是 +0.044 那种上界幻觉。
    """
    humans = [9.0] + [3.0] * 21
    judges = [7.6] + [8.7] * 21
    d = discrimination_stats(humans, judges, LINE, reps=200, seed=DISCRIMINATION_SEED)
    assert d["n_human_pass"] == 1
    assert math.isclose(d["trivial_accuracy"], 21 / 22, abs_tol=0.001)
    assert d["loo_gain_vs_trivial"] is not None and d["loo_gain_vs_trivial"] <= 0
    # 留一折必须严格低于同批拟合的上界，否则"留一折"只是换了个名字的同批拟合
    assert d["loo_accuracy"] < d["best_accuracy"]
    assert d["estimable"] is False, "1 个正例的考卷不许把 κ 当结论"


def test_ci_is_deterministic_for_the_same_pairs() -> None:
    humans, judges = _sep(6)
    a = discrimination_stats(humans, judges, LINE, reps=300, seed=DISCRIMINATION_SEED)
    b = discrimination_stats(humans, judges, LINE, reps=300, seed=DISCRIMINATION_SEED)
    assert a["auc_ci95"] == b["auc_ci95"]
    assert a["kappa_ci95"] == b["kappa_ci95"]


def test_ci_declared_skipped_below_two_pairs() -> None:
    d = discrimination_stats([9.0], [9.3], LINE, reps=100, seed=1)
    assert "ci_skipped" in d
    assert d["auc"] is None  # 单条没有可分的两类
    assert d["estimable"] is False


def test_perfect_judge_is_estimable_and_distinguishable() -> None:
    """反向守卫：把防线拆了会红，但真好的量具也不该被门槛一起压成"不可估"。"""
    humans, judges = _sep(14)
    d = discrimination_stats(humans, judges, LINE, reps=400, seed=DISCRIMINATION_SEED)
    assert d["auc"] == 1.0
    assert d["auc_distinguishable_from_chance"] is True
    assert d["loo_gain_vs_trivial"] is not None and d["loo_gain_vs_trivial"] > 0
    assert d["estimable"] is True


# ------------------------------------------------------------------ 5. 渲染口径


def _render(d: dict[str, Any]) -> str:
    return "\n".join(render_discrimination(d))


def test_render_says_not_estimable_rather_than_a_bare_kappa() -> None:
    humans = [9.0] + [3.0] * 21
    judges = [7.6] + [8.7] * 21
    d = discrimination_stats(humans, judges, LINE, reps=200, seed=3)
    # 1 个正例时 κ 照样"有定义"（重抽里判定线两侧并不总为空），那个数读的是考卷设计
    assert d["min_cell"] == 1
    assert d["estimable"] is False
    txt = _render(d)
    assert "答不了「达标一致性」" in txt
    assert "留一折" in txt and "平凡基线" in txt


def test_render_blocks_verdict_on_wide_ci_even_with_fat_cells() -> None:
    """格子够厚、但这次读数的 CI 本身就宽 → 第二条门槛也要能拦。
    直接喂构造给渲染器：真实数据要同时满足"四格 ≥10"和"半宽 >0.3"，
    那种样本是凑出来的假象，测渲染分支不该依赖它。"""
    txt = _render(
        {
            "n": 28,
            "line": 8.0,
            "n_human_pass": 14,
            "n_judge_pass": 11,
            "min_cell": 11,
            "auc": 0.72,
            "auc_ci95": [0.58, 0.86],
            "auc_distinguishable_from_chance": True,
            "trivial_accuracy": 0.5,
            "best_accuracy": 0.75,
            "loo_accuracy": 0.71,
            "loo_gain_vs_trivial": 0.21,
            "kappa_ci_half_width": 0.42,
            "estimable": False,
        }
    )
    assert "半宽 0.42" in txt
    assert str(KAPPA_CI_HALF_WIDTH_MAX) in txt
    assert "答不了" in txt
    assert "AUC=0.72" in txt and "显著高于瞎猜" in txt


def test_render_does_not_claim_auc_verdict_when_ci_covers_chance() -> None:
    """构造一个"点值看着不像瞎猜但 CI 覆盖 0.5"的例子：3 条达标 / 7 条不达标，
    评委分几乎不随标签变化（点值 AUC≈0.45）。这种读数只能报"与随机猜不可区分"。"""
    humans = [9.0, 9.0, 9.0, 3.0, 3.0, 3.0, 3.0, 3.0, 3.0, 3.0]
    judges = [8.6, 8.4, 8.5, 8.7, 8.3, 8.8, 8.2, 8.9, 8.1, 8.5]
    d = discrimination_stats(humans, judges, LINE, reps=200, seed=DISCRIMINATION_SEED)
    assert d["auc"] is not None and 0.3 < d["auc"] < 0.7
    txt = _render(d)
    assert "与随机猜不可区分" in txt
    assert "换量具或补人工标签" in txt


def test_render_empty_for_missing_block() -> None:
    assert render_discrimination({}) == []
    assert render_discrimination({"n": 0}) == []


# --------------------------------------------------------------- 6. 入口接线（不是只测被调函数）


def test_analyze_and_report_wire_the_block_end_to_end() -> None:
    """只测 arithmetic 函数会漏掉"analyze 没挂上 / 报告没渲染"这一类接线缺陷。
    2026-10 那轮空产出封顶就是"三处接线对不上"，这里同源防一遍。"""
    humans = [9.0, 8.5, 3.0, 4.0, 5.0, 2.0, 7.0, 6.5, 4.5, 3.5, 9.5, 2.5]
    judges = [9.2, 8.8, 3.4, 4.6, 5.5, 2.2, 7.4, 6.9, 4.9, 3.9, 9.6, 2.8]
    samples = [
        {"id": f"a{i}", "human_score": h, "test_output": "有实质内容的输出文本"}
        for i, h in enumerate(humans)
    ]
    a = analyze(samples, judges)
    disc = a["discrimination"]
    assert disc["n"] == len(samples)
    assert disc["auc"] == 1.0
    txt = render_report("evaluator", a)
    assert "- 判别力：AUC=1.0" in txt
    assert "可估性：" in txt
    assert json.dumps(a, ensure_ascii=False, default=str)  # 报告要能进 --json，不许有序列化炸弹


# ------------------------------------------------- 7. 聚合侧同口径（README 引用的那一行）


def _rounds(n_per: int = 6, rounds: int = 4) -> list[dict[str, Any]]:
    """同一批锚点跑 N 轮：配对之间**不独立**，重抽单位必须是锚点。"""
    recs: list[dict[str, Any]] = []
    for r in range(rounds):
        items = []
        for i in range(n_per):
            human = 9.0 if i < n_per // 2 else 3.0
            judge = human + (0.4 if i % 3 else -1.2)
            items.append({"id": f"a{i}", "human": human, "judge": round(judge, 2), "delta": 0.0})
        recs.append(
            {
                "ts": f"2026-10-0{r + 1} 03:0{i % 9}:00",
                "judge": "evaluator",
                "mode": "impression",
                "n": len(items),
                "mae": 0.8,
                "bias": 0.3,
                "decision_agree": 0.5,
                "decision_kappa": 0.1,
                "items": items,
            }
        )
    return recs


def test_clustered_resample_is_wider_than_pairwise() -> None:
    """同一锚点跨轮重复时，按配对独立重抽会假装样本很多、CI 假窄。

    这条钉的是"聚合侧必须按锚点聚类"这个**口径选择**本身——它是 §十四 当初
    给 MAE 单列聚类自助法的同一个理由，判别力量具不能例外。
    """
    items = _rounds()[0]["items"]
    humans = [float(x["human"]) for x in items for _ in range(3)]
    judges = [float(x["judge"]) for x in items for _ in range(3)]
    groups = [str(x["id"]) for x in items for _ in range(3)]
    pairwise = discrimination_stats(humans, judges, LINE, reps=400, seed=5)
    clustered = discrimination_stats(humans, judges, LINE, reps=400, seed=5, group_keys=groups)
    assert pairwise["kappa_ci_half_width"] is not None
    assert clustered["kappa_ci_half_width"] is not None
    assert pairwise["kappa_ci_half_width"] <= clustered["kappa_ci_half_width"], (
        f"配对 {pairwise['kappa_ci_half_width']} vs 聚类 {clustered['kappa_ci_half_width']}"
    )


def test_aggregate_history_carries_the_gate_into_the_rendered_block() -> None:
    """最被人引用的那行是**聚合**读数（README §三 抄的就是它）——
    门槛只挂在单轮报告上等于没上。"""
    agg = aggregate_history(_rounds(), mode="impression", judge="evaluator", bootstrap=200)
    assert agg is not None
    disc = agg["pooled"]["discrimination"]
    assert disc["n"] == agg["pooled"]["n_pairs"]
    assert isinstance(disc["estimable"], bool)
    # 口径必须真的是"按锚点聚类"：同参数复算要逐位相等，退成按配对重抽就得红
    items = [x for r in _rounds() for x in r["items"]]
    hu = [float(x["human"]) for x in items]
    ju = [float(x["judge"]) for x in items]
    clustered = discrimination_stats(hu, ju, LINE, group_keys=[str(x["id"]) for x in items])
    pairwise = discrimination_stats(hu, ju, LINE)
    assert disc["kappa_ci_half_width"] == clustered["kappa_ci_half_width"]
    assert disc["kappa_ci_half_width"] > pairwise["kappa_ci_half_width"], (
        f"聚类 {disc['kappa_ci_half_width']} 应严格宽于按配对重抽 {pairwise['kappa_ci_half_width']}，"
        "否则这条断言根本区分不了两种口径"
    )
    txt = render_aggregate(agg)
    assert "判别力" in txt
    assert "可估性" in txt


def test_cli_ledger_records_the_gate_alongside_kappa() -> None:
    """账本是唯一活过一轮跑批的地方。只落 κ 不落"当时能不能读"，
    下一轮换考卷就查不回来 —— 所以按源码同源钉：这四个字段必须**读自 discrimination**，
    而不是在 CLI 里另编一套数（与本仓库"出处字面量跨 judge/report 一致"同一手法）。"""
    import re
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "pm" / "cli" / "calibrate.py").read_text(
        encoding="utf-8"
    )
    for key in ("auc", "loo_gain_vs_trivial", "min_cell", "decision_estimable"):
        pat = re.compile(rf'"{key}": \(analysis\.get\("discrimination"\) or \{{}}\)\.get\(')
        assert len(pat.findall(src)) == 1, f"账本字段 {key} 没有读自 discrimination"
