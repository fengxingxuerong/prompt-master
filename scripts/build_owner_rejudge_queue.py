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

## 取样口径（2026-10-27 改，PE-4）

**改之前的缺陷（实测）**：排序键是 `human_score`（= AI 代判模型的分数）落在哪一段，
`>=8.0` 优先。于是队列把这 25 条里**离判定线最远**的先摊到面前：

| 排序口径 | 前 8 条的评委分 |
|---|---|
| 旧（按 `human_score` 分段） | 9.65 / 9.50 / 9.57 / 10.00 / 7.38 / 9.00 / 9.80 / 7.53 |
| 新（按离判定线距离） | 7.55 / 7.55 / 7.55 / 7.53 / 7.53 / 7.38 / 7.27 / 7.27 |

也就是说：**旧口径系统性跳过了判定线附近**，而那正是人工标注信息量最大、也最能定住
"评委到底准不准"的位置（边界样本上评委与代判的分歧最大：评委 7.55 / 代判 2.0、
评委 10.00 / 代判 7.0）。更根本的问题是**用被测变量自己选样本**——按分数选，再拿这批
样本去估评委在分数上的偏差，偏差估计就被选区带偏了。

**现在的口径**（照 jev-align 的做法，只抄取样，不抄它的 GEPA 优化）：

1. **不确定性优先**：评委分距 `PASS_THRESHOLD`（8.0）≤ `NEAR_MARGIN` 的排最前，
   越近越前——「该不该判达标」这个判决在边界上最不确定，标它最值。
2. **固定随机审计切片**：从其余条目里按 `sha256(盐 + id)` 抽 `AUDIT_SLICE` 条，
   排在边界样本之后。作用有二：防止锚点集**全**落在边界（那是另一种偏斜），
   以及保证「只填前几行」的人也能拿到一份不按分数挑过的样本。用哈希而不是 `random`
   是为了**重跑出同一张表**（不同 Python 版本的 `random` 序列不保证一致）。
3. 其余按离判定线距离排。

⚠️ 诚实边界：**池子本身是分带捞来的**（`harvest_anchors.py` 按评委分带取样），
所以"随机审计切片"只能消除**这一层再选**的偏斜，不能把整个锚点集变成无偏样本。

