# 顶档门槛校准的端点预检：用真实 checklist 渲染形态，对照 max_tokens 两档的耗时与输出质量。
# 产物：stdout 打印对照结果，不落任何账本/缓存。
import json
import time
import urllib.error
import urllib.request

from dotenv import dotenv_values
from pm.calibration import EVALUATOR_USER
from pm.prompts import EVALUATOR_SYSTEM_CHECKLIST, render
from pm.scoring import prepare

env = dotenv_values(".env")
KEY = env.get("PM_API_KEY_NVIDIA", "")
URL = "https://integrate.api.nvidia.com/v1/chat/completions"
MODEL = "z-ai/glm-5.3-flash"

samples = json.load(open("judge_calibration/samples.ab.json", encoding="utf-8"))
anchor = next(s for s in samples if s.get("test_output"))  # 取第一条有输出的真锚点

user_base = render(
    EVALUATOR_USER,
    original_task=anchor["original_task"],
    context=anchor.get("context") or "（无）",
    prompt=anchor["prompt"],
    test_input=anchor["test_input"],
    test_output=anchor["test_output"],
)
prep = prepare(
    anchor["prompt"], anchor["original_task"], anchor["test_input"], anchor["test_output"]
)
checklist, numbers, block = prep
user_prompt = user_base + block


def call(max_tokens: int):
    body = json.dumps(
        {
            "model": MODEL,
            "messages": [
                {"role": "system", "content": EVALUATOR_SYSTEM_CHECKLIST},
                {"role": "user", "content": user_prompt},
            ],
            "max_tokens": max_tokens,
        }
    ).encode()
    req = urllib.request.Request(
        URL,
        data=body,
        headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=240) as r:
            d = json.loads(r.read())
            msg = d["choices"][0]["message"]
            content = (msg.get("content") or "").strip()
            rc = msg.get("reasoning_content") or ""
            usage = d.get("usage", {})
            ok_json = content.startswith("{") and "quality_band" in content
            print(
                f"max_tokens={max_tokens}: {time.time() - t0:.0f}s | 正文 {len(content)} 字 | "
                f"reasoning {len(rc)} 字 | usage={usage.get('completion_tokens')} tok | 合法JSON: {ok_json}"
            )
            print("  正文头 120 字:", content[:120].replace("\n", " "))
    except urllib.error.HTTPError as e:
        print(
            f"max_tokens={max_tokens}: HTTP {e.code} ({time.time() - t0:.0f}s) {e.read()[:120].decode(errors='replace')}"
        )
    except Exception as e:  # noqa: BLE001 预检探针：任何失败都要打印后继续，不能整轮作废
        print(
            f"max_tokens={max_tokens}: {type(e).__name__} ({time.time() - t0:.0f}s) {str(e)[:120]}"
        )


print(
    f"锚点 prompt {len(anchor['prompt'])} 字 | 输出 {len(anchor['test_output'])} 字 | 清单 {len(checklist)} 条 | user_prompt 共 {len(user_prompt)} 字"
)
print(f"system prompt {len(EVALUATOR_SYSTEM_CHECKLIST)} 字")
call(24000)
time.sleep(3)
call(8000)
