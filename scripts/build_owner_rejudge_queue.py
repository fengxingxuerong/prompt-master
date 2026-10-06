"""生成"这一轮请所有者判哪些"的打分表（零调用、不新建存储）。

## 为什么是这个形状

§三十一·十 当场数过：想让"达标一致性"从"不可估"变成能读，缺的是**所有者亲判的
"达标"样本**，而归档里评委 ≥8 的素材按输出内容去重后只剩 2 条没标过——
"再补一批新料"这条路基本是空的。

但存量里有 25 条人工分其实不是人打的：`samples.candidates.json` 里
`provenance.human_score_source = ai_proxy_*` 的那些（代判模型与评委同族，
量的是家族一致性）。把它们**改判**一遍，一次动作同时补两件事：
owner 的达标格条数，和"64 条人工锚点、bias +3.09 拆不出严格人工那一半"那个老洞。

## 两条约束

1. **只产视图，不产平行存储。** 表从 `judge_calibration/samples.candidates.json` 摊出来，
   回填也回到那一个文件。不新建 `samples.label_queue.json` 之类的副本——同一个 id
   在两个文件里各带一份人工分，迟早会撞上"哪一份是真的"。
2. **`human_score` 一列一律留空**（含已亲判的行）。`_apply_score_form` 只写填了分的行，
   所以留空 = 不动。把现值抄进表里让他"确认一遍"，等于给他 23 次无意义劳动，
   还给误改留口子；AI 代判过的分数也不抄进表——那正是他要独立判断的东西。

用法：`python scripts/build_owner_rejudge_queue.py`
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from harvest_anchors import ANCHOR_DIR, classify_human_source  # noqa: E402
from pm.cli.calibrate import _FORM_COLUMNS, _make_score_form  # noqa: E402

TARGET = ANCHOR_DIR / "samples.candidates.json"
FORM = ANCHOR_DIR / "score_form.owner_rejudge.csv"


def _score(item: dict[str, Any]) -> float:
    try:
        return float(item.get("human_score") or 0)
    except (TypeError, ValueError):
        return 0.0


def _rows(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, list) else (data.get("samples") or [])


def plan() -> tuple[list[str], dict[str, int]]:
    """要改判的 id 列表（最缺的格子排前面）+ 计数摘要。"""
    items = _rows(TARGET)
    todo = [it for it in items if classify_human_source(it) == "ai_proxy"]

    def rank(item: dict[str, Any]) -> int:
        score = _score(item)
        if score >= 8.0:  # 达标格最缺（owner 现在 10 条、>=9 只 1 条）
            return 0
        if score >= 6.0:
            return 1
        return 2

    todo.sort(key=lambda it: (rank(it), str(it.get("id"))))
    counts = {
        "todo": len(todo),
        "in_pass_band": sum(1 for it in todo if _score(it) >= 8.0),
        "skipped_owner": sum(1 for it in items if classify_human_source(it) == "owner"),
        "total_rows": len(items),
    }
    return [str(it.get("id")) for it in todo], counts


def build_form() -> int:
    if not TARGET.exists():
        print(f"锚点文件不存在：{TARGET}", file=sys.stderr)
        return 2
    todo_ids, counts = plan()
    order = {rid: i for i, rid in enumerate(todo_ids)}
    if not todo_ids:
        print("没有 AI 代判条目需要改判（全部已是所有者亲判）。", file=sys.stderr)
        return 0
    rc = _make_score_form(TARGET, FORM)
    if rc != 0:
        return rc

    with FORM.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    for row in rows:
        # human_score 一律清空：留空的行回填时不会被写（见模块 docstring 第 2 条）
        row["human_score"] = ""
        row["备注"] = (
            "【要你判】AI 代判曾给过分，但那是与评委同族的模型说的，不算人工锚点"
            if row.get("id") in set(todo_ids)
            else "【已亲判·不用填】留空即保持原样"
        )
    # 表的顺序就是他被摊到面前的顺序：最缺的格子排最前（plan() 已经排好）
    rows.sort(key=lambda r: (order.get(str(r.get("id")), len(order)), str(r.get("id"))))
    with FORM.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(_FORM_COLUMNS))
        writer.writeheader()
        writer.writerows(rows)

    print(f"打分表：{FORM}")
    print(
        f"  共 {counts['total_rows']} 行；要你判 {counts['todo']} 行"
        f"（其中 {counts['in_pass_band']} 行落在评委说的达标段），"
        f"{counts['skipped_owner']} 行留空即可。"
    )
    print("  填完回填（只有填了分的行会被写，且会被记为 owner 亲判）：")
    print(f"    python run.py calibrate --samples {TARGET} --apply-scores {FORM}")
    print("  之后看读数有没有从「不可估」翻过来：")
    print("    python run.py calibrate --aggregate")
    return 0


if __name__ == "__main__":
    raise SystemExit(build_form())
