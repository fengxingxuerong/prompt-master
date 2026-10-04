"""给锚点文件补 `provenance.human_score_source`——把"哪几条人工分其实是 AI 代判的"变成机读事实。

背景（2026-10-03）：48 条候选锚点里 25 条不是产品所有者打的，是 AI 按所有者已确认判例外推的
（代判模型 GLM-5.3，与评委 B glm-5.2 同族）。这件事此前只写在提交说明 8af8c3b 与
`score_form.csv` 的备注栏里，锚点 JSON 里**没有机读位** ⇒ 交付报告那句"64 条人工锚点、
bias +3.09"拆不出严格人工那一半，读者会把"家族一致性"误读成"与人的偏差"。

判据来源就是那张打分表：备注以「AI代判」开头的条目 ⇒ `ai_proxy_glm-5.3`；
同文件里其余有 `human_score` 且 `confirmed` 的 ⇒ `owner`；拿不到的不动（读作 unknown，
**不把"没标注"读成"人打的"**）。`samples.example.json` 是演示数据，整份跳过。

跑法：
    python scripts/mark_anchor_provenance.py            # 只看会改什么（不落盘）
    python scripts/mark_anchor_provenance.py --apply    # 真写
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

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

DIR = ROOT / "judge_calibration"
FORM = DIR / "score_form.csv"
SKIP = {"samples.example.json"}  # 演示数据，不是尺子
AI_SOURCE = "ai_proxy_glm-5.3"
OWNER_SOURCE = "owner"


def ai_proxy_ids(form_path: Path = FORM) -> set[str]:
    """打分表备注栏里以「AI代判」开头的锚点 id。"""
    with open(form_path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    if not rows or "备注" not in rows[0]:
        raise SystemExit(f"{form_path} 没有备注列，判据读不出来")
    return {str(r["id"]).strip() for r in rows if str(r.get("备注") or "").startswith("AI代判")}


def stamp(rows: list[dict[str, Any]], ai: set[str]) -> dict[str, int]:
    """就地给行打出处位，返回各档计数（纯函数，方便用例直接喂假数据验）。"""
    counts = {AI_SOURCE: 0, OWNER_SOURCE: 0, "skipped": 0}
    for item in rows:
        if not isinstance(item, dict):
            continue
        sid = str(item.get("id") or "")
        prov = item.get("provenance")
        if not isinstance(prov, dict):
            prov = {}
            item["provenance"] = prov
        if sid in ai:
            prov["human_score_source"] = AI_SOURCE
            counts[AI_SOURCE] += 1
        elif item.get("human_score") is not None and item.get("confirmed", True):
            prov["human_score_source"] = OWNER_SOURCE
            counts[OWNER_SOURCE] += 1
        else:
            prov.pop("human_score_source", None)
            # 别留下空的 "provenance": {} —— 那是给数据文件加噪声，不是加信息
            if not prov and isinstance(item.get("provenance"), dict):
                del item["provenance"]
            counts["skipped"] += 1
    return counts


def main(argv: list[str]) -> int:
    apply = "--apply" in argv
    ai = ai_proxy_ids()
    print(f"打分表里 AI 代判 id：{len(ai)} 条")
    total = {AI_SOURCE: 0, OWNER_SOURCE: 0, "skipped": 0}
    for path in sorted(DIR.glob("samples*.json")):
        if path.name in SKIP:
            print(f"  跳过 {path.name}（演示数据）")
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        rows = data if isinstance(data, list) else data.get("samples")
        if not isinstance(rows, list):
            print(f"  跳过 {path.name}（结构不是列表）")
            continue
        counts = stamp(rows, ai)
        total = {k: total[k] + counts[k] for k in total}
        print(f"  {path.name}: ai_proxy={counts[AI_SOURCE]} owner={counts[OWNER_SOURCE]} 未标={counts['skipped']}")
        if apply:
            # 与 `run.py calibrate --apply-scores` 的写出格式逐字一致（同一把尺子的两个写入点
            # 各排各的版，下次回填就会产生"只有缩进变了"的假差异）
            text = json.dumps(data, ensure_ascii=False, indent=2)
            path.write_text(text, encoding="utf-8")
    print(
        f"合计 ai_proxy={total[AI_SOURCE]} owner={total[OWNER_SOURCE]} 未标={total['skipped']}"
        f"{'（已写盘）' if apply else '（未写盘，加 --apply）'}"
    )
    if total[AI_SOURCE] != len(ai):
        print(f"⚠️ 代判 {len(ai)} 个 id 里只有 {total[AI_SOURCE]} 个在锚点文件中出现——打分表与样本集可能已漂移")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
