"""规则层可靠性测量（PE-5）：把 `check` 的 7 条确定性规则与 `diff`/`gate` 的回归判决
放到版本化语料上量精确率 / 召回 / 误报率，供 `docs/rule-reliability.md` 落档。

设计约束（与 PE-5 的验收口径一致）：
- 零调用、零出网、确定性：全部判据取自 `pm.quality` / `pm.promptdiff`，与 `check`/`gate`
  同源，不另写一套判据；
- 语料是**版本化**的（`rule_reliability/rule_corpus.json` 入库），读数可复算；
- 语料标注（label）是"这条文本是否真有该缺陷"的人工判定，与实现无关 ——
  实现命中而 label 为假 = 误报（FP），label 为真而未命中 = 漏判（FN）。

用法：
    python scripts/measure_rule_reliability.py            # 打印 markdown 表（文档嵌这段）
    python scripts/measure_rule_reliability.py --json     # 结构化读数（守卫测试消费）

表内容会随实现或语料变化而变化；文档里的表是本脚本输出的**抄件**，
`tests/test_rule_reliability.py` 负责两份永远一致。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pm.bootstrap import ensure_utf8_stdio  # noqa: E402
from pm.promptdiff import diff_prompts  # noqa: E402
from pm.quality import check_prompt_quality  # noqa: E402

# 表里有 ✓/✗：中文 Windows 控制台（cp936）上不接这条引导，print 会当场崩
ensure_utf8_stdio()

CORPUS_PATH = ROOT / "rule_reliability" / "rule_corpus.json"

Codes = list[str]
Corpus = dict[str, Any]
Item = dict[str, Any]


def load_corpus(path: Path | None = None) -> Corpus:
    p = path or CORPUS_PATH
    data: Corpus = json.loads(p.read_text(encoding="utf-8"))
    return data


def fired_codes(text: str, known: Codes) -> set[str]:
    """该文本命中的规则 code 集合；语料清单之外的 code 直接抛错（新规则必须先补语料）。"""
    codes = {i.code for i in check_prompt_quality(text).issues}
    unknown = codes - set(known)
    if unknown:
        raise AssertionError(
            f"语料未覆盖的实现规则 code：{sorted(unknown)} —— 新增/改名规则后必须先补语料标注再发布"
        )
    return codes


def _safe(num: float | None) -> str:
    return "—" if num is None else f"{num:.3f}"


def _ratio(a: int, b: int) -> float | None:
    return None if b == 0 else a / b


def confusion(items: list[Item], code: str) -> dict[str, float | int | None]:
    tp = fp = fn = tn = 0
    for it in items:
        fired = code in it["_fired"]
        labeled = bool(it["labels"].get(code))
        if fired and labeled:
            tp += 1
        elif fired and not labeled:
            fp += 1
        elif not fired and labeled:
            fn += 1
        else:
            tn += 1
    precision = _ratio(tp, tp + fp)
    recall = _ratio(tp, tp + fn)
    fpr = _ratio(fp, fp + tn)
    f1: float | None
    if precision is None or recall is None or (precision + recall) == 0:
        f1 = None
    else:
        f1 = 2 * precision * recall / (precision + recall)
    return {
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
        "precision": precision,
        "recall": recall,
        "fpr": fpr,
        "f1": f1,
    }


def compute(corpus: Corpus) -> dict[str, Any]:
    items: list[Item] = []
    for it in corpus["items"]:
        row = dict(it)
        row["_fired"] = fired_codes(it["text"], corpus["codes"])
        items.append(row)
    real = [it for it in items if it["provenance"] == "run-log"]
    per_code: dict[str, Any] = {}
    for code in corpus["codes"]:
        overall = confusion(items, code)
        real_only = confusion(real, code)
        pos_real = sum(1 for it in real if it["labels"].get(code))
        pos_craft = sum(
            1 for it in items if it["provenance"] == "crafted" and it["labels"].get(code)
        )
        neg_real = sum(1 for it in real if not it["labels"].get(code))
        neg_craft = sum(
            1 for it in items if it["provenance"] == "crafted" and not it["labels"].get(code)
        )
        per_code[code] = {
            "overall": overall,
            "real_only": real_only,
            "pos_real": pos_real,
            "pos_crafted": pos_craft,
            "neg_real": neg_real,
            "neg_crafted": neg_craft,
        }
    pairs: list[dict[str, Any]] = []
    for pair in corpus["pairs"]:
        delta = diff_prompts(pair["before"], pair["after"])
        pairs.append(
            {
                "id": pair["id"],
                "expect": bool(pair["expect_regressed"]),
                "actual": bool(delta["regressed"]),
                "match": bool(pair["expect_regressed"]) == bool(delta["regressed"]),
                "summary": delta["summary"],
                "note": pair.get("note", ""),
            }
        )
    return {
        "items_total": len(items),
        "real_total": len(real),
        "crafted_total": len(items) - len(real),
        "per_code": per_code,
        "pairs": pairs,
        "pairs_match": sum(1 for p in pairs if p["match"]),
    }


def render_markdown(result: dict[str, Any], corpus: Corpus) -> str:
    lines: list[str] = []
    n_items = result["items_total"]
    n_real = result["real_total"]
    n_craft = result["crafted_total"]
    lines.append(
        f"语料 {n_items} 条 = 真实文本 {n_real} 条（run 日志全量去重，无一挑选）"
        f" + 对抗变体 {n_craft} 条 + 前后对 {len(result['pairs'])} 组（gate 层）。"
    )
    lines.append("")
    lines.append("### 表A · 主指标（全语料，label = 该文本是否真有此类缺陷）")
    lines.append("")
    lines.append("| 规则 | 正例 | 负例 | TP | FP | FN | 精确率 | 召回 | F1 |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for code in corpus["codes"]:
        d = result["per_code"][code]
        o = d["overall"]
        pos = d["pos_real"] + d["pos_crafted"]
        neg = d["neg_real"] + d["neg_crafted"]
        lines.append(
            f"| `{code}` | {pos} | {neg} | {o['tp']} | {o['fp']} | {o['fn']} "
            f"| {_safe(o['precision'])} | {_safe(o['recall'])} | {_safe(o['f1'])} |"
        )
    lines.append("")
    lines.append(
        "### 表B · 误报率拆解（对抗负例是刻意构造的「应该放行」，真实负例才是误报率的分母）"
    )
    lines.append("")
    lines.append(
        "| 规则 | FP(全体) | 负例(全体) | 误报率(全体) | FP(仅真实) | 负例(仅真实) | 误报率(仅真实) |"
    )
    lines.append("|---|---|---|---|---|---|---|")
    for code in corpus["codes"]:
        d = result["per_code"][code]
        o, r = d["overall"], d["real_only"]
        lines.append(
            f"| `{code}` | {o['fp']} | {d['neg_real'] + d['neg_crafted']} | {_safe(o['fpr'])} "
            f"| {r['fp']} | {d['neg_real']} | {_safe(r['fpr'])} |"
        )
    lines.append("")
    lines.append("### 表C · gate 层前后对（regressed 判决 vs 文档合同）")
    lines.append("")
    lines.append("| 对 | 期望 | 实际 | 一致 | 说明 |")
    lines.append("|---|---|---|---|---|")
    for p in result["pairs"]:
        lines.append(
            f"| {p['id']} | {'回归' if p['expect'] else '不回归'} | {'回归' if p['actual'] else '不回归'} "
            f"| {'✓' if p['match'] else '✗'} | {p['note']} |"
        )
    lines.append("")
    lines.append(f"前后对一致：**{result['pairs_match']}/{len(result['pairs'])}**。")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="规则层可靠性测量（零调用、确定性）")
    ap.add_argument("--json", action="store_true", help="输出 JSON（守卫测试消费）")
    ap.add_argument("--corpus", help="语料路径（默认 rule_reliability/rule_corpus.json）")
    args = ap.parse_args()
    corpus = load_corpus(Path(args.corpus) if args.corpus else None)
    result = compute(corpus)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=1))
    else:
        print(render_markdown(result, corpus))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
