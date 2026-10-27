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


def _anchor(i: int, human: float, src: str, judge: float = 8.6) -> dict[str, Any]:
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
        "provenance": {
            "human_score_source": src,
            "judge_score": judge,
            "target_model": "glm-5.2",
        },
    }


@pytest.fixture()
def store(tmp_path: Path) -> Path:
    # 夹具的三个作业（缺一个，对应实现就能蒙混过关）：
    # ① 存储顺序 ≠ 队列顺序；
    # ② 队列顺序 ≠ id 顺序（否则"按不确定性排"与"按 id 排"分不开）；
    # ③ 队列顺序 ≠ 代判分顺序（否则"按不确定性排"与**旧的**按分数带排分不开）。
    #    ⇒ judge_score 必须逐条不同：h-2/h-3 贴着判定线 8.0，h-0/h-1/h-5 都在线外。
    items = [
        _anchor(3, 7.0, "ai_proxy_glm-5.3", judge=7.6),  # 近边界（|7.6-8|=0.4）
        _anchor(5, 3.0, "ai_proxy_glm-5.3", judge=6.0),  # 远（2.0）
        _anchor(1, 9.0, "ai_proxy_glm-5.3", judge=9.6),  # 远（1.6）—— 代判分最高
        _anchor(4, 6.0, "owner", judge=8.6),  # 已亲判，不进队列
        _anchor(2, 8.5, "ai_proxy_glm-5.3", judge=7.9),  # 近边界（|7.9-8|=0.1）
        _anchor(0, 2.0, "ai_proxy_glm-5.3", judge=9.8),  # 远（1.8）—— id 最小
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

    # 排序（2026-10-27 改，PE-4）：**离判定线最近的先出**，而不是"代判≥8 的先出"。
    # 旧实现给出 ["h-1","h-2","h-3","h-0","h-5"]（按代判分分段）——那是拿被测变量选样本。
    todo_ids = [r["id"] for r in rows if r["备注"].startswith("【要你判】")]
    assert todo_ids[:2] == ["h-2", "h-3"], f"判定线附近的两条必须排最前，实际 {todo_ids}"
    assert todo_ids != ["h-0", "h-1", "h-2", "h-3", "h-5"], "退化成按 id 排了"
    assert todo_ids != ["h-1", "h-2", "h-3", "h-0", "h-5"], "退化成按代判分排了（旧的偏斜口径）"


def test_audit_slice_is_random_but_reproducible(
    monkeypatch: pytest.MonkeyPatch, store: Path, tmp_path: Path
) -> None:
    """随机审计切片：既要**真的**从其余样本里抽，又必须**重跑出同一张表**。

    两个失效方向各挡一次：
      · 不抽（全按距离排）⇒ 锚点集全落在判定线附近，那是另一种偏斜；
      · 用 `random` 抽 ⇒ 同一份数据两次生成顺序不同，昨天填了一半的表与今天对不上号。
    """
    mod = _load(monkeypatch, tmp_path)
    monkeypatch.setattr(mod, "AUDIT_SLICE", 1)
    ids_first, counts = mod.plan()
    ids_second, _ = mod.plan()
    assert ids_first == ids_second, "同一份数据重跑必须得到同一张表（审计切片要可复现）"
    assert counts["audit_slice"] == 1, counts

    with (tmp_path / "score_form.owner_rejudge.csv").open("w", encoding="utf-8"):
        pass
    assert mod.build_form() == 0
    with (tmp_path / "score_form.owner_rejudge.csv").open(encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    audit_rows = [r for r in rows if "随机审计切片" in r["备注"]]
    assert len(audit_rows) == 1, f"审计切片应恰好 1 行，实际 {[r['id'] for r in audit_rows]}"
    # 切片必须来自"线外"那批，不能把边界样本重复算作切片
    assert audit_rows[0]["id"] in {"h-0", "h-1", "h-5"}, audit_rows[0]["id"]


def test_missing_judge_score_does_not_masquerade_as_boundary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """评委分缺失的样本不许插到最前面（`_distance` 若返回 0 就会那样）。

    为什么会缺：老归档/手工摘录的样本可能没有 `provenance.judge_score`。
    把"不知道离判定线多远"当成"正好在判定线上"是典型的假精确。
    """
    items = [
        _anchor(1, 5.0, "ai_proxy_glm-5.3", judge=7.9),  # 真边界
        _anchor(2, 5.0, "ai_proxy_glm-5.3"),  # judge 缺失
    ]
    items[1]["provenance"].pop("judge_score")
    (tmp_path / "samples.candidates.json").write_text(
        json.dumps(items, ensure_ascii=False), encoding="utf-8"
    )
    mod = _load(monkeypatch, tmp_path)
    ids, counts = mod.plan()
    assert ids[0] == "h-1", f"真有边界样本时它必须排第一，实际 {ids}"
    assert counts["near_boundary"] == 1, "评委分缺失的那条不该被算进边界样本"
    assert mod.build_form() == 0


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
