"""OpenAI 兼容网关：把 ModelHub 模型池暴露为标准 /v1 接口。

任何会用 OpenAI SDK / LangChain ChatOpenAI / HTTP POST 的智能体都能零改造接入：
    base_url = http://127.0.0.1:8687/v1
    api_key  = PM_API_KEYS 中任意一把（网关鉴权用，与上游密钥解耦）

端点：
- GET  /v1/models            模型池清单（OpenAI 格式）
- POST /v1/chat/completions  对话补全（自动切换；model 字段可省略）
- GET  /v1/pool/status       池内断路器/冷却状态
- GET  /v1/ledger            台账检索（model/success/type/since/until/limit）
- GET  /v1/ledger/stats      台账汇总
- GET  /v1/agents            智能体清单
- POST /v1/agents            注册智能体 {"name","framework","default_role",...}
- DELETE /v1/agents/{name}   注销智能体
- GET  /v1/roles             固定角色清单
- GET  /api/health           健康检查

启动：python run_modelhub.py --port 8687
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv()  # 让 ${PM_API_KEY_*} 占位符在进程内可解析

from fastapi import FastAPI, Header, HTTPException, Response  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pm.modelhub.agents import get_registry  # noqa: E402
from pm.modelhub.ledger import ledger_stats, query_ledger  # noqa: E402
from pm.modelhub.pool import (  # noqa: E402
    ConfigError,
    GatewayError,
    ModelHub,
    ModelPoolExhaustedError,
)

app = FastAPI(title="ModelHub Gateway", version="1.0.0")

_hub: ModelHub | None = None


def _hub_instance() -> ModelHub:
    global _hub
    if _hub is None:
        _hub = ModelHub()
    return _hub


def _auth(x_api_key: str | None) -> None:
    expected = (os.getenv("PMH_GATEWAY_TOKEN") or "").strip()
    if not expected:
        return  # 未设令牌 = 本地开放模式（与 pm.server 的 PM_API_TOKEN 约定一致）
    if not x_api_key or x_api_key.strip() != expected:
        raise HTTPException(status_code=401, detail="缺少或错误的 X-API-Key / Authorization")


def _key_from_header(authorization: str | None, x_api_key: str | None) -> str | None:
    if x_api_key:
        return x_api_key
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return None


class ChatMessage(BaseModel):
    role: str
    content: Any = ""


class ChatRequest(BaseModel):
    model: str | None = None
    messages: list[ChatMessage]
    agent: str | None = None
    role: str | None = None
    stream: bool = False
    temperature: float | None = None
    max_tokens: int | None = Field(default=None, alias="max_tokens")

    class Config:
        populate_by_name = True
        extra = "allow"


class AgentRegisterRequest(BaseModel):
    name: str
    framework: str = "unknown"
    default_role: str | None = None
    default_model: str | None = None
    description: str = ""


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "service": "modelhub", "time": time.time()}


@app.get("/v1/models")
def list_models(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    _auth(_key_from_header(authorization, x_api_key))
    try:
        models = _hub_instance().list_models()
    except ConfigError as e:
        raise HTTPException(status_code=503, detail=str(e))
    return {
        "object": "list",
        "data": [
            {
                "id": m["name"],
                "object": "model",
                "created": 0,
                "owned_by": m["source"] or "modelhub",
                "priority": m["priority"],
                "family": m["family"],
                "enabled": m["enabled"],
            }
            for m in models
        ],
    }


@app.post("/v1/chat/completions")
def chat_completions(
    req: ChatRequest,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> Response:
    _auth(_key_from_header(authorization, x_api_key))
    registry = get_registry()
    agent = (req.agent or "").strip() or None

    # 未注册 agent 自动登记（幂等），保证台账可归属
    if agent and registry.get_agent(agent) is None:
        registry.register_agent(agent, framework="auto-registered")

    # 角色解析：显式 role > 智能体默认角色；角色不存在 → 422 显式报错
    role_name = (req.role or "").strip() or None
    if agent:
        a = registry.get_agent(agent) or {}
        role_name = role_name or a.get("default_role")
    system_prompt: str | None = None
    if role_name:
        try:
            system_prompt = registry.render_system_prompt(role_name)
        except ConfigError as e:
            raise HTTPException(status_code=422, detail=str(e))

    messages = [{"role": m.role, "content": m.content} for m in req.messages]
    if system_prompt and not any(m.get("role") == "system" for m in messages):
        messages = [{"role": "system", "content": system_prompt}] + messages

    params: dict[str, Any] = {}
    if req.temperature is not None:
        params["temperature"] = req.temperature
    if req.max_tokens is not None:
        params["max_tokens"] = req.max_tokens

    try:
        result = _hub_instance().chat(
            messages,
            model=req.model,
            agent=agent,
            role=role_name,
            stream=req.stream,
            **params,
        )
    except ConfigError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except ModelPoolExhaustedError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except GatewayError as e:
        raise HTTPException(
            status_code=(e.http_status if e.http_status and e.http_status >= 400 else 502),
            detail=str(e),
        )

    cid = "chatcmpl-" + uuid.uuid4().hex[:20]
    return Response(
        content=json.dumps(
            {
                "id": cid,
                "object": "chat.completion",
                "created": int(time.time()),
                "model": result["model"],
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": result["content"]},
                        "finish_reason": result.get("raw_finish_reason") or "stop",
                    }
                ],
                "usage": result.get("usage")
                or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                # ModelHub 扩展字段（OpenAI SDK 会忽略未知字段）
                "modelhub": {
                    "request_id": result["request_id"],
                    "endpoint": result["endpoint"],
                    "failovers": result["failovers"],
                    "latency_ms": result["latency_ms"],
                    "agent": agent,
                    "role": role_name,
                    "requested_model": req.model,
                },
            },
            ensure_ascii=False,
        ),
        media_type="application/json",
    )


@app.get("/v1/pool/status")
def pool_status(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    _auth(_key_from_header(authorization, x_api_key))
    return _hub_instance().status()


@app.get("/v1/ledger")
def ledger(
    model: str | None = None,
    success: str | None = None,
    type: str | None = None,
    since: str | None = None,
    until: str | None = None,
    request_id: str | None = None,
    limit: int = 200,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    _auth(_key_from_header(authorization, x_api_key))
    succ: bool | None = None
    if success is not None:
        succ = success.lower() in ("1", "true", "yes")
    rows = query_ledger(
        model=model,
        success=succ,
        type=type,
        since=since,
        until=until,
        request_id=request_id,
        limit=min(max(1, limit), 2000),
    )
    return {"count": len(rows), "rows": rows}


@app.get("/v1/ledger/stats")
def ledger_stats_api(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    _auth(_key_from_header(authorization, x_api_key))
    return ledger_stats()


@app.get("/v1/roles")
def list_roles(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    _auth(_key_from_header(authorization, x_api_key))
    registry = get_registry()
    try:
        names = registry.role_names()
    except ConfigError as e:
        raise HTTPException(status_code=503, detail=str(e))
    return {"roles": names, "note": "角色系统提示词固定在 config/roles.json，模型切换不影响"}


@app.get("/v1/agents")
def list_agents(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    _auth(_key_from_header(authorization, x_api_key))
    return {"agents": get_registry().list_agents()}


@app.post("/v1/agents")
def register_agent(
    req: AgentRegisterRequest,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    _auth(_key_from_header(authorization, x_api_key))
    try:
        agent = get_registry().register_agent(
            req.name,
            framework=req.framework,
            default_role=req.default_role,
            default_model=req.default_model,
            description=req.description,
        )
    except ConfigError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return {"ok": True, "agent": agent}


@app.delete("/v1/agents/{name}")
def unregister_agent(
    name: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    _auth(_key_from_header(authorization, x_api_key))
    ok = get_registry().unregister_agent(name)
    if not ok:
        raise HTTPException(status_code=404, detail=f"智能体 {name} 未注册")
    return {"ok": True, "removed": name}


@app.get("/")
def root() -> dict[str, Any]:
    return {
        "service": "ModelHub Gateway",
        "routes": [
            "GET /v1/models",
            "POST /v1/chat/completions",
            "GET /v1/pool/status",
            "GET /v1/ledger",
            "GET /v1/ledger/stats",
            "GET /v1/roles",
            "GET|POST /v1/agents",
            "DELETE /v1/agents/{name}",
            "GET /api/health",
        ],
        "agent_quickstart": {
            "base_url": "http://127.0.0.1:8687/v1",
            "note": "OpenAI 兼容；model 可省略（自动按 priority 主备链切换）",
        },
    }
