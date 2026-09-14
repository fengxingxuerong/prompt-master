"""骨架收益的最小化验证：单次调用，看模型**首轮**是否平铺。

绕开重试逻辑（重试会掩盖首轮错误），直接 plain_call 一次，
统计"首轮就写对"的比例。这是 A/B 对照里 attempts 差异的根源。
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))
import pm.llm as L
from pm.prompts import EVALUATOR_SYSTEM, EVALUATOR_USER, render
from pm.schemas import EvaluationResult

N = int(os.environ.get("SE_N", "6"))
MODE = os.environ.get("SE_MODE", "old")  # old=旧提示块 / new=新提示块

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

if MODE == "old":
    block = (
        "\n\n<json_schema>\n你必须严格输出一个符合以下 JSON Schema 的 JSON 对象，"
        "且只输出该 JSON 对象，不要任何解释、不要 Markdown 围栏：\n"
        f"{L._schema_hint(EvaluationResult)}\n</json_schema>"
    )
else:
    block = L._schema_hint_block(EvaluationResult)

print(f"== 模式 {MODE} × {N} 次（单轮，看首轮是否平铺）==")
flat = ok = noparse = 0
for i in range(1, N + 1):
    text, _m = L.plain_call("evaluator", EVALUATOR_SYSTEM + block, user)
    data = L.extract_json_object(text)
    if data is None:
        noparse += 1
        print(f"  #{i} 无 JSON")
        continue
    try:
        EvaluationResult.model_validate(data)
        ok += 1
        print(f"  #{i} 首轮即通过")
    except Exception:  # noqa: BLE001 — 探针只需知道"是否校验通过"，任何异常都算不通过
        ds = EvaluationResult.model_fields["dimension_scores"]
        kids = L._model_required_fields(ds.annotation)
        if all(k in data for k in kids):
            flat += 1
            print(f"  #{i} 平铺（缺 dimension_scores 嵌套）")
        else:
            print(f"  #{i} 其他校验失败：{sorted(data)[:6]}")
print(f"\n模式 {MODE}: 首轮通过 {ok}/{N}  平铺 {flat}/{N}  无JSON {noparse}/{N}")
