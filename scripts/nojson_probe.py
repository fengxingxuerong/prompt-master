"""看"无 JSON"时模型到底返回了什么（第 9 轮 3/6~5/6 的主要失败模式）。

三种可能，对应完全不同的修法：
  a) 返回了散文/解释 → 指令遵循问题，要强化"只输出 JSON"
  b) 返回了带围栏的 JSON → extract_json_object 的抽取能力不足
  c) 返回了截断的 JSON（max_tokens 不够）→ 调大预算
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))
import pm.llm as L
from pm.prompts import EVALUATOR_SYSTEM, EVALUATOR_USER, render
from pm.schemas import EvaluationResult

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
print("== 采集无 JSON 的原始返回 ==")
for i in range(1, 7):
    text, meta = L.plain_call("evaluator", EVALUATOR_SYSTEM + block, user)
    data = L.extract_json_object(text)
    tag = "OK" if data is not None else "NOJSON"
    print(f"\n--- #{i} [{tag}] len={len(text)} finish={meta.get('finish_reason', '?')} ---")
    if data is None:
        print("原文前 400 字:", " ".join(text.split())[:400])
        print("原文末 200 字:", " ".join(text.split())[-200:])
        print(
            "含围栏:",
            "```" in text,
            "| 含花括号:",
            "{" in text,
            "| 含 dimension_scores:",
            "dimension_scores" in text,
        )
