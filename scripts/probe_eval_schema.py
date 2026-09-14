"""复现并诊断 evaluate#2 的 schema 校验失败（第 7 轮 2 条错误）。

做法：从第 7 轮的 run json 里取出 case#2 的输入/输出/当时的提示词，
用与产品**完全相同**的渲染与调用路径重跑评委，观察：
  1) 失败率（结构化输出解析失败次数）
  2) 失败时模型实际返回了什么（缺哪些键）
"""

import json
import os
import sys
from pathlib import Path

ROOT = Path.cwd()
sys.path.insert(0, str(ROOT))

from pm.llm import _schema_hint, plain_call, structured_call  # noqa: E402
from pm.prompts import EVALUATOR_SYSTEM, EVALUATOR_USER, render  # noqa: E402
from pm.schemas import EvaluationResult  # noqa: E402

RUN = os.environ.get("PROBE_RUN", "logs/run_aa34bebb778e.json")
CASE = int(os.environ.get("PROBE_CASE", "2"))
N = int(os.environ.get("PROBE_N", "3"))

st = json.loads(Path(RUN).read_text(encoding="utf-8"))
st = st.get("final_state") or st
task = st.get("task", "")
run = next(r for r in st["test_runs"] if r["test_case_index"] == CASE)

user = render(
    EVALUATOR_USER,
    original_task=task,
    context="（无）",
    prompt=run["prompt"],
    test_input=run["test_input"],
    test_output=run["output"],
)

print(f"== 复现：case#{CASE} 评委调用 × {N}（run={RUN}）==")
ok = fail = 0
for i in range(1, N + 1):
    try:
        ev, meta = structured_call("evaluator", EvaluationResult, EVALUATOR_SYSTEM, user)
        ok += 1
        ds = ev.dimension_scores
        print(
            f"  #{i} OK channel={meta['channel']} attempts={meta['attempts']} "
            f"dims=({ds.task_completion},{ds.format_adherence},{ds.constraint_compliance},"
            f"{ds.robustness},{ds.quality})"
        )
    except Exception as e:  # noqa: BLE001
        fail += 1
        print(f"  #{i} FAIL {type(e).__name__}: {str(e)[:160]}")

print(f"\n结果：成功 {ok} / 失败 {fail}")

# 探针：直接看模型按 schema 要求返回了什么（定位缺键）
print("\n== 原始输出探针（通道 B 文本，看缺哪些键）==")
schema_block = (
    "\n\n<json_schema>\n"
    "你必须严格输出一个符合以下 JSON Schema 的 JSON 对象，且只输出该 JSON 对象，"
    "不要任何解释、不要 Markdown 围栏：\n"
    f"{_schema_hint(EvaluationResult)}\n"
    "</json_schema>"
)
text, _meta = plain_call("evaluator", EVALUATOR_SYSTEM + schema_block, user)
flat = " ".join(text.split())
print("  前 300 字:", flat[:300])
try:
    data = json.loads(text.strip().strip("`"))
    print("  顶层键:", sorted(data.keys()))
except Exception as e:  # noqa: BLE001
    print("  直接解析失败（可能带围栏/前后缀）：", type(e).__name__)
