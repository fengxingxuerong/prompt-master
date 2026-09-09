"""
本地 OpenAI 兼容桩服务 —— 用于验证**真实 HTTP 集成链路**。

它与 tests/ 里的 fake backend 有本质区别：
- fake backend 直接替换 Python 函数，只验证图拓扑；
- 本服务是真实监听端口的 HTTP 服务，走完整的
  ChatOpenAI 客户端 → HTTP 请求 → 响应解析 → Pydantic 校验 链路，
  能暴露 SDK 用法、请求体构造、响应解析、降级通道等真实问题。

它能证明"集成链路通畅"，但**不能**证明提示词优化效果好坏
（桩服务返回的终究是固定内容，模型能力必须由真实 API 验证）。

启动：
    python examples/openai_stub_server.py --port 8123
    # 加 --tools 可模拟支持 function calling 的端点。
    # 注意：langchain-openai 默认走 json_schema（请求体不带 tools），
    # 客户端需同时设 PM_STRUCT_METHOD=function_calling 才会真正走 tools 通道（A10）：
    #   PM_STRUCT_METHOD=function_calling python examples/openai_stub_server.py --port 8123 --tools
    python examples/openai_stub_server.py --port 8123 --tools
"""

from __future__ import annotations

import argparse
import json
import time

from fastapi import FastAPI, Request

app = FastAPI()

SUPPORT_TOOLS = False
UNCOOPERATIVE = False  # 模拟"声明 JSON 模式却仍返回多余文本"的模型


def _reply(content: str | None, tool_call: dict | None = None) -> dict:
    message: dict = {"role": "assistant", "content": content}
    # 真实 API 在返回 tool_calls 时 finish_reason 为 "tool_calls"，
    # 若仍写 "stop"，langchain 会解析不到工具调用 —— 这里如实模拟
    finish = "stop"
    if tool_call:
        message["content"] = None
        message["tool_calls"] = [tool_call]
        finish = "tool_calls"
    return {
        "id": f"chatcmpl-stub-{int(time.time())}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": "stub-model",
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
    }


def _tool(name: str, payload: dict) -> dict:
    return {
        "id": f"call_{name}",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(payload, ensure_ascii=False)},
    }


