"""复核四个"推算值"预算是否够用（clarifier / mockgen / comparator / optimizer）。

背景：2026-09-14 一次性上调了全部角色的 MAX_TOKENS。
其中 evaluator/evaluator_b/arbiter → 8000 **有直接对照**
（第 11 轮：同模型/端点/任务，调用次数同为 15 → 6000 失败 33%、8000 失败 0%）。
但 clarifier/comparator/mockgen/optimizer 是按「reasoning ≈ prompt × 1.5 + 正文余量」
**推算**出来的，当时明确标注了"待复核" —— 本脚本就是那次复核。

做法：对每个角色用**真实输入**走 `structured_call`（与产品完全相同的路径），
统计：
  · 原生通道成功率（有没有 LengthFinish / 降级）
  · completion_tokens 与 reasoning_tokens 的实测值
  · Peak reasoning ≈ 该角色真正需要的推理额度

用法：
    MR_N=3 python scripts/multi_role_budget_probe.py
    MR_ROLES=clarifier,comparator MR_N=3 python scripts/multi_role_budget_probe.py

端点（AMD 已死，用 sensenova 承接）：
    PM_BASE_URL=https://token.sensenova.cn/v1 PM_MODEL=deepseek-v4-pro \
    PM_API_KEY=sk-xxx MR_N=3 python scripts/multi_role_budget_probe.py
"""

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))

import pm.llm as L
from pm.prompts import (
    CLARIFIER_SYSTEM,
    CLARIFIER_USER,
    COMPARATOR_SYSTEM,
    COMPARATOR_USER,
    MOCKGEN_SYSTEM,
    MOCKGEN_USER,
    render,
)
from pm.schemas import (
    ClarificationResult,
    MockInputSet,
    PreferenceResult,
)

N = int(os.environ.get("MR_N", "3"))
ROLES = os.environ.get("MR_ROLES", "clarifier,mockgen,comparator").split(",")

st = json.loads(Path("logs/run_4e225d1f82b3.json").read_text(encoding="utf-8"))
st = st.get("final_state") or st
TASK = st.get("task", "")
RUN0 = next(r for r in st["test_runs"] if r["test_case_index"] == 0)


def _cases() -> dict[str, dict]:
    """每个角色的真实调用参数（system / user / schema 类），全部用真实 run 数据渲染。"""
    return {
        "clarifier": {
            "cls": ClarificationResult,
            "system": CLARIFIER_SYSTEM,
            "user": render(CLARIFIER_USER, task=TASK, context="（无）"),
        },
        "mockgen": {
            "cls": MockInputSet,
            "system": MOCKGEN_SYSTEM,
            "user": render(MOCKGEN_USER, prompt=RUN0["prompt"], n=3),
        },
        "comparator": {
            "cls": PreferenceResult,
            "system": COMPARATOR_SYSTEM,
            "user": render(
                COMPARATOR_USER,
                original_task=TASK,
                test_input=RUN0["test_input"],
                output_a=RUN0["output"],
                # 拿另一条用例的输出当候选 B，保证两侧都是真实文本
                output_b=st["test_runs"][1]["output"],
            ),
        },
    }


def _usage_of(text: str) -> tuple[int | None, int | None, int | None]:
    """从告警/异常文本里解 usage（探针拿不到对象，只能兜 text）。"""
    comp = re.search(r"completion_tokens=(\d+)", text)
    reas = re.search(r"reasoning_tokens=(\d+)", text)
    prom = re.search(r"prompt_tokens=(\d+)", text)
    g = lambda m: int(m.group(1)) if m else None  # noqa: E731
    return g(comp), g(reas), g(prom)


print(f"== 多角色预算复核：{ROLES} × {N} 次 ==")
print(f"   任务：{TASK[:40]}...\n")

cases = _cases()
for role in ROLES:
    budget = L.MAX_TOKENS.get(role, 0)
    print(f"--- {role}（当前预算 {budget}）---")
    if role not in cases:
        print("  跳过：本探针暂无该角色的真实用例构造\n")
        continue
    cfg = cases[role]
    ok = degr = 0
    for i in range(1, N + 1):
        try:
            _out, meta = L.structured_call(
                role, cfg["cls"], cfg["system"], cfg["user"], max_retries=1
            )
            ok += 1
            print(f"  #{i} OK channel={meta['channel']} attempts={meta['attempts']}")
        except Exception as e:  # noqa: BLE001
            degr += 1
            body = f"{type(e).__name__}: {e}"
            comp, reas, prom = _usage_of(body)
            kind = "额度耗尽" if L._is_length_finish_error(e) else "其他"
            extra = ""
            if comp:
                extra = f" completion={comp} reasoning={reas} prompt={prom}"
            print(f"  #{i} FAIL [{kind}]{extra}")
    print(f"  → 成功 {ok}/{N}  失败 {degr}/{N}")
    if degr:
        print(f"  ⚠️ 预算 {budget} 不足，建议上调")
    print()
