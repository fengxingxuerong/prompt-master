"""改判队列脚本的回归：表必须留空、回填必须只动填了分的行、且把那些行记成 owner 亲判。

承重的是第 2 条。`scripts/build_owner_rejudge_queue.py` 的话术是
"留空 = 不动、填了才写"，而它指向的是**装着 48 条不可再行人工分的文件**——
这句话如果是错的，一次"照着说明操作"就会毁掉校准的整个依据。
所以它在真文件的副本上验，绝不碰 `judge_calibration/samples.candidates.json` 本身。
"""

from __future__ import annotations

import csv
import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest
from pm.cli.calibrate import _apply_score_form

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "build_owner_rejudge_queue.py"


def _load(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    spec = importlib.util.spec_from_file_location("owner_rejudge_queue", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "TARGET", tmp_path / "samples.candidates.json")
    monkeypatch.setattr(mod, "FORM", tmp_path / "score_form.owner_rejudge.csv")
    return mod


def _anchor(i: int, human: float, src: str) -> dict[str, Any]:
    return {
        "id": f"h-{i}",
        "original_task": f"任务{i}",
        "context": "",
        "prompt": f"提示词{i}" * 8,
        "test_input": f"输入{i}",
        "test_output": f"输出内容{i}" * 10,
        "human_score": human,
        "confirmed": True,
        "band": "8.0-8.9" if human >= 8 else "7.0-7.9",
        "note": "既有备注",
        "provenance": {"human_score_source": src, "judge_score": 8.6, "target_model": "glm-5.2"},
    }


@pytest.fixture()
def store(tmp_path: Path) -> Path:
    # 故意打乱存储顺序，让"文件顺序"与"缺口顺序"不同 —— 第一版夹具两者恰好一致，
    # 于是"表按缺口排序"这条断言区分不了两种实现（去掉 sort 也照样绿）。
    items = [
        _anchor(3, 7.0, "ai_proxy_glm-5.3"),
        _anchor(5, 3.0, "ai_proxy_glm-5.3"),
        _anchor(1, 9.0, "ai_proxy_glm-5.3"),
        _anchor(4, 6.0, "owner"),
        _anchor(2, 8.5, "ai_proxy_glm-5.3"),
        # h-0：低段但 id 最小 —— 于是"按 id 排"与"按缺口排"必须给出不同结果
        _anchor(0, 2.0, "ai_proxy_glm-5.3"),
    ]
    path = tmp_path / "samples.candidates.json"
    path.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    return path


# ------------------------------------------------------------------ 1. 表的形状


def test_form_leaves_every_score_blank_and_marks_who_must_judge(
    monkeypatch: pytest.MonkeyPatch, store: Path, tmp_path: Path
) -> None:
    mod = _load(monkeypatch, tmp_path)
    assert mod.build_form() == 0
    with (tmp_path / "score_form.owner_rejudge.csv").open(encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 6
    assert all(r["human_score"] == "" for r in rows), (
        "把现值抄进表 = 让他重抄 5 次并给误改留口子；AI 代判的分抄进表还会锚定他的判断"
    )
    marked = {r["id"]: r["备注"] for r in rows}
    assert marked["h-1"].startswith("【要你判】") and marked["h-3"].startswith("【要你判】")
    assert marked["h-4"].startswith("【已亲判·不用填】")
    # 排序：评委说的达标段（>=8）必须排在前面，那才是最缺的格子
    todo_ids = [r["id"] for r in rows if r["备注"].startswith("【要你判】")]
    # 同一段内按 id 排（h-0 < h-5），段间按缺口大小排：>=8 先、6.x 次之、<6 最后
    assert todo_ids == ["h-1", "h-2", "h-3", "h-0", "h-5"], todo_ids


def test_script_refuses_when_nothing_to_rejudge(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    items = [_anchor(9, 8.0, "owner")]
    (tmp_path / "samples.candidates.json").write_text(
        json.dumps(items, ensure_ascii=False), encoding="utf-8"
    )
    mod = _load(monkeypatch, tmp_path)
    assert mod.build_form() == 0
    assert not (tmp_path / "score_form.owner_rejudge.csv").exists()


# ------------------------------------------------- 2. 承重：照说明操作不会毁掉存量


def test_partial_fill_touches_only_filled_rows_and_records_owner(
    monkeypatch: pytest.MonkeyPatch, store: Path, tmp_path: Path
) -> None:
    mod = _load(monkeypatch, tmp_path)
    assert mod.build_form() == 0
    form = tmp_path / "score_form.owner_rejudge.csv"
    before = {str(x["id"]): x for x in json.loads(store.read_text(encoding="utf-8"))}

    with form.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    # 他只判两行：一条达标段的代判（h-2，AI 曾说 8.5）、一条低段的（h-5，AI 曾说 3.0）
    filled = {"h-2": "4", "h-5": "8"}
    for r in rows:
        if r["id"] in filled:
            r["human_score"] = filled[r["id"]]
    with form.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    assert _apply_score_form(store, form) == 0
    after = {str(x["id"]): x for x in json.loads(store.read_text(encoding="utf-8"))}

    assert float(after["h-2"]["human_score"]) == 4.0
    assert after["h-2"]["provenance"]["human_score_source"].startswith("owner"), (
        "改判必须从 ai_proxy 变成 owner，否则补的还是同源标签，达标格依旧读不出 κ"
    )
    assert float(after["h-5"]["human_score"]) == 8.0
    # 没填的三条：分值、出处、confirmed 全部原样
    for untouched in ("h-1", "h-3", "h-4", "h-0"):
        assert after[untouched]["human_score"] == before[untouched]["human_score"], untouched
        assert (
            after[untouched]["provenance"]["human_score_source"]
            == before[untouched]["provenance"]["human_score_source"]
        ), untouched
        assert after[untouched]["confirmed"] == before[untouched]["confirmed"], untouched


def test_a_garbage_row_aborts_the_whole_apply(
    monkeypatch: pytest.MonkeyPatch, store: Path, tmp_path: Path
) -> None:
    """`_apply_score_form` 承诺"有任何非法行就一条都不写"。

    这条承诺是脚本话术的前提（他说"留空即不动"），所以连它一起验，
    否则半写状态下毁掉的正是那份不可再生的文件。
    """
    mod = _load(monkeypatch, tmp_path)
    assert mod.build_form() == 0
    form = tmp_path / "score_form.owner_rejudge.csv"
    with form.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    for r in rows:
        if r["id"] == "h-2":
            r["human_score"] = "4"
        if r["id"] == "h-3":
            r["human_score"] = "17"  # 越界：1-10 之外
    with form.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    before = store.read_bytes()
    rc = _apply_score_form(store, form)
    assert rc != 0, "非法行必须让整次回填作废"
    assert store.read_bytes() == before, "拒绝路径上一字节都不许动"
