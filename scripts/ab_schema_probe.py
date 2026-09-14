"""A/B/C 对照：定位「评委 schema 成功率 25% → 高成功率」到底是谁的功劳。

第 9 轮实测：`evaluate` 在通道 B（文本 JSON）下过 schema 成功率仅 25%，
根因是模型把嵌套字段 `dimension_scores` 的**子键平铺到顶层**，导致 3 个字段 missing。
本轮做了两处修改，本脚本把收益拆开归因（其余条件完全相同）：

  A 旧提示块（只有机器生成的 JSON Schema）+ 禁形状修复   = 修复前基线
  B 新提示块（可照抄骨架在前）+ 禁形状修复               = 只吃骨架收益
  C 新提示块 + 形状修复                                  = 完整修复

用法：
    AB_GROUP=A AB_N=6 python scripts/ab_schema_probe.py
    AB_GROUP=B AB_N=6 python scripts/ab_schema_probe.py
    AB_GROUP=C AB_N=6 python scripts/ab_schema_probe.py

端点（main 端点已下线时用 sensenova 承接 evaluator 角色）：
    PM_EVALUATOR_BASE_URL=https://token.sensenova.cn/v1 \
    PM_EVALUATOR_MODEL=deepseek-v4-pro PM_EVALUATOR_API_KEY=sk-xxx \
      AB_GROUP=A AB_N=6 python scripts/ab_schema_probe.py
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))

import pm.llm as L
from pm.prompts import EVALUATOR_SYSTEM, EVALUATOR_USER, render
from pm.schemas import EvaluationResult

GROUP = os.environ.get("AB_GROUP", "A")
N = int(os.environ.get("AB_N", "6"))
RUN = os.environ.get("AB_RUN", "logs/run_4e225d1f82b3.json")
CASE = int(os.environ.get("AB_CASE", "3"))

st = json.loads(Path(RUN).read_text(encoding="utf-8"))
st = st.get("final_state") or st
run = next(r for r in st["test_runs"] if r["test_case_index"] == CASE)
user = render(
    EVALUATOR_USER,
    original_task=st.get("task", ""),
    context="（无）",
    prompt=run["prompt"],
    test_input=run["test_input"],
    test_output=run["output"],
)

# 旧提示块 = 修复前的写法（只有机器生成的 JSON Schema，无骨架）
OLD_BLOCK = (
    "\n\n<json_schema>\n"
    "你必须严格输出一个符合以下 JSON Schema 的 JSON 对象，"
    "且只输出该 JSON 对象，不要任何解释、不要 Markdown 围栏：\n"
    f"{L._schema_hint(EvaluationResult)}\n</json_schema>"
)

if GROUP == "A":
    L._schema_hint_block = lambda m: OLD_BLOCK  # type: ignore[assignment]
    L._repair_shape = lambda m, d: d  # type: ignore[assignment]
elif GROUP == "B":
    L._repair_shape = lambda m, d: d  # type: ignore[assignment]
elif GROUP != "C":
    raise SystemExit(f"未知 AB_GROUP={GROUP!r}（可选 A / B / C）")

os.environ["PM_FORCE_JSON_CHANNEL"] = "1"  # 全部走通道 B（出事的通道）

print(f"== 组 {GROUP} × {N} 次（通道 B，case#{CASE}）==")
ok = 0
attempts_total = 0
for i in range(1, N + 1):
    try:
        ev, meta = L.structured_call("evaluator", EvaluationResult, EVALUATOR_SYSTEM, user)
        ok += 1
        attempts_total += meta["attempts"]
        print(f"  #{i} OK attempts={meta['attempts']} quality={ev.dimension_scores.quality}")
    except Exception as e:  # noqa: BLE001
        print(f"  #{i} FAIL {type(e).__name__}")
        attempts_total += 3  # 失败即耗尽全部重试

print(f"\n组 {GROUP}: 成功 {ok}/{N}  平均尝试次数 {attempts_total / N:.2f}")
