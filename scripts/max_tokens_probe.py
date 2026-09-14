"""验证"空返回 = 思考型模型 reasoning 吃掉 max_tokens"假设。

第 9 轮/本轮实测：evaluator 主要失败模式是 **len=0 空内容**（3/6），
不是散文、不是围栏、不是截断。evaluator 代码默认 max_tokens=3500，
而 .env 给 evaluator_b 配了 8000 —— main 评委预算偏小。

做法：同一输入，在 max_tokens=3500 / 8000 下各跑 N 次，统计"非空返回"比例。
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))
import pm.llm as L
from pm.prompts import EVALUATOR_SYSTEM, EVALUATOR_USER, render
from pm.schemas import EvaluationResult

N = int(os.environ.get("MT_N", "6"))
BUDGETS = [int(x) for x in os.environ.get("MT_BUDGETS", "3500,8000").split(",")]

st = json.loads(Path("logs/run_4e225d1f82b3.json").read_text(encoding="utf-8"))
st = st.get("final_state") or st
run = next(r for r in st["test_runs"] if r["test_case_index"] == 3)
user = render(
    EVALUATOR_USER,
    original_task=st.get("task", ""),
    context="（无）",
    prompt=run["prompt"],
    test_input=run["test_input"],
    test_output=run["output"],
)
block = L._schema_hint_block(EvaluationResult)

print(f"== max_tokens 对照：{BUDGETS} × {N} 次 ==")
for budget in BUDGETS:
    nonempty = schema_ok = 0
    for i in range(1, N + 1):
        text, meta = L.plain_call(
            "evaluator", EVALUATOR_SYSTEM + block, user, overrides={"max_tokens": budget}
        )
        if text.strip():
            nonempty += 1
        data = L.extract_json_object(text)
        if data is not None:
            try:
                EvaluationResult.model_validate(data)
                schema_ok += 1
            except Exception:  # noqa: BLE001
                pass
        print(f"  budget={budget} #{i}: len={len(text):4d} {'非空' if text.strip() else '空!'}")
    print(f"  → max_tokens={budget}: 非空 {nonempty}/{N}  过schema {schema_ok}/{N}\n")