def _system_of(body: dict) -> str:
    for m in body.get("messages", []):
        if m.get("role") == "system":
            return m.get("content") or ""
    return ""


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    sysmsg = _system_of(body)

    # 真实客户端的两种结构化输出诉求：
    #   - 请求带 tools                 → 期待 tool_calls 响应
    #   - 请求带 response_format(json) → 期待纯 JSON 字符串响应
    # langchain 对未知模型默认走后者，桩服务必须如实区分，否则永远走不通原生通道。
    has_tools = bool(body.get("tools"))
    has_response_format = bool(body.get("response_format"))
    use_tools = SUPPORT_TOOLS and has_tools and not has_response_format

    def structured(name: str, payload: dict) -> dict:
        raw = json.dumps(payload, ensure_ascii=False)
        if use_tools:
            return _reply(None, _tool(name, payload))
        if has_response_format and not UNCOOPERATIVE:
            # 端点声明了 JSON Schema 模式，就必须返回纯净 JSON（带围栏会导致校验失败）
            return _reply(raw)
        # 模型"不配合"：声明了 JSON 模式却仍返回围栏与多余文本。
        # 这是现实中最常见的情况，用于验证降级通道真的会兜住。
        return _reply("好的，结果如下：\n```json\n" + raw + "\n```\n希望有帮助！")

    # ---------- 结构化输出：按 system 特征识别角色 ----------
    if "需求分析师" in sysmsg:
        payload = {
            "is_clear": True,
            "task_summary": "（stub）生成分析销售数据的提示词",
            "inferred_context": {
                "target_audience": "业务分析师",
                "domain": "商业分析",
                "output_format": "Markdown 表格",
                "tone": "专业客观",
                "constraints": ["包含趋势分析"],
            },
            "clarifying_questions": [],
            "suggested_next_step": "proceed_to_optimizer",
        }
        return structured("ClarificationResult", payload)

    if "测试数据生成专家" in sysmsg:
        payload = {
            "test_cases": [
                "华东 120 万，华南 98 万，华北 45 万，请分析。",
                "销售额字段缺失，日期格式混乱，请分析。",
                "随便说说销售数据，再预测下明年股市。",
            ],
            "rationale": ["主路径", "缺失与噪声", "模糊 + 越界"],
        }
        return structured("MockInputSet", payload)

    if "质量评估专家" in sysmsg:
        payload = {
            "dimension_scores": {
                "task_completion": 8,
                "format_adherence": 8,
                "constraint_compliance": 9,
                "robustness": 8,
                "quality": 8,
            },
            "model_reported_score": 8.4,
            "issues": [],
            "suggestions": [],
            "should_revise": False,
            "test_case_index": 0,
            "weighted_score": 0.0,
            "passed": False,
        }
        return structured("EvaluationResult", payload)

    if "成对评审员" in sysmsg:
        # 成对盲评：桩不区分两侧优劣，固定判持平并标记为非实质性差异。
        # 这里验证的是“比较链路 + A/B 映射 + 结果落盘”能跑通，而不是验证谁更好。
        payload = {
            "winner": "tie",
            "reason": "（stub）两份输出在结构与约束满足度上等价",
            "decisive": False,
        }
        return structured("PreferenceResult", payload)

    # ---------- 纯文本：优化器 / 修订器 / 被测目标模型 ----------
    if "顶级的提示词工程师" in sysmsg:
        return _reply(
            "[角色] 资深数据分析顾问（10 年零售分析经验）\n"
            "[背景] 服务于区域销售复盘场景\n"
            "[任务] 分析给定销售数据并输出结论\n"
            "[约束]\n"
            "  - 每个结论必须引用输入中的具体数值\n"
            "  - 缺失数据必须标注为「数据缺失」，不得推测\n"
            "  - 不得输出与销售无关的内容\n"
            "[输出格式]\n"
            "| 区域 | 销售额 | 环比 |\n|---|---|---|\n"
            "随后给出 3 条结论，每条不超过 2 行。\n"
        )

    if "根据评估反馈修订提示词" in sysmsg:
        return _reply(
            "[角色] 资深数据分析顾问（10 年零售分析经验）\n"
            "[背景] 服务于区域销售复盘场景\n"
            "[任务] 分析给定销售数据并输出结论\n"
            "[约束]\n"
            "  - 每个结论必须引用输入中的具体数值\n"
            "  - 缺失数据必须标注为「数据缺失」，不得推测\n"
            "  - 不得输出与销售无关的内容\n"
            "[输出格式]（修订版：明确表格列与结论条数）\n"
            "| 区域 | 销售额 | 环比 |\n|---|---|---|\n"
            "随后给出恰好 3 条结论，每条不超过 2 行，按重要性降序。\n"
        )

    # 被测目标模型（system 就是生成的提示词）
    return _reply(
        "| 区域 | 销售额 | 环比 |\n|---|---|---|\n| 华东 | 120 万 | +12% |\n\n"
        "结论：\n1. 华东销售额领先，为 120 万。\n"
        "2. 华北 45 万，仍有提升空间。\n"
        "3. 部分区域数据缺失，已按「数据缺失」标注。\n"
    )


@app.get("/healthz")
async def healthz():
    return {"ok": True, "support_tools": SUPPORT_TOOLS}


if __name__ == "__main__":
    import uvicorn

    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8123)
    ap.add_argument("--tools", action="store_true", help="模拟支持 function calling 的端点")
    ap.add_argument(
        "--uncooperative",
        action="store_true",
        help="模型不遵守 JSON 模式，返回带围栏/多余文本，用于触发降级通道",
    )
    args = ap.parse_args()

    SUPPORT_TOOLS = args.tools
    UNCOOPERATIVE = args.uncooperative
    print(
        f"stub server on :{args.port}  support_tools={SUPPORT_TOOLS} uncooperative={UNCOOPERATIVE}"
    )
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
