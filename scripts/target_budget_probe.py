"""复核 `target` 角色的预算（4000）—— 它是上一轮复核时**漏掉**的角色。

发现的经过：2026-09-16 矩阵 A（客服分诊新任务域）跑出「基线 3.79 → 优化 9.81，
Δ=+6.02」，是历史最大正收益。但核对 run json 时发现
**基线 8 条里 4 条 output 为空（len=0）**，且：
    error=None      ← 没有异常
    latency=58~71s  ← 跑了很久
    target 预算=4000 ← 从未复核
这是 reasoning 吃满预算的典型特征（不抛异常，只返回空内容）。

若成立，则「Δ=+6.02」被夸大 —— 部分收益来自基线侧的空输出被判低分，
而不是优化真的那么有效。必须先量化这个偏差。

做法：用基线侧**完全相同的输入**（原始需求直喂 target），在 4000 / 8000 两个预算下
各跑 N 次，统计「非空返回」比例。

用法：
    TB_N=3 TB_BUDGETS=4000,8000 python scripts/target_budget_probe.py
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))

import pm.llm as L

N = int(os.environ.get("TB_N", "3"))
BUDGETS = [int(x) for x in os.environ.get("TB_BUDGETS", "4000,8000").split(",")]

st = json.loads(Path("logs/run_b48fa272462d.json").read_text(encoding="utf-8"))
st = st.get("final_state") or st
# 基线 = 原始需求直喂 target（正是出空输出的那条路径）
base_runs = st.get("baseline_runs", [])
if not base_runs:
    raise SystemExit("run json 里没有 baseline_runs")
PROMPT = base_runs[0]["prompt"]
TEST_INPUT = base_runs[0]["test_input"]

print("== target 预算对照：基线路径（原始需求直喂 target）==")
print(f"   预算组：{BUDGETS} × {N} 次")
print(f"   当前 MAX_TOKENS['target'] = {L.MAX_TOKENS['target']}")
print(f"   输入长度：prompt={len(PROMPT)} test_input={len(TEST_INPUT)}\n")

for budget in BUDGETS:
    nonempty = 0
    for i in range(1, N + 1):
        text, meta = L.plain_call(
            "target",
            PROMPT,
            TEST_INPUT,
            overrides={"max_tokens": budget},
        )
        n = len(text.strip())
        if n:
            nonempty += 1
        print(f"  budget={budget} #{i}: len={n:5d} {'非空' if n else '空!'}")
    print(f"  → max_tokens={budget}: 非空 {nonempty}/{N}\n")
