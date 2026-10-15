"""校准聚合里的**示例锚点**不许静默混进去（2026-10-15）。

起因（实测，不是推演）：活账本 `logs/judge_calibration_history.json` 里唯一那条带数字的
校准行（2026-10-10，n=11）中，**6 条锚点是仓库自带的示例集**——`demo-good` /
`demo-hallucinated` / `demo-vague` / `anchor-fab-trend` / `anchor-fab-attribution` /
`anchor-mixed`，与 `judge_calibration/samples.example.json` 逐字同名；只有 5 条是 `real-*`。
这些示例的人工分是**编的**，用途是让第一次跑的人把流程跑通。

而 README §三 那句"评委这把尺子没有对准所有者的标准"引用的 MAE/bias 正是从这条账本
重算的 ⇒ 结论有一半压在编出来的锚点上，读的人却无从得知。聚合刻意不按考卷过滤
（§十四 的先例：换一批锚点重测，对锚点总体的估计依然成立），那就更必须**当场披露**，
而不是让"准不准"这句话悬空。

这与 `test_history_mock_filter.py` 是同族：mock 臂不许混进 Δ 统计，示例锚点也不许
**静默**混进校准聚合。区别只在于这里不拦、只披露——示例轮本身是合法的历史记录。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pm import calibration as C

REPO = Path(__file__).resolve().parents[1]


def _pairs(*ids: str) -> list[dict[str, Any]]:
    return [{"id": i, "human": 5.0, "judge": 6.0} for i in ids]


# --------------------------------------------------------------------------
# 识别：示例集认得出，真锚点不误伤
# --------------------------------------------------------------------------


def test_example_ids_come_from_the_shipped_example_file() -> None:
    """示例 id 只能取自仓库里那份文件，不许在函数里写死一张表（漂移了没人知道）。"""
    ids = C.example_anchor_ids()
    assert ids, "读不到示例锚点集 ⇒ 下面所有披露都会恒为 0，守卫变空闸"
    on_disk = json.loads(C.EXAMPLE_SAMPLES.read_text(encoding="utf-8"))
    rows = on_disk if isinstance(on_disk, list) else on_disk["samples"]
    assert ids == {str(r["id"]) for r in rows}


def test_real_anchors_are_never_counted_as_examples() -> None:
    """反向护栏：真锚点轮必须得 0，否则"示例"这个标签就被用废了。"""
    assert C.count_example_anchors(_pairs("real-a", "real-b", "anchor-x")) == 0


def test_mixed_round_counts_only_the_example_ones() -> None:
    ex = sorted(C.example_anchor_ids())[:1]
    assert ex, "示例集为空，这条用例无从构造"
    pairs = _pairs("real-a", *ex)
    assert C.count_example_anchors(pairs) == 1


def test_missing_example_file_means_zero_not_everything() -> None:
    """读不到示例文件时必须报 0（"认不出"），不许把整批都判成示例。"""
    assert C.count_example_anchors(_pairs("demo-good")) >= 0  # 文件在：能认出
    # 空 id 集合 ⇒ 认不出 ⇒ 0
    assert C.count_example_anchors([]) == 0


# --------------------------------------------------------------------------
# 披露：掺了必须说，没掺别啰嗦
# --------------------------------------------------------------------------


def test_render_discloses_when_examples_are_in_the_pool() -> None:
    pooled = {"n_anchors": 11, "example_anchors": 6}
    text = "\n".join(C.render_example_anchor_disclosure(pooled))
    assert "6/11" in text, text
    assert "示例" in text and "编的" in text, "只报数字不说性质，读者照样会当真锚点抄走"


def test_render_is_silent_when_the_pool_is_clean() -> None:
    assert C.render_example_anchor_disclosure({"n_anchors": 9, "example_anchors": 0}) == []
    assert C.render_example_anchor_disclosure({}) == []


# --------------------------------------------------------------------------
# 接线：聚合算得出，渲染接得上
# --------------------------------------------------------------------------


def test_aggregate_counts_example_anchors_into_pooled() -> None:
    """聚合必须把示例条数算进 pooled —— 只加渲染函数不加这里是"半个功能"。"""
    ex = sorted(C.example_anchor_ids())[:2]
    history = [
        {
            "ts": "2026-10-10 03:02:44",
            "judge": "evaluator",
            "mode": "impression",
            "n": 3,
            "items": [
                {"id": ex[0], "human": 5.0, "judge": 6.0},
                {"id": ex[1], "human": 4.0, "judge": 5.0},
                {"id": "real-a", "human": 7.0, "judge": 7.0},
            ],
        }
    ]
    agg = C.aggregate_history(history, mode="impression", judge="evaluator")
    assert agg is not None
    assert agg["pooled"]["example_anchors"] == 2, agg["pooled"]


def test_live_ledger_round_is_flagged_when_it_used_examples() -> None:
    """端到端：本机账本那条用示例跑的轮，聚合读数必须带披露（skip 条件写清楚）。

    这条不是"聚合能算"，是"真账本上确实算得出"——只验函数的那条在空账本机器上照样绿。
    """
    path = REPO / "logs" / "judge_calibration_history.json"
    if not path.exists():
        pytest.skip("本机没有校准账本（新检出）")
    history = json.loads(path.read_text(encoding="utf-8"))
    agg = C.aggregate_history(history, mode="impression", judge="evaluator")
    if not agg:
        pytest.skip("账本里没有 impression/evaluator 的带明细轮次")
    pooled = agg["pooled"]
    assert "example_anchors" in pooled, "pooled 里没有示例计数 ⇒ 接线断了"
    n = int(pooled["example_anchors"] or 0)
    lines = C.render_aggregate(agg)  # 返回整段字符串，别再 join（会把字符拆开）
    if n:
        assert "示例" in lines, f"账本里 {n} 条示例锚点，聚合渲染却一个字没提"
    else:
        assert "示例集" not in lines, "全是真锚点时不该挂那句警示"
