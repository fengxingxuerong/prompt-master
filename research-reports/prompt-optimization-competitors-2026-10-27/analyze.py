#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Round 6 (2026-10-27) aggregation: 24h deltas vs the round-5 (2026-10-26) baseline.

Baseline numbers are the 2026-10-26 readings recorded in
`research-reports/prompt-optimization-competitors-2026-10-26/report.md` §一 and §二.
"""
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).parent
DATA = ROOT / "data"

BASE_1026 = {
    "stanfordnlp/dspy": 38790,
    "linshenkx/prompt-optimizer": 39241,
    "langfuse/langfuse": 36486,
    "promptfoo/promptfoo": 26429,
    "comet-ml/opik": 22925,
    "openai/evals": 19654,
    "confident-ai/deepeval": 19104,
    "Arize-ai/phoenix": 12004,
    "gepa-ai/gepa": 7148,
    "Kiln-AI/Kiln": 5233,
    "microsoft/PromptWizard": 4022,
    "zou-group/textgrad": 3770,
    "UKGovernmentBEIS/inspect_ai": 3094,
    "meta-llama/prompt-ops": 1027,
    "fengxingxuerong/prompt-master": 0,
    # round-5 new entrants
    "ifixai-ai/iFixAi": 34502,
    "Q00/ouroboros": 6275,
    "Tencent/AI-Infra-Guard": 7091,
    "truera/trulens": 3611,
    "hashgraph-online/hol-guard": 996,
    "sutro-sh/jev-align": 311,
}

# 与本产品同题的关键词：判断"新面孔"是否值得看
RELEVANT = re.compile(
    r"(prompt|eval|judge|bench|test|assert|lint|guard|skill|rubric|score|"
    r"calibrat|audit|trajectory|trace)",
    re.I,
)


def load(name):
    p = DATA / name
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8-sig"))


def main():
    print("=" * 96)
    print("A. 24h 坐标（Δ 基线 = 第五轮 2026-10-26 读数）")
    print("=" * 96)
    rows = []
    for f in sorted(DATA.glob("repo-*.json")):
        r = load(f.name)
        if not isinstance(r, dict) or "full_name" not in r:
            print(f"  ! {f.name}: 无数据")
            continue
        full = r["full_name"]
        base = BASE_1026.get(full)
        delta = "" if base is None else f"{r['stargazers_count'] - base:+,}"
        lic = (r.get("license") or {}).get("spdx_id") or "NOASSERTION"
        rows.append(
            (
                r["stargazers_count"],
                full,
                delta,
                lic,
                (r.get("pushed_at") or "")[:16].replace("T", " "),
                (r.get("created_at") or "")[:10],
                r.get("open_issues_count"),
                (r.get("description") or "")[:64],
            )
        )
    for stars, full, delta, lic, pushed, created, issues, desc in sorted(rows, reverse=True):
        print(f"{stars:>7} {delta:>7}  {full:<32} {lic:<13} push={pushed} created={created} iss={issues}")
        print(f"        {desc}")

    print()
    print("=" * 96)
    print("B. 本轮新建（created>2026-10-19，star 降序，只列同题相关的前 25）")
    print("=" * 96)
    for name in ("s1-created-since-round5.json", "s2-created-last-48h.json"):
        d = load(name)
        if not d:
            print(f"  ! 缺 {name}")
            continue
        print(f"\n--- {name} (total={d.get('total_count')}) ---")
        shown = 0
        for it in d.get("items", []):
            desc = it.get("description") or ""
            if not RELEVANT.search(it["full_name"] + " " + desc):
                continue
            print(f"  {it['stargazers_count']:>6} {it['full_name']:<40} created={(it.get('created_at') or '')[:10]}")
            print(f"         {desc[:100]}")
            shown += 1
            if shown >= 25:
                break

    print()
    print("=" * 96)
    print("C. 同题搜索前列（star 降序，各取前 12，只列与评测/提示词/技能相关的）")
    print("=" * 96)
    for name in (
        "s3-prompt-optimization.json",
        "s4-llm-evaluation.json",
        "s5-agent-skills.json",
        "s6-judge-calibration.json",
    ):
        d = load(name)
        if not d:
            print(f"  ! 缺 {name}")
            continue
        print(f"\n--- {name} (total={d.get('total_count')}) ---")
        shown = 0
        for it in d.get("items", []):
            desc = it.get("description") or ""
            if not RELEVANT.search(it["full_name"] + " " + desc):
                continue
            base = BASE_1026.get(it["full_name"])
            delta = "" if base is None else f"({it['stargazers_count'] - base:+,})"
            print(f"  {it['stargazers_count']:>7}{delta:>9} {it['full_name']:<40} push={(it.get('pushed_at') or '')[:10]}")
            print(f"           {desc[:96]}")
            shown += 1
            if shown >= 12:
                break

    print()
    d = load("s7-pushed-this-week.json")
    if d:
        print("=" * 96)
        print(f"D. 本周有推送的高星 llm 仓（total={d.get('total_count')}，前 15）")
        print("=" * 96)
        for it in d.get("items", [])[:15]:
            print(f"  {it['stargazers_count']:>7} {it['full_name']:<38} push={(it.get('pushed_at') or '')[:10]}")
            print(f"           {(it.get('description') or '')[:92]}")

    rl = load("rate-limit.json")
    if rl:
        core = rl.get("resources", {}).get("core", {})
        print(f"\nrate_limit core: {core.get('remaining')}/{core.get('limit')}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()