用法：`python scripts/build_owner_rejudge_queue.py`
"""

from __future__ import annotations

import csv
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from harvest_anchors import ANCHOR_DIR, classify_human_source  # noqa: E402
from pm.cli.calibrate import _FORM_COLUMNS, _make_score_form  # noqa: E402
from pm.schemas import PASS_THRESHOLD  # noqa: E402

TARGET = ANCHOR_DIR / "samples.candidates.json"
FORM = ANCHOR_DIR / "score_form.owner_rejudge.csv"

#: 判定线两侧这么近就算"边界样本"（评委自己的判决在这里最不确定）。
NEAR_MARGIN = 1.0
#: 从其余条目里固定抽这么多条做"随机审计切片"。
AUDIT_SLICE = 5
#: 审计切片的固定盐。**改它等于换一张表**：只在确实要重抽时才改，并写进台账。
AUDIT_SALT = "pm-owner-rejudge-v1"

#: 三种取样层，写进 `备注` 让人看得出这一行为什么被摊到面前。
STRATUM_NEAR = "判定线附近"
STRATUM_AUDIT = "随机审计切片"
STRATUM_REST = "其余"


def _score(item: dict[str, Any]) -> float:
    try:
        return float(item.get("human_score") or 0)
    except (TypeError, ValueError):
        return 0.0


def _judge_score(item: dict[str, Any]) -> float | None:
    """评委分（判定线 8.0 是**它自己**的阈值）；缺字段/坏值返回 None，不假装它在边界上。"""
    raw = (item.get("provenance") or {}).get("judge_score")
    if isinstance(raw, bool) or raw is None:  # bool 是 int 的子类，先挡掉
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str):
        try:
            return float(raw)
        except ValueError:
            return None
    return None


def _distance(item: dict[str, Any]) -> float:
    """离判定线的距离。评委分缺失的排最后（用一个大数，而不是 0 —— 0 会让它插到最前面）。"""
    j = _judge_score(item)
    return float("inf") if j is None else abs(j - PASS_THRESHOLD)


def _audit_key(item: dict[str, Any]) -> str:
    """确定性随机：`sha256(盐 + id)`。重跑同一份数据必须得到同一张表。

    不用 `random`：同一 seed 的序列**不保证跨 Python 版本一致**，
    那会让"昨天填了一半的表"和"今天重跑的表"顺序不同，回填时对不上号。
    """
    return hashlib.sha256(f"{AUDIT_SALT}:{item.get('id')}".encode()).hexdigest()


def _rows(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, list) else (data.get("samples") or [])


def plan() -> tuple[list[str], dict[str, int]]:
    """要改判的 id 列表（判定线附近 → 固定随机审计切片 → 其余）+ 计数摘要。"""
    items = _rows(TARGET)
    todo = [it for it in items if classify_human_source(it) == "ai_proxy"]

    near = [it for it in todo if _distance(it) <= NEAR_MARGIN]
    far = [it for it in todo if _distance(it) > NEAR_MARGIN]
    near.sort(key=lambda it: (_distance(it), str(it.get("id"))))

    audit = sorted(far, key=_audit_key)[:AUDIT_SLICE]
    audit_ids = {str(it.get("id")) for it in audit}
    rest = sorted(
        (it for it in far if str(it.get("id")) not in audit_ids),
        key=lambda it: (_distance(it), str(it.get("id"))),
    )
    ordered = near + audit + rest

    counts = {
        "todo": len(ordered),
        "near_boundary": len(near),
        "audit_slice": len(audit),
        "rest": len(rest),
        # 旧口径的读数保留下来：环比时还要看"落在代判达标段的还剩几条"
        "in_pass_band": sum(1 for it in todo if _score(it) >= PASS_THRESHOLD),
        "skipped_owner": sum(1 for it in items if classify_human_source(it) == "owner"),
        "total_rows": len(items),
    }
    return [str(it.get("id")) for it in ordered], counts


def _stratum_of(item_id: str, near_ids: set[str], audit_ids: set[str]) -> str:
    if item_id in near_ids:
        return STRATUM_NEAR
    if item_id in audit_ids:
        return STRATUM_AUDIT
    return STRATUM_REST


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
        # `_make_score_form` 的返回类型是 Any（它同时吃 argparse 的退出码），
        # 这里收成 int 再往外传，免得本函数的 int 契约被 Any 污染。
        return int(rc)

    with FORM.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    near_ids = set(todo_ids[: counts["near_boundary"]])
    audit_ids = set(
        todo_ids[counts["near_boundary"] : counts["near_boundary"] + counts["audit_slice"]]
    )
    by_id = {str(it.get("id")): it for it in _rows(TARGET)}
    for row in rows:
        # human_score 一律清空：留空的行回填时不会被写（见模块 docstring 第 2 条）
        row["human_score"] = ""
        rid = str(row.get("id"))
        if rid not in set(todo_ids):
            row["备注"] = "【已亲判·不用填】留空即保持原样"
            continue
        stratum = _stratum_of(rid, near_ids, audit_ids)
        judge = _judge_score(by_id.get(rid, {}))
        if stratum == STRATUM_NEAR:
            row["备注"] = (
                f"【要你判】{STRATUM_NEAR}：评委给 {judge:.2f}，离 {PASS_THRESHOLD:g} 只差 "
                f"{abs((judge or 0) - PASS_THRESHOLD):.2f} —— 这类样本最能定住评委准不准"
            )
        elif stratum == STRATUM_AUDIT:
            row["备注"] = (
                f"【要你判】{STRATUM_AUDIT}：从其余样本里按固定种子抽的，"
                "防锚点集全落在判定线附近（另一种偏斜）"
            )
        else:
            row["备注"] = "【要你判】AI 代判曾给过分，但那是与评委同族的模型说的，不算人工锚点"
    # 表的顺序就是他被摊到面前的顺序：边界样本 → 审计切片 → 其余（plan() 已经排好）
    rows.sort(key=lambda r: (order.get(str(r.get("id")), len(order)), str(r.get("id"))))
    with FORM.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(_FORM_COLUMNS))
        writer.writeheader()
        writer.writerows(rows)

    print(f"打分表：{FORM}")
    print(
        f"  共 {counts['total_rows']} 行；要你判 {counts['todo']} 行 —— "
        f"其中 {counts['near_boundary']} 行在判定线 ±{NEAR_MARGIN:g} 内（最值钱），"
        f"{counts['audit_slice']} 行是随机审计切片，{counts['rest']} 行其余；"
        f"{counts['skipped_owner']} 行留空即可。"
    )
    print(
        f"  （旧口径的读数留作环比：这 {counts['todo']} 行里有 "
        f"{counts['in_pass_band']} 行落在代判模型的达标段）"
    )
    print("  填完回填（只有填了分的行会被写，且会被记为 owner 亲判）：")
    print(f"    python run.py calibrate --samples {TARGET} --apply-scores {FORM}")
    print("  之后看读数有没有从「不可估」翻过来：")
    print("    python run.py calibrate --aggregate")
    return 0


if __name__ == "__main__":
    raise SystemExit(build_form())
