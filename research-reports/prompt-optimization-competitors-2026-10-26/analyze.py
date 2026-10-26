#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""2026-10-26 竞品对照：把 data/*.json 汇总成可读表。

口径：
- 搜索命中来自 GitHub /search/repositories（匿名），star 数为抓取当时读数。
- Δ 基线 = research-reports/prompt-optimization-competitors-2026-10-13/report.md 的 2026-10-13 读数。
"""
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).parent
DATA = ROOT / "data"

# 2026-10-13 基线读数（来自上一份报告的一·当日坐标表）
BASE_1013 = {
    "stanfordnlp/dspy": 38601,
    "linshenkx/prompt-optimizer": 37735,
    "promptfoo/promptfoo": 25937,
    "confident-ai/deepeval": 18770,
    "openai/evals": 19591,
    "gepa-ai/gepa": 6955,
    "comet-ml/opik": 22530,
    "langfuse/langfuse": 35701,
    "Arize-ai/phoenix": 11808,
    "microsoft/PromptWizard": 4018,
    "zou-group/textgrad": 3758,
    "UKGovernmentBEIS/inspect_ai": 2989,
    "meta-llama/prompt-ops": 1037,
    "Kiln-AI/Kiln": 5190,
}

def load(name):
    p = DATA / name
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8-sig"))

def fmt_date(s):
    return (s or "")[:10]

def main():
    print("=" * 100)
    print("A. 单仓坐标实测（基线 14 家 + 本仓）")
    print("=" * 100)
    rows = []
    for f in sorted(DATA.glob("repo-*.json")):
        r = load(f.name)
        if not isinstance(r, dict) or "full_name" not in r:
            print(f"  ! {f.name}: 无数据")
            continue
        full = r["full_name"]
        lic = (r.get("license") or {}).get("spdx_id") or "NOASSERTION"
        base = BASE_1013.get(full)
        delta = "" if base is None else f"{r['stargazers_count'] - base:+,}"
        rows.append((r["stargazers_count"], full, delta, lic, fmt_date(r.get("pushed_at")),
                     fmt_date(r.get("created_at")), r.get("open_issues_count"),
                     (r.get("description") or "")[:70]))
    for s, full, delta, lic, pushed, created, oi, desc in sorted(rows, reverse=True):
        print(f"{s:>7}  {delta:>8}  {full:<34} {lic:<14} push={pushed} created={created} issues={oi}")
        print(f"         {desc}")

    print()
    print("=" * 100)
    print("B. 关键词搜索：prompt/eval 空间前列（star 降序，仅列前 25）")
    print("=" * 100)
    for name in ["s1-prompt-optimization.json", "s2-prompt-engineering.json",
                 "s3-llm-evaluation.json", "s4-evals-benchmark.json"]:
        d = load(name)
        if not d:
            print(f"  ! 缺 {name}")
            continue
        print(f"\n--- {name}  (total_count={d.get('total_count')}) ---")
        for it in d.get("items", [])[:25]:
            print(f"  {it['stargazers_count']:>7}  {it['full_name']:<40} push={fmt_date(it.get('pushed_at'))} "
                  f"created={fmt_date(it.get('created_at'))} lic={(it.get('license') or {}).get('spdx_id')}")
            print(f"           {(it.get('description') or '')[:110]}")

    print()
    print("=" * 100)
    print("C. 近一月新建（created>2026-09-26, stars>30）")
    print("=" * 100)
    d = load("s6-created-month.json")
    if d:
        print(f"total_count={d.get('total_count')}")
        for it in d.get("items", [])[:60]:
            print(f"  {it['stargazers_count']:>6}  {it['full_name']:<42} created={fmt_date(it.get('created_at'))} "
                  f"push={fmt_date(it.get('pushed_at'))}")
            print(f"          {(it.get('description') or '')[:110]}")

    print()
    print("=" * 100)
    print("D. 本周新建（created>2026-10-19, stars>5）")
    print("=" * 100)
    d = load("s7-created-week.json")
    if d:
        print(f"total_count={d.get('total_count')}")
        for it in d.get("items", [])[:60]:
            print(f"  {it['stargazers_count']:>6}  {it['full_name']:<42} created={fmt_date(it.get('created_at'))} "
                  f"push={fmt_date(it.get('pushed_at'))}")
            print(f"          {(it.get('description') or '')[:110]}")

    print()
    print("=" * 100)
    print("E. 本周有推送的高星 llm 仓（pushed>2026-10-19, stars>2000）")
    print("=" * 100)
    d = load("s8-pushed-week-hot.json")
    if d:
        print(f"total_count={d.get('total_count')}")
        for it in d.get("items", [])[:40]:
            print(f"  {it['stargazers_count']:>7}  {it['full_name']:<40} push={fmt_date(it.get('pushed_at'))}")
            print(f"          {(it.get('description') or '')[:110]}")

    print()
    print("=" * 100)
    print("F. topic / 专项搜索")
    print("=" * 100)
    for name in ["s5-judge.json", "s9-topic-prompt-opt.json", "s10-topic-llm-eval.json",
                 "s11-context-engineering.json", "s12-agent-skills.json",
                 "s13-prompt-cli-agent.json", "s14-prompt-lint.json"]:
        d = load(name)
        if not d:
            print(f"  ! 缺 {name}")
            continue
        print(f"\n--- {name} (total_count={d.get('total_count')}) ---")
        for it in d.get("items", [])[:18]:
            print(f"  {it['stargazers_count']:>7}  {it['full_name']:<40} push={fmt_date(it.get('pushed_at'))} "
                  f"created={fmt_date(it.get('created_at'))}")
            print(f"          {(it.get('description') or '')[:110]}")

    print()
    rl = load("rate-limit.json")
    if rl:
        print("rate_limit:", json.dumps(rl.get("resources", {}).get("core", {})))

if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()