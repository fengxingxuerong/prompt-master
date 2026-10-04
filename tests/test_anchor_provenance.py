"""锚点人工分的**出处**必须机读，且 bias 能按出处拆开。

背景（2026-10-03）：交付报告里最承重的一句是"64 条人工锚点、聚合 bias +3.09（95% CI +2.36~+3.82）"。
但 48 条候选里 25 条不是产品所有者打的，是 AI 按所有者已确认判例外推的（GLM-5.3，与评委 B
glm-5.2 同族）。这件事此前只写在提交说明 `8af8c3b` 和 `score_form.csv` 的备注栏里，
锚点 JSON 里**没有出处位** ⇒ 那句 bias 拆不出"严格人工"那一半，读者会把家族一致性误读成与人的偏差。

补完之后实测（`run.py calibrate --aggregate`，纯账本重算、零调用）：
所有者亲判 41 条 bias **+2.90** ／ AI 代判 23 条 bias **+3.67** —— 两个数都在 CI 宽度内，
所以"评委偏松"这个结论不靠代判那半撑着。这条文件钉的就是这个可拆性本身。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pm import calibration as C

REPO = Path(__file__).resolve().parents[1]


def test_classify_human_source_table() -> None:
    assert (
        C.classify_human_source({"provenance": {"human_score_source": "owner"}})
        == C.HUMAN_SOURCE_OWNER
    )
    assert (
        C.classify_human_source({"provenance": {"human_score_source": "ai_proxy_glm-5.3"}})
        == C.HUMAN_SOURCE_AI_PROXY
    )
    # 中文备注也算代判（历史数据里有这种写法）
    assert C.classify_human_source({"provenance": "AI代判"}) == C.HUMAN_SOURCE_AI_PROXY
    # 关键一条：没标注一律 unknown，**不许默认成 owner**
    assert C.classify_human_source({"human_score": 7}) == C.HUMAN_SOURCE_UNKNOWN
    assert C.classify_human_source({"provenance": {}}) == C.HUMAN_SOURCE_UNKNOWN


def test_split_is_pure_arithmetic() -> None:
    pairs = [
        {"id": "a", "human": 5.0, "judge": 8.0},
        {"id": "b", "human": 6.0, "judge": 7.0},
        {"id": "c", "human": 4.0, "judge": 9.0},
    ]
    sources = {"a": "owner", "b": "owner", "c": "ai_proxy_glm-5.3"}
    out = C.split_by_human_source(
        pairs,
        {
            k: C.classify_human_source({"provenance": {"human_score_source": v}})
            for k, v in sources.items()
        },
    )
    assert out[C.HUMAN_SOURCE_OWNER]["n_pairs"] == 2
    assert out[C.HUMAN_SOURCE_OWNER]["bias"] == pytest.approx(2.0)
    assert out[C.HUMAN_SOURCE_AI_PROXY]["bias"] == pytest.approx(5.0)
    # 分组的 bias 各自算，合并值不等于任何一组——这正是拆分的目的
    assert out[C.HUMAN_SOURCE_OWNER]["n_anchors"] == 2


def test_split_reports_unknown_instead_of_dropping() -> None:
    """账本里的 id 在锚点文件中查不到时归 unknown，不能悄悄消失（那会缩小分母）。"""
    pairs = [{"id": "ghost", "human": 5.0, "judge": 8.0}]
    out = C.split_by_human_source(pairs, {})
    assert out[C.HUMAN_SOURCE_UNKNOWN]["n_pairs"] == 1


def test_real_anchors_carry_source_for_every_scored_row() -> None:
    """真实锚点集：凡 `confirmed=true 且有 human_score` 的都必须带出处位。

    这是把"披露只存在于提交说明里"变成数据事实的那一条。
    """
    for name in ("samples.json", "samples.candidates.json", "samples_disputed.json"):
        rows = json.loads((REPO / "judge_calibration" / name).read_text(encoding="utf-8"))
        missing = [
            r.get("id")
            for r in rows
            if isinstance(r, dict)
            and r.get("id")
            and r.get("human_score") is not None
            and C.classify_human_source(r) == C.HUMAN_SOURCE_UNKNOWN
        ]
        assert not missing, f"{name} 有 {len(missing)} 条已打分却没标出处：{missing[:5]}"


def test_proxy_count_agrees_with_score_form() -> None:
    """漂移守卫：锚点 JSON 里的代判条数必须等于打分表备注里的「AI代判」条数。

    两处是不同时间、不同写入者产生的同一件事——一旦对不上，说明有人改了其一，
    那么报告里按出处拆分出来的两个 bias 就都不可信了。
    """
    import csv

    form = (REPO / "judge_calibration" / "score_form.csv").read_text(encoding="utf-8-sig")
    in_form = {
        str(r["id"]).strip()
        for r in csv.DictReader(form.splitlines())
        if str(r.get("备注") or "").startswith("AI代判")
    }
    rows = json.loads(
        (REPO / "judge_calibration" / "samples.candidates.json").read_text(encoding="utf-8")
    )
    in_json = {
        str(r["id"])
        for r in rows
        if isinstance(r, dict)
        and r.get("id")
        and C.classify_human_source(r) == C.HUMAN_SOURCE_AI_PROXY
    }
    assert in_json == in_form, (
        f"两处代判集合不一致：只在表里{sorted(in_form - in_json)[:3]} 只在 JSON{sorted(in_json - in_form)[:3]}"
    )
    assert len(in_json) == 25, "代判条数与提交说明 8af8c3b 记录的 25 条对不上，先查是谁改的"


def test_pooled_split_on_the_live_ledger_is_zero_call() -> None:
    """真账本 + 真锚点 → pooled 里必须带出处拆分，且各分组锚点数之和 == 合并锚点数。

    这条同时钉住"拆分不额外调用模型"：`aggregate_history` 只吃已传入的 history。
    """
    path = REPO / "logs" / "judge_calibration_history.json"
    if not path.exists():
        pytest.skip("本机没有校准账本（新检出）")
    history = json.loads(path.read_text(encoding="utf-8"))
    agg = C.aggregate_history(history, mode="impression", judge="evaluator")
    if not agg:
        pytest.skip("账本里没有 impression/evaluator 的带明细轮次")
    pooled = agg["pooled"]
    groups = pooled.get("by_human_source") or {}
    assert groups, "pooled 里没有出处拆分 ⇒ 接线断了"
    total = sum(int(g.get("n_anchors") or 0) for g in groups.values())
    assert total == pooled["n_anchors"], (
        f"分组锚点数 {total} 与合并 {pooled['n_anchors']} 不等——有 id 被吞了"
    )
    assert pooled["owner_anchors"] > 0, "严格人工那一半必须存在，否则 bias 整句都是家族一致性"


def test_render_split_names_both_numbers(capsys: pytest.CaptureFixture[str]) -> None:
    pooled = {
        "by_human_source": {
            C.HUMAN_SOURCE_OWNER: {"n_anchors": 41, "n_pairs": 72, "bias": 2.9, "mae": 3.0},
            C.HUMAN_SOURCE_AI_PROXY: {"n_anchors": 23, "n_pairs": 23, "bias": 3.67, "mae": 3.7},
        },
        "n_anchors": 64,
    }
    lines = C.render_human_source_split(pooled)
    text = "\n".join(lines)
    assert "+2.90" in text and "+3.67" in text, text
    assert "家族一致性" not in text, "代判不过半时不该喊同源——那是把警示用废"


def test_render_split_warns_when_proxy_majority() -> None:
    pooled = {
        "by_human_source": {
            C.HUMAN_SOURCE_OWNER: {"n_anchors": 3, "n_pairs": 3, "bias": 1.0, "mae": 1.0},
            C.HUMAN_SOURCE_AI_PROXY: {"n_anchors": 20, "n_pairs": 20, "bias": 3.0, "mae": 3.0},
        },
        "n_anchors": 23,
    }
    text = "\n".join(C.render_human_source_split(pooled))
    assert "家族一致性" in text, text


def test_render_split_is_silent_when_nothing_to_split() -> None:
    """只有一组有数据就没得拆，硬写一行"其中 X 条是代判"反而像在有话。"""
    pooled = {
        "by_human_source": {C.HUMAN_SOURCE_OWNER: {"n_anchors": 9, "n_pairs": 9, "bias": 1.0}}
    }
    assert C.render_human_source_split(pooled) == []


def test_report_disclosure_names_the_split_only_when_it_matters() -> None:
    from pm.report import _human_source_note

    strong = {
        "by_human_source": {
            C.HUMAN_SOURCE_OWNER: {"n_anchors": 3, "n_pairs": 3, "bias": 1.2},
            C.HUMAN_SOURCE_AI_PROXY: {"n_anchors": 20, "n_pairs": 20, "bias": 3.4},
        }
    }
    note = _human_source_note(strong)
    assert "20" in note and "AI" in note and "+1.20" in note, note
    # 现状（代判不过半）不该改动那句已经被人读的披露行的长度
    weak = {
        "by_human_source": {
            C.HUMAN_SOURCE_OWNER: {"n_anchors": 41, "n_pairs": 72, "bias": 2.9},
            C.HUMAN_SOURCE_AI_PROXY: {"n_anchors": 23, "n_pairs": 23, "bias": 3.67},
        }
    }
    assert _human_source_note(weak) == ""
    assert _human_source_note({}) == ""


def test_migration_script_is_idempotent_on_a_copy(tmp_path: Path) -> None:
    """重跑 `mark_anchor_provenance.stamp` 不产生第二次改动 ⇒ 它不是每次重写都搅一遍尺子。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "mark_anchor_provenance", REPO / "scripts" / "mark_anchor_provenance.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    rows = [
        {"id": "h-1", "human_score": 7, "confirmed": True},
        {"id": "h-2", "human_score": 2, "confirmed": True},
        {"id": "h-3", "human_score": None, "confirmed": False},
    ]
    first = mod.stamp(rows, {"h-2"})
    snap = json.dumps(rows, ensure_ascii=False, sort_keys=True)
    second = mod.stamp(rows, {"h-2"})
    assert json.dumps(rows, ensure_ascii=False, sort_keys=True) == snap, "第二次跑改动了数据"
    assert first == second
    assert C.classify_human_source(rows[0]) == C.HUMAN_SOURCE_OWNER
    assert C.classify_human_source(rows[1]) == C.HUMAN_SOURCE_AI_PROXY
    # 没打分的条目不许被盖成"人打的"
    assert C.classify_human_source(rows[2]) == C.HUMAN_SOURCE_UNKNOWN
    assert "provenance" not in rows[2], "不该留一个空的 provenance 对象当噪声"
