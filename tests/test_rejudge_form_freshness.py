"""判分页是**派生产物**：数据源一动，HTML 必须跟着动。

2026-11-13 实测到的漂移（本文件就是它的补丁）：`judge_calibration/rejudge_form.html`
里 25 条的「备注」还停在 2026-10-18 的文案（「【owner 亲判】改判 AI 代判条」），
而打分队列在 11-09 已按不确定性取样重排（「判定线附近…」「随机审计切片…」）。
表单能打开、能填分、能导出，导出的 CSV 还把旧备注写回表里 —— 人看不出来，
程序也不报错。判分页是唯一需要人坐着读完 49 份输出的入口，它错了等于白判。

两条守卫：
1. **逐字一致**：已提交的 HTML 必须等于 `scripts/build_rejudge_form.py` 现在生成的内容
   （不是"长得像"，是 byte 相等 —— 数据、备注、key、导出的列名都算）；
2. **覆盖集正确**：表单里嵌的 id 恰好是当前待判集，顺序也一致
   （顺序错了人就会对着 A 的卡片打 B 的分）。
"""

from __future__ import annotations

import csv
import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "build_rejudge_form.py"
CALIB = ROOT / "judge_calibration"

BATCHES = {
    "main": {
        "html": CALIB / "rejudge_form.html",
        "form": CALIB / "score_form.owner_rejudge.csv",
        "samples": CALIB / "samples.candidates.json",
    },
    "r4": {
        "html": CALIB / "rejudge_form.r4.html",
        "form": CALIB / "score_form.r4.csv",
        "samples": CALIB / "samples.candidates.r4.json",
    },
}


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("build_rejudge_form", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _embedded(html: str) -> tuple[list[dict[str, Any]], list[str]]:
    """从 HTML 里抠出嵌进去的 DATA 与导出列名（导出的 CSV 靠这两样回填）。"""
    data_line = next(ln for ln in html.splitlines() if ln.startswith("const DATA = "))
    cols_line = next(ln for ln in html.splitlines() if ln.startswith("  const cols = "))
    data = json.loads(data_line.removeprefix("const DATA = ").strip().rstrip(";"))
    cols = json.loads(cols_line.removeprefix("  const cols = ").strip().rstrip(";"))
    return data, cols


def _expected_ids(batch: str) -> list[str]:
    """待判 id，按表里的顺序：主线 = 备注写「要你判」且没填分的行；r4 = 整批没人工分的行。"""
    if batch == "main":
        with BATCHES[batch]["form"].open(encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.DictReader(fh))
        return [
            r["id"]
            for r in rows
            if "要你判" in (r.get("备注") or "") and not (r.get("human_score") or "").strip()
        ]
    items = json.loads(BATCHES[batch]["samples"].read_text(encoding="utf-8"))
    return [str(it["id"]) for it in items if not it.get("human_score")]


@pytest.mark.parametrize("batch", list(BATCHES))
def test_committed_form_is_byte_identical_to_its_sources(batch: str) -> None:
    """已提交的 HTML 必须与「现在重新生成」的结果逐字相同 —— 数据源改了就得重生成。"""
    if not _expected_ids(batch):
        pytest.skip(f"{batch} 已判完，没有待判行")
    mod = _load()
    assert mod.build(batch, check=True) == 0, (
        f"{batch} 的判分页与数据源（打分表 / 锚点 JSON）不一致："
        f"跑 `python scripts/build_rejudge_form.py --batch {batch}` 重生成后再提交"
    )


@pytest.mark.parametrize("batch", list(BATCHES))
def test_form_covers_exactly_the_pending_rows_in_order(batch: str) -> None:
    """表单里嵌的 id 与待判集一一对应且同序；导出列名与 `_FORM_COLUMNS` 同序。"""
    from pm.cli.calibrate import _FORM_COLUMNS

    html = BATCHES[batch]["html"].read_text(encoding="utf-8")
    data, cols = _embedded(html)
    expected = _expected_ids(batch)
    if not expected:
        pytest.skip(f"{batch} 已判完，没有待判行")

    assert [it["id"] for it in data] == expected, (
        f"{batch} 的判分页覆盖的 id 与打分表对不上（顺序也算）—— "
        "人是对着卡片打分的，错一条就是把分记到别人头上"
    )
    assert cols == list(_FORM_COLUMNS), f"{batch} 导出的列名/顺序必须与 --apply-scores 认的一致"
    # 每条都得有可判的正文：输出为空或只有摘要会让「值不值 8 分」无从判起。
    empty = [it["id"] for it in data if not it.get("output") or not it.get("task")]
    assert not empty, f"{batch} 这些行缺完整输出或原始需求：{empty}"
