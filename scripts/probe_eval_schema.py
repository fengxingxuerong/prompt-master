"""复现并诊断 evaluate 的 schema 校验失败（第 7 轮 2 条错误 / 第 9 轮 25% 成功率）。

做法：从 run json 里取出指定 case 的输入/输出/当时的提示词，
用与产品**完全相同**的渲染与调用路径重跑评委，观察：
  1) 失败率（结构化输出解析失败次数）
  2) 失败时模型实际返回了什么（缺哪些键 / 什么形状）

第 9 轮的用法（验证"骨架 + 形状修复"是否把 25% 拉起来）：
    PROBE_RUN=logs/run_4e225d1f82b3.json PROBE_CASE=3 PROBE_N=4 \
    PROBE_CHANNEL_B=1 python scripts/probe_eval_schema.py

  PROBE_CHANNEL_B=1              强制走通道 B（文本 JSON），复现第 9 轮的失败场景
  PROBE_NO_REPAIR=1              禁用形状修复，用于 A/B 对照（证明修复真有用）
"""

import json
import os
import sys
from pathlib import Path

ROOT = Path.cwd()
sys.path.insert(0, str(ROOT))

from pm.llm import _schema_hint_block, plain_call, structured_call  # noqa: E402
from pm.prompts import EVALUATOR_SYSTEM, EVALUATOR_USER, render  # noqa: E402
from pm.schemas import EvaluationResult  # noqa: E402

RUN = os.environ.get("PROBE_RUN", "logs/run_aa34bebb778e.json")
CASE = int(os.environ.get("PROBE_CASE", "2"))
N = int(os.environ.get("PROBE_N", "3"))

# 强制走通道 B（文本 JSON）：第 9 轮的失败都发生在这条通道上，
# 不强制的话端点的原生结构化输出（通道 A）会先兜住，复现不出来。
if os.environ.get("PROBE_CHANNEL_B", "0") not in ("", "0"):
    os.environ["PM_FORCE_JSON_CHANNEL"] = "1"

# A/B 对照用：临时禁用形状修复（证明"骨架 + 修复"各自贡献了多少）
if os.environ.get("PROBE_NO_REPAIR", "0") not in ("", "0"):
    import pm.llm as _L

    _L._repair_shape = lambda model, data: data  # type: ignore[assignment]

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
print(
    f"   通道B强制={os.environ.get('PROBE_CHANNEL_B', '0')}  禁形状修复={os.environ.get('PROBE_NO_REPAIR', '0')}"
)
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

# 探针：直接看模型按 schema 要求返回了什么（定位缺键 / 看形状）
print("\n== 原始输出探针（通道 B 文本，看缺哪些键 / 有没有平铺）==")
schema_block = _schema_hint_block(EvaluationResult)
text, _meta = plain_call("evaluator", EVALUATOR_SYSTEM + schema_block, user)
flat = " ".join(text.split())
print("  前 300 字:", flat[:300])
try:
    data = json.loads(text.strip().strip("`"))
    print("  顶层键:", sorted(data.keys()))
    try:
        EvaluationResult.model_validate(data)
        print("  直接校验: 通过")
    except Exception as e:  # noqa: BLE001
        print("  直接校验: 失败 →", str(e).splitlines()[0])
        # 试试形状修复能不能救
        from pm.llm import _repair_shape

        fixed = _repair_shape(EvaluationResult, data)
        try:
            EvaluationResult.model_validate(fixed)
            print("  形状修复后: 通过（本可被代码救回）")
        except Exception as e2:  # noqa: BLE001
            print("  形状修复后: 仍失败 →", str(e2).splitlines()[0])
except Exception as e:  # noqa: BLE001
    print("  直接解析失败（可能带围栏/前后缀）：", type(e).__name__)
