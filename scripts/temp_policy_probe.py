"""温度策略对照：解析失败后「降温」vs「升温」，谁更容易恢复？

背景（第 9 轮实测，4 次重复出现的稳定三段式）：
    第 1 次失败：ValidationError  —— 拿到了 JSON，但不符 schema
    第 2 次失败：ValueError       —— 升温后连 JSON 对象都找不到
    第 3 次失败：ValueError
怀疑「升温让输出发散，反而把"schema 不符"恶化成"无 JSON"」。
注意：第 9 轮伴随高频限流，单看日志无法排除干扰 —— 所以必须做可控对照。

做法：同一 case 输入、同一提示词（EVALUATOR_SYSTEM + schema hint + EVALUATOR_USER），
在不同温度下各跑 N 次，分别统计：
    · 有 JSON 对象（能 json.loads）
    · 过 schema（EvaluationResult 校验通过）

用法：
    PROBE_RUN=logs/run_4e225d1f82b3.json PROBE_CASE=3 \
    PROBE_N=4 PROBE_TEMPS=0.0,0.1,0.2,0.3 \
    python scripts/temp_policy_probe.py
"""

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path.cwd()
sys.path.insert(0, str(ROOT))

from pm.llm import _schema_hint, plain_call  # noqa: E402
from pm.prompts import EVALUATOR_SYSTEM, EVALUATOR_USER, render  # noqa: E402
from pm.schemas import EvaluationResult  # noqa: E402

RUN = os.environ.get("PROBE_RUN", "logs/run_4e225d1f82b3.json")
CASE = int(os.environ.get("PROBE_CASE", "3"))
N = int(os.environ.get("PROBE_N", "4"))
TEMPS = [float(x) for x in os.environ.get("PROBE_TEMPS", "0.0,0.1,0.2,0.3").split(",")]

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

schema_block = (
    "\n\n<json_schema>\n"
    "你必须严格输出一个符合以下 JSON Schema 的 JSON 对象，且只输出该 JSON 对象，"
    "不要任何解释、不要 Markdown 围栏：\n"
    f"{_schema_hint(EvaluationResult)}\n"
    "</json_schema>"
)
system = EVALUATOR_SYSTEM + schema_block


def try_parse(text: str) -> tuple[bool, bool]:
    """返回 (是否有合法 JSON 对象, 是否通过 schema 校验)。"""
    t = text.strip()
    if t.startswith("```"):
        body = t[3:]
        t = body.split("```", 1)[0] if "```" in body else body
        t = t.strip()
        if t[:4].lower() == "json":
            t = t[4:].strip()
    try:
        data = json.loads(t)
    except Exception:  # noqa: BLE001 — 探针要的就是"能不能解析"，任何异常都算解析失败
        return False, False
    try:
        EvaluationResult.model_validate(data)
        return True, True
    except Exception:  # noqa: BLE001 — 同上，任何校验异常都算 schema 不符
        return True, False


print(f"== 温度策略对照：case#{CASE} × {N} 次 × 温度 {TEMPS} ==")
print(f"   run={RUN}\n")

rows: list[tuple[float, int, int]] = []
for temp in TEMPS:
    n_json = n_schema = 0
    for i in range(1, N + 1):
        t0 = time.time()
        try:
            text, _meta = plain_call("evaluator", system, user, overrides={"temperature": temp})
            has_json, ok_schema = try_parse(text)
            n_json += int(has_json)
            n_schema += int(ok_schema)
            flag = "OK  " if ok_schema else ("JSON" if has_json else "FAIL")
            print(f"  temp={temp} #{i}: {flag} ({time.time() - t0:.0f}s)")
        except Exception as e:  # noqa: BLE001
            print(f"  temp={temp} #{i}: EXC {type(e).__name__}: {str(e)[:80]}")
        time.sleep(2)
    rows.append((temp, n_json, n_schema))
    print()

print("== 汇总 ==")
print(f"{'温度':>6} | {'有 JSON':>9} | {'过 schema':>10}")
for temp, a, b in rows:
    print(f"{temp:>6} | {a:>4}/{N:<4} | {b:>5}/{N:<4}")
