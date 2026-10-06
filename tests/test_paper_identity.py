""" "是不是同一张考卷"必须有唯一答案（§三十二）。

## 判据（写在读数之前）

1. 考卷身份 = 账本轮记录里的 `anchors` 指纹（`_anchors_stamp`：每条 (id, 人工分) 的
   集合摘要，2026-09-18 就存在）。**条数只是它在老记录里的替身**，不是身份本身。
2. 漂移读者（`_comparable`）与聚合读者（`aggregate_history`）对"这两轮是不是同一张考卷"
   必须给**同一个答案**——这是 §二十九"同一本账本两本读者"的同一类错误，
   只是这次漂的是考卷而不是尺子。
3. 老记录没有指纹时不许猜"相同"：退化按条数判，并**在报告里明说自己退化了**。
   读不到就当作一样，等于把"没证据"当成"证据表明没变"。

## 缺陷（实测复现在下面第 1、2 条）

`papers_differ` 写的是 `len({r.get("n") for r in rounds}) > 1` —— 用**条数集合的大小**
当考卷身份。于是"换掉 5 条又补进 5 条"（n 不变、集合全变）这一轮，聚合侧报
"没换考卷"，而漂移侧拿着指纹明说"锚点集已更换"。产品报告引用的是聚合那行。
"""

from __future__ import annotations

import json
from typing import Any

from pm.calibration import aggregate_history, render_aggregate

_STAMP_A = "aaaa111122"
_STAMP_B = "bbbb333344"


def _items(prefix: str, n: int = 4) -> list[dict[str, Any]]:
    return [
        {
            "id": f"{prefix}{i}",
            "human": 9.0 if i % 2 else 3.0,
            "judge": 8.0 + i * 0.1,
            "delta": 0.1,
        }
        for i in range(n)
    ]


def _round(ts: str, items: list[dict[str, Any]], anchors: str | None = None) -> dict[str, Any]:
    rec: dict[str, Any] = {
        "ts": ts,
        "judge": "evaluator",
        "mode": "impression",
        "model": "some-judge",
        "rubric": "some-rubric",
        "n": len(items),
        "mae": 1.2,
        "bias": 0.4,
        "items": items,
    }
    if anchors is not None:
        rec["anchors"] = anchors
    return rec


def _agg(*rounds: dict[str, Any]) -> dict[str, Any] | None:
    return aggregate_history(
        list(rounds), mode="impression", judge="evaluator", bootstrap=200, last=0
    )


# ----------------------------------------------------- 判据 1/2：条数不是身份


def test_same_count_different_anchor_set_is_a_different_paper() -> None:
    """换掉一半锚点、条数没变 —— 这就是"换考卷"，聚合侧必须认。"""
    agg = _agg(
        _round("2026-10-01 03:00:00", _items("a"), _STAMP_A),
        _round("2026-10-02 03:00:00", _items("b"), _STAMP_B),
    )
    assert agg is not None
    assert agg["papers_differ"] is True, "n 相同、集合不同 ⇒ 是两张考卷"
    assert agg["paper_identity"]["mode"] == "stamp"
    assert agg["paper_identity"]["distinct_papers"] == 2


def test_same_stamp_is_the_same_paper_even_across_rounds() -> None:
    agg = _agg(
        _round("2026-10-01 03:00:00", _items("a"), _STAMP_A),
        _round("2026-10-02 03:00:00", _items("a"), _STAMP_A),
    )
    assert agg is not None
    assert agg["papers_differ"] is False
    assert agg["paper_identity"]["distinct_papers"] == 1


def test_the_two_ledger_readers_agree_on_paper_identity() -> None:
    """承重的一条：漂移侧与聚合侧对同一本账本必须同答案。

    两侧用的是同一个 `anchors` 字段，但过去各自解释：漂移判"不可比"，
    聚合判"没换考卷"。产品报告引用的是聚合那行 ⇒ 读者被引导着做跨考卷比较。
    """
    from pm.cli.calibrate import _Cohort, _comparable

    prev = _round("2026-10-01 03:00:00", _items("a"), _STAMP_A)
    now = _round("2026-10-02 03:00:00", _items("b"), _STAMP_B)
    cohort = _Cohort(
        judge="evaluator",
        mode="impression",
        model="some-judge",
        rubric="some-rubric",
        anchors=_STAMP_B,
        n=4,
    )
    drift_says_same = _comparable(prev, cohort)
    agg = _agg(prev, now)
    assert agg is not None
    assert drift_says_same is False, "夹具坏了：漂移侧本应认出新考卷"
    assert agg["papers_differ"] is True, "两本读者答案相反"


# ------------------------------------------------------- 判据 3：退化要自报


def test_unstamped_rounds_fall_back_to_count_and_say_so() -> None:
    """老记录没有指纹：可以按条数退化判，但必须留下"这是替身判据"的痕。"""
    agg = _agg(
        _round("2026-09-20 03:00:00", _items("a")),
        _round("2026-09-21 03:00:00", _items("b")),  # 同样 4 条，集合其实不同
    )
    assert agg is not None
    ident = agg["paper_identity"]
    assert ident["mode"] == "count-fallback"
    assert ident["unstamped_rounds"] == 2
    txt = render_aggregate(agg)
    assert "条数" in txt and "指纹" in txt, "退化路径必须在报告里可见"


def test_mixed_ledger_is_treated_as_degraded_not_as_proof_of_same_paper() -> None:
    """一半有一半没有：不许因为"有的那几轮指纹一致"就宣布同一张考卷。"""
    agg = _agg(
        _round("2026-09-20 03:00:00", _items("a")),
        _round("2026-10-01 03:00:00", _items("a"), _STAMP_A),
        _round("2026-10-02 03:00:00", _items("a"), _STAMP_A),
    )
    assert agg is not None
    assert agg["paper_identity"]["mode"] == "count-fallback"
    assert agg["paper_identity"]["unstamped_rounds"] == 1


def test_render_wording_matches_what_the_code_actually_compared() -> None:
    """报告说"条数不同"而代码比的是指纹，就是给读者一套假的判据出处。"""
    agg = _agg(
        _round("2026-10-01 03:00:00", _items("a"), _STAMP_A),
        _round("2026-10-02 03:00:00", _items("b"), _STAMP_B),
    )
    txt = render_aggregate(agg)
    assert "指纹" in txt
    assert "条数不同" not in txt


def test_real_ledger_shape_still_aggregates(tmp_path: Any) -> None:
    """端到端一条：走真实账本文件（带锚点指纹）也要给出身份判定。"""
    entries = [
        _round("2026-10-01 03:00:00", _items("a"), _STAMP_A),
        _round("2026-10-02 03:00:00", _items("b"), _STAMP_B),
    ]
    (tmp_path / "ledger.json").write_text(json.dumps(entries), encoding="utf-8")
    loaded = json.loads((tmp_path / "ledger.json").read_text(encoding="utf-8"))
    agg = _agg(*loaded)
    assert agg is not None and agg["papers_differ"] is True
