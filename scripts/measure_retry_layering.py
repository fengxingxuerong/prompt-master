"""量一次"卡死机"到底由几层重试叠出来：本地装死端点，数它真实收到几个请求。

零出网、不花钱：端点是 `127.0.0.1` 上一个故意不返回的 HTTP server，
每次请求挂 3s，而客户端 timeout=1s —— 于是"一发请求"必然超时，
端点收到的请求数就**直接等于重试层叠的倍数**。

为什么需要它（2026-10-03 的归因更正）：
    日志里那段 15 分钟空洞，先前被记成"读超时被 `_TRANSIENT_CONN_HINTS` 判成瞬时故障 ⇒
    我方原地重试 3 发"。实测是错的：真实异常文本是 `OpenAITimeoutError: Request timed out.`，
    旧 hint 里的 `readtimeout` / `read timeout` **永不命中**它。那 3 倍来自
    **openai SDK 的隐式 `max_retries=2`**（`openai._base_client.DEFAULT_MAX_RETRIES`），
    它不看代码想不到、日志里也不留痕。这个脚本就是把那层"看不见的重试"数出来。

比例尺：这里 timeout=1s，生产 `.env` 是 `PM_TIMEOUT=300` ⇒ 把每一发按 300 倍读。

跑法：
    python scripts/measure_retry_layering.py
    # 想换算成生产口径就加大比例尺（会明显更慢）：
    PM_PROBE_TIMEOUT=10 PM_PROBE_HANG=30 python scripts/measure_retry_layering.py

退出码：0 = 三档都测到了并且"修复后 ≤ 修复前"成立；1 = 排序反了或环境不允许绑端口。
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# 中文 Windows 上 stdout 缺省是 cp936：表头里的"⇒/≥"一重定向就 UnicodeEncodeError，
# 脚本还没出数就崩（入口层的编码问题，与被测逻辑无关）。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

TIMEOUT = float(os.environ.get("PM_PROBE_TIMEOUT", "1"))
HANG = float(os.environ.get("PM_PROBE_HANG", "3"))


class _HangingHandler(BaseHTTPRequestHandler):
    """故意比 timeout 更久才回，逼客户端超时；回的是合法 OpenAI 形状，不影响超时判定。"""

    protocol_version = "HTTP/1.1"
    hits: list[float] = []

    def do_POST(self) -> None:
        n = int(self.headers.get("content-length") or 0)
        self.rfile.read(n)
        type(self).hits.append(time.monotonic())
        time.sleep(HANG)
        body = json.dumps(
            {
                "id": "cmpl-1",
                "object": "chat.completion",
                "created": 1,
                "model": "probe",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
        ).encode()
        try:
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            pass  # 客户端早已超时断开，正是要量的形态

    def log_message(self, *_a: Any) -> None:
        pass


def measure(label: str, sdk_retries: int, budget: float, conn_retries: int) -> dict[str, Any]:
    """按给定的三层配置装一次死，返回"端点收到几发 / 墙钟几秒"。"""
    os.environ["PM_SDK_RETRIES"] = str(sdk_retries)
    os.environ["PM_CALL_BUDGET"] = str(budget)
    os.environ["PM_CONN_RETRIES"] = str(conn_retries)
    os.environ["PM_TIMEOUT"] = str(int(TIMEOUT))
    # 干净检出口径：本机 .env 里 PM_FORCE_JSON_CHANNEL=1 会跳过通道 A，量出来的层叠就少了半条链
    os.environ["PM_FORCE_JSON_CHANNEL"] = "0"
    os.environ.pop("PM_JUDGE_JITTER", None)

    import importlib

    import pm.llm as L

    importlib.reload(L)

    _HangingHandler.hits = []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _HangingHandler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    os.environ["PM_BASE_URL"] = f"http://127.0.0.1:{port}/v1"
    importlib.reload(L)  # 端点要换过来
    from pm.schemas import EvaluationResult

    t0 = time.monotonic()
    err = ""
    try:
        L.structured_call("evaluator", EvaluationResult, "sys", "只输出 JSON")
    except Exception as e:  # noqa: BLE001 - 量的就是失败形态
        err = f"{type(e).__name__}"
    wall = time.monotonic() - t0
    srv.shutdown()
    srv.server_close()

    hits = len(_HangingHandler.hits)
    return {
        "label": label,
        "sdk": L.SDK_RETRIES,
        "conn": L.TRANSIENT_CONN_RETRIES,
        "budget": L.CALL_BUDGET,
        "hits": hits,
        "wall_s": round(wall, 2),
        "as_prod": round(wall * (300.0 / TIMEOUT)),
        "err": err,
    }


def main() -> int:
    print(
        f"装死端点：每发挂 {HANG}s，客户端 timeout={TIMEOUT}s"
        f"（生产 PM_TIMEOUT=300 ⇒ as_prod 列按 {300.0 / TIMEOUT:g}× 换算）\n"
    )
    rows = [
        # 修复前的层叠：SDK 隐式重试 2 次 + 没有墙钟预算
        measure("A 修复前：SDK 隐式 2 + 无预算", 2, 0.0, 2),
        # 只钉住 SDK 层，预算仍关：看"看不见的那一层"单独值多少
        measure("C 只关 SDK 隐式层", 0, 0.0, 2),
        # 现行缺省：SDK 0 + 墙钟预算（比例尺 3s ≈ 生产 900s 档）
        measure("B 现行缺省：SDK 0 + 墙钟预算", 0, 3.0 * TIMEOUT, 2),
    ]
    print(f"{'配置':<30} {'SDK':>4} {'连接重试':>8} {'预算s':>7} {'端点收到':>8} {'墙钟s':>7} {'生产口径s':>9}")
    for r in rows:
        print(
            f"{r['label']:<30} {r['sdk']:>4} {r['conn']:>8} {r['budget']:>7.0f} "
            f"{r['hits']:>8} {r['wall_s']:>7.2f} {r['as_prod']:>9}"
        )
    # 先验"壳子还活着"：A 档（SDK 隐式 2 + 我方连接重试 2、无预算）按设计至少该被数到 3 发。
    # 端点数到 0 只说明**探针没看见**，不代表重试消失了——而下面那条排序判据是
    # `A ≥ C ≥ B`，在 hits 全 0 时会以 0 ≤ 0 ≤ 0 通过（2026-10-05 实测：本机 HTTP 客户端
    # 连不上 127.0.0.1 的自建端口，三档 hits 全 0，脚本照样报"层叠逐层收敛"）。
    # 没有这道前置检查，探针失效就会输出一个看起来最像"修好了"的结论——比没有探针更坏。
    control = rows[0]["hits"]
    if control < 3:
        print(
            f"\n探针失效：A 档端点只数到 {control} 次（按设计应 ≥3）——"
            "计数看不见请求，这张表的排序就没有任何意义。\n"
            "先修本地实证环境（能否连上 127.0.0.1 上的自建端口），别引用本次任何数字。"
        )
        return 2
    fixed = rows[2]["hits"] <= rows[1]["hits"] <= rows[0]["hits"]
    print(
        f"\n判读：{'A ≥ C ≥ B 成立 ⇒ 层叠逐层收敛' if fixed else '排序反了——隐式层或预算有一处失效'}"
        f"；三档最后一次错误分别是 {', '.join(r['err'] or 'ok' for r in rows)}"
    )
    print("注意：B 用 CallBudgetExceeded 收手，A/C 报的是原始超时——同一个'失败'，可执行性不同。")
    return 0 if fixed else 1


if __name__ == "__main__":
    sys.exit(main())
