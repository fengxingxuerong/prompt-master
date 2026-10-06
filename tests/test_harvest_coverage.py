"""补采覆盖度（coverage_by_human_cell / band_yield / recommended_bands / select(prefer)）的回归。

背景（2026-10-06 实测）：`select()` 一直按**评委分带**均匀分层，理由写在它自己的
docstring 里（人工分离散度低时 r 无意义）。但 κ/一致率的可估性取决于**人工标签**的格子：
补采了一轮 45 条之后，所有者亲判的"达标"格还是只有个位数。这里把"哪个格子缺几条"
变成可算的表，并且门槛与 pm.calibration 同源，不另立一个数。

零 LLM、零网络、不写运营文件（--coverage 必须是只读的）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import harvest_anchors as H
from pm.calibration import MIN_ESTIMABLE_CELL

ITEM_KEYS = ("id", "human_score", "original_task", "test_input", "test_output", "prompt")


def _anchor(i: int, human: float, judge: float | None = None, src: str = "owner") -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": f"a{i}-{int(human * 10)}",
        "human_score": human,
        "original_task": f"任务{i}",
        "test_input": "输入",
        "test_output": "输出",
        "prompt": "提示词",
        "confirmed": True,
        "provenance": {"human_score_source": src},
    }
    if judge is not None:
        item["provenance"]["judge_score"] = judge
    return item


def _fill(base: Path, items: list[dict[str, Any]], name: str = "samples.json") -> None:
    base.mkdir(parents=True, exist_ok=True)
    (base / name).write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")


def _many(n_per_band: int, src: str = "owner") -> list[dict[str, Any]]:
    """每个人工分带各 n 条，用于构造"格子填满"的对照。"""
    out: list[dict[str, Any]] = []
    k = 0
    for _, human in (
        ("<6.0", 3.0),
        ("6.0-6.9", 6.5),
        ("7.0-7.9", 7.5),
        ("8.0-8.9", 8.5),
        (">=9.0", 9.5),
    ):
        for _ in range(n_per_band):
            out.append(_anchor(k, human, src=src))
            k += 1
    return out


# -------------------------------------------------------------------- 1. 去重


def test_same_id_across_files_counts_once(tmp_path: Path) -> None:
    """实测缺陷：ab.json 与 samples.json 有 18 条同 id，按行累加会把一格数成两格，
    而这张表本身就是"还差几条"的判据 —— 重复即虚高，直接导致"格子满了"的假结论。"""
    shared = [_anchor(i, 8.5) for i in range(3)]
    _fill(tmp_path, shared, "samples.json")
    _fill(tmp_path, [*shared, _anchor(99, 3.0)], "samples.ab.json")
    cov = H.coverage_by_human_cell(tmp_path)
    assert cov["sources"]["owner"]["n"] == 4
    assert cov["sources"]["owner"]["bands"]["8.0-8.9"] == 3


def test_example_file_and_unscored_rows_are_ignored(tmp_path: Path) -> None:
    _fill(tmp_path, [_anchor(1, 9.0)], "samples.example.json")
    _fill(
        tmp_path,
        [{"id": "pending", "human_score": None, "confirmed": False}],
        "samples.candidates.json",
    )
    assert H.coverage_by_human_cell(tmp_path)["sources"] == {}


# ---------------------------------------------------------------- 2. 覆盖与门槛


def test_deficient_cells_use_the_shared_threshold(tmp_path: Path) -> None:
    below = MIN_ESTIMABLE_CELL - 1
    _fill(tmp_path, _many(below))
    cov = H.coverage_by_human_cell(tmp_path)
    assert set(cov["sources"]["owner"]["deficient"]) == {
        "<6.0",
        "6.0-6.9",
        "7.0-7.9",
        "8.0-8.9",
        ">=9.0",
    }
    _fill(tmp_path.parent / "full", _many(MIN_ESTIMABLE_CELL))
    assert H.coverage_by_human_cell(tmp_path.parent / "full")["sources"]["owner"]["deficient"] == []


def test_ai_proxy_shortfall_does_not_drive_the_queue(tmp_path: Path) -> None:
    """AI 代判与评委同族，拿它排出来的补采顺序是在加固同源偏见：
    只有所有者亲判的格子缺，才产生建议顺序。"""
    items = [
        _anchor(i, h, judge=j, src="owner")
        for i, (h, j) in enumerate(
            [(3.0, 8.4)] * 10
            + [(6.5, 8.4)] * 10
            + [(7.5, 8.4)] * 10
            + [(8.5, 9.4)] * 10
            + [(9.5, 9.4)] * 10
        )
    ]
    # 代判侧只有 >=9.0 这一格够，其余四格都缺 —— 但它不该驱动队列
    items += [
        _anchor(500 + i, h, judge=j, src="ai_proxy")
        for i, (h, j) in enumerate([(3.0, 8.4), (8.5, 8.6), (9.5, 9.6)])
    ]
    _fill(tmp_path, items)
    cov = H.coverage_by_human_cell(tmp_path)
    assert cov["sources"]["ai_proxy"]["deficient"], "代判侧应当确实缺格"
    assert not cov["sources"]["owner"]["deficient"], "夹具坏了：所有者侧本应填满"
    y = H.band_yield(tmp_path)
    assert y, "夹具坏了：没有评委分的话这条断言区分不了两种实现"
    assert H.recommended_bands(cov, y) == ([], [])
    # 反向对照：把"也算代判"的实现放回去就必须红 —— 所以这里显式验一次非空输入下的行为
    cov_fake = {
        "min_cell": cov["min_cell"],
        "sources": {"owner": {"deficient": ["<6.0"], "n": 0, "bands": {}}},
    }
    assert H.recommended_bands(cov_fake, y)[0] != [], "有缺口时应给出顺序"


# ---------------------------------------------------------------- 3. 历史命中率


def test_band_yield_counts_mapping_not_assumption(tmp_path: Path) -> None:
    """评委分带 → 人工达标的映射要实测：评委说 8.0-8.9 的那批，人工判达标的有几条？
    2026-10-06 全量实算是 0/14 —— 这正是"按评委分带补采补不出正例"的原因。"""
    items = [_anchor(i, 3.0, judge=8.4) for i in range(4)]  # 评委说达标，人工说不
    items += [_anchor(50 + i, 9.0, judge=9.4) for i in range(2)]  # 两边都说达标
    items += [_anchor(90, 8.5, judge=None) for _ in range(1)]  # 没有评委分 → 不计
    _fill(tmp_path, items)
    y = H.band_yield(tmp_path)
    assert y["8.0-8.9"] == {"labeled": 4, "human_pass": 0}
    assert y[">=9.0"] == {"labeled": 2, "human_pass": 2}
    assert sum(v["labeled"] for v in y.values()) == 6


def test_recommended_bands_prefers_high_yield_when_support_is_adequate() -> None:
    """支持度够（每带 ≥ 门槛）时，才轮到"历史命中率"决定顺序。"""
    cov = {"sources": {"owner": {"deficient": [">=9.0"], "n": 0, "bands": {}}}}
    yields = {
        ">=9.0": {"labeled": 19, "human_pass": 3},  # 实测值：2026-10-06 那批锚点
        "8.0-8.9": {"labeled": 14, "human_pass": 0},
    }
    ranked, weak = H.recommended_bands(cov, yields)
    assert ranked == [">=9.0", "8.0-8.9"]
    assert weak == []


def test_low_support_band_cannot_outrank_a_well_supported_one() -> None:
    """今天把工具真走了一遍才暴露的缺陷（不是假想）：实测锚点里评委带 `<6.0` 只被
    人工判过 3 条（其中 1 条达标 → 平滑后命中率 0.417，全场最高），于是第一版
    `--prefer-deficient` 把**已经最满的那一格**（owner 17 条）排到了队列第一位。

    n=3 的比率不该决定"接下来花所有者 20 分钟判哪几条"——§三十一 立可估性门槛
    挡的就是这件事，门槛这次得管到自己写的排序上。
    """
    cov = {"sources": {"owner": {"deficient": [">=9.0"], "n": 0, "bands": {}}}}
    yields = {
        "<6.0": {"labeled": 3, "human_pass": 1},  # 0.417（平滑后最高）
        ">=9.0": {"labeled": 19, "human_pass": 3},  # 0.190
    }
    ranked, weak = H.recommended_bands(cov, yields)
    assert ranked[0] == ">=9.0", ranked
    assert "<6.0" in weak and ranked[-1] == "<6.0"


def test_turning_the_support_floor_off_reproduces_the_defect() -> None:
    """门槛一关掉就复现缺陷 ⇒ 起作用的是支持度门槛，不是拉普拉斯平滑。

    这条是"不红的变异不许写进证据链"的反面用法：断言必须能区分两种实现。
    """
    cov = {"sources": {"owner": {"deficient": [">=9.0"], "n": 0, "bands": {}}}}
    yields = {"<6.0": {"labeled": 3, "human_pass": 1}, ">=9.0": {"labeled": 19, "human_pass": 3}}
    ranked, weak = H.recommended_bands(cov, yields, min_support=1)
    assert ranked[0] == "<6.0"
    assert weak == []


def test_weak_bands_are_disclosed_not_silently_dropped() -> None:
    """排后 ≠ 不补：那几格照样缺，只是不许靠一两个偶然样本插队，且理由要说出来。"""
    cov = {"sources": {"owner": {"deficient": [">=9.0"], "n": 0, "bands": {}}}}
    yields = {"<6.0": {"labeled": 3, "human_pass": 1}, ">=9.0": {"labeled": 19, "human_pass": 3}}
    cov["min_cell"] = MIN_ESTIMABLE_CELL  # 缺键就该炸：render 用的门槛必须来自同一处
    txt = H.render_coverage(cov, yields)
    tail = txt.split("建议补采顺序")[1]
    assert "<6.0" in tail, "被排后的带仍要出现在队列里（排后不等于不补）"
    assert "排在末尾不是因为命中率低" in txt


# ------------------------------------------------------------------ 4. select 顺序


def _row(out_sha: str, task: str, judge: float) -> dict[str, Any]:
    return {
        "out_sha": out_sha,
        "original_task": task,
        "context": "",
        "prompt": "p",
        "test_input": "i",
        "test_output": "o" * 50,
        "judge_score": judge,
        "judge": "evaluator",
        "judge_dims": {},
        "judge_issues": [],
        "model_reported_score": judge,
        "target_model": "glm-5.2",
        "source_run": "run_x.json",
        "hints": [],
        "band": "",
    }


def test_prefer_changes_visit_order_not_per_band_cap() -> None:
    rows = [_row(f"a{i}", f"t{i}", 9.5) for i in range(6)]  # >=9.0 带
    rows += [_row(f"b{i}", f"u{i}", 3.2) for i in range(6)]  # <6.0 带
    plain = H.select(list(rows), per_band=3, skip_shas=set())
    assert {r["band"] for r in plain} == {">=9.0", "<6.0"}
    # 不传 prefer 时按 BANDS 固定顺序，<6.0 先来；把 8.0-8.9 之外的"靠后带"提到前面才测得出顺序
    rows2 = [_row(f"c{i}", f"v{i}", 8.4) for i in range(6)] + [
        _row(f"d{i}", f"w{i}", 3.2) for i in range(6)
    ]
    plain = H.select(list(rows2), per_band=2, skip_shas=set())
    assert plain[0]["band"] == "<6.0"
    preferred = H.select(list(rows2), per_band=2, skip_shas=set(), prefer=("8.0-8.9",))
    assert preferred[0]["band"] == "8.0-8.9"
    # 每带上限不许被 prefer 悄悄放宽，否则同一族输出能灌满候选集
    assert sum(1 for r in preferred if r["band"] == "8.0-8.9") == 2
    assert (
        sum(
            1
            for r in H.select(list(rows), per_band=3, skip_shas=set(), prefer=("<6.0",))
            if r["band"] == "<6.0"
        )
        == 3
    )


def test_prefer_order_is_a_sequence_not_a_membership_set() -> None:
    """实测缺陷：`ordered` 曾写成 `[b for b in BANDS if b[0] in prefer] + ...`，
    那只是把带分成两段，**prefer 内部的先后仍被 BANDS 顺序覆盖**。
    单带 prefer 的夹具看不出来（我上一版就是这么漏过的）——两种顺序必须给出不同结果。"""
    rows = [_row(f"c{i}", f"v{i}", 8.4) for i in range(4)] + [
        _row(f"d{i}", f"w{i}", 3.2) for i in range(4)
    ]
    a = H.select(list(rows), per_band=4, skip_shas=set(), prefer=("8.0-8.9", "<6.0"))
    b = H.select(list(rows), per_band=4, skip_shas=set(), prefer=("<6.0", "8.0-8.9"))
    assert [r["band"] for r in a] == ["8.0-8.9"] * 4 + ["<6.0"] * 4
    assert [r["band"] for r in b] == ["<6.0"] * 4 + ["8.0-8.9"] * 4
    assert a != b, "顺序必须真的起作用，否则 prefer 只是装饰"


def test_total_lets_priority_decide_who_gets_a_slot() -> None:
    """prefer 只在**名额有限**时才决定谁进队列：每带各自封顶时把某带排前面并不会让它多拿名额。
    这条就是 2026-10-06 发现 `--prefer-deficient` 是个空开关的复现。"""
    rows = [_row(f"c{i}", f"v{i}", 8.4) for i in range(4)] + [
        _row(f"d{i}", f"w{i}", 3.2)
        for i in range(40)  # 最满的那一格素材最多
    ]
    ordered = H.select(list(rows), per_band=6, skip_shas=set(), prefer=("8.0-8.9", "<6.0"))
    assert len(ordered) == 10, "不设 total 时 prefer 只排序，不该减少条数"
    capped = H.select(list(rows), per_band=6, skip_shas=set(), prefer=("8.0-8.9", "<6.0"), total=5)
    assert [r["band"] for r in capped] == ["8.0-8.9"] * 4 + ["<6.0"], capped
    # 反向对照：同样的 total，不给 prefer 就还是 BANDS 顺序（<6.0 先占满）
    naive = H.select(list(rows), per_band=6, skip_shas=set(), total=5)
    assert [r["band"] for r in naive] == ["<6.0"] * 5


# --------------------------------------------------------------- 5. --coverage 必须只读


def test_coverage_flag_writes_nothing(tmp_path: Path, capsys: Any, monkeypatch: Any) -> None:
    """这个开关的目的是"随时能看一眼还差几条"。它一旦顺手重写候选文件或 REVIEW.md，
    就等于把未确认的候选覆盖掉人工已填的分数——那是最贵的一类事故。"""
    target = tmp_path / "judge_calibration"
    _fill(target, [_anchor(i, 8.5) for i in range(3)])
    (target / "samples.candidates.json").write_text("[]", encoding="utf-8")
    (target / "REVIEW.md").write_text("原内容", encoding="utf-8")
    before = {p.name: p.read_bytes() for p in target.glob("*")}
    monkeypatch.setattr(sys, "argv", ["harvest_anchors.py", "--coverage"])
    monkeypatch.setattr(H, "ROOT", tmp_path)
    assert H.main() == 0
    assert {p.name: p.read_bytes() for p in target.glob("*")} == before
    out = capsys.readouterr().out
    assert "人工标签格子覆盖度" in out
    assert f"≥{MIN_ESTIMABLE_CELL}" in out
