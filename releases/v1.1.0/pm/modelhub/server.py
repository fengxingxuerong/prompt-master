"""OpenAI 兼容网关 v1.1.0：ModelHub 模型池对外接口。

新增（相对 v1.0.0，对标竞品优点 OPT-1~6）：
- POST /v1/chat/completions 支持 stream=true（上游 SSE 透传，切换语义：首块前失败自动换模型）
- POST /v1/keys 签发虚拟密钥（vk-…，落盘持久）；GET /v1/keys；PATCH /v1/keys/{key} 启停；DELETE /v1/keys/{key}
- GET /v1/usage 按 agent/model/key/day 聚合用量
- GET /v1/metrics 指标端点（探针友好）
- GET /console 轻量管理控制台（池状态/台账/密钥/对话测试）
- 智能体注册持久化（data/agents.json，重启不丢）

保留 v1.0.0 全部端点与语义。
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

load_dotenv()

from fastapi import FastAPI, Header, HTTPException, Request, Response  # noqa: E402
from fastapi.responses import StreamingResponse, HTMLResponse  # noqa: E402
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
from pm.modelhub.streaming import stream_upstream  # noqa: E402
from pm.modelhub.vkeys import get_agent_store, get_vkey_store  # noqa: E402

app = FastAPI(title="ModelHub Gateway", version="1.1.0")

_hub: ModelHub | None = None


def _hub_instance() -> ModelHub:
    global _hub
    if _hub is None:
        _hub = ModelHub()
    return _hub


def _admin_auth(x_api_key: str | None) -> None:
    """管理操作鉴权：未设 PMH_GATEWAY_TOKEN = 本地开放模式。"""
    expected = (os.getenv("PMH_GATEWAY_TOKEN") or "").strip()
    if not expected:
        return
    if not x_api_key or x_api_key.strip() != expected:
        raise HTTPException(status_code=401, detail="缺少或错误的 X-API-Key / Authorization（管理操作）")


def _chat_auth(authorization: str | None, x_api_key: str | None) -> tuple[str | None, str | None]:
    """对话鉴权：vk- 虚拟密钥（返回归属 agent）或管理员口令直通（返回 None）。

    返回 (vk_record, admin_pass)。vk- 无效/停用 → 401。
    """
    expected = (os.getenv("PMH_GATEWAY_TOKEN") or "").strip()
    key = None
    if x_api_key:
        key = x_api_key.strip()
    elif authorization and authorization.lower().startswith("bearer "):
        key = authorization[7:].strip()
    if key and key.startswith("vk-"):
        rec = get_vkey_store().verify(key)
        if rec is None:
            raise HTTPException(status_code=401, detail="虚拟密钥无效或已停用")
        return rec, None
    # 管理员口令直通
    if not expected:
        return None, "open"
    if key and key == expected:
        return None, "admin"
    raise HTTPException(status_code=401, detail="缺少或错误的鉴权（vk- 密钥或管理员口令）")


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


class KeyIssueRequest(BaseModel):
    agent: str
    note: str = ""


class KeyPatchRequest(BaseModel):
    enabled: bool


def _resolve_role_and_messages(req: ChatRequest) -> tuple[list[dict], str | None, str | None]:
    registry = get_registry()
    agent = (req.agent or "").strip() or None
    role_name = (req.role or "").strip() or None
    if agent:
        a = get_agent_store().get(agent) or {}
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
    return messages, agent, role_name


def _params_of(req: ChatRequest) -> dict[str, Any]:
    params: dict[str, Any] = {}
    if req.temperature is not None:
        params["temperature"] = req.temperature
    if req.max_tokens is not None:
        params["max_tokens"] = req.max_tokens
    return params


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "service": "modelhub", "version": "1.1.0", "time": time.time()}


@app.get("/v1/models")
def list_models(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    try:
        _chat_auth(authorization, x_api_key)
    except HTTPException:
        _admin_auth(_key_from_header(authorization, x_api_key))
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
):
    try:
        vk_rec, _mode = _chat_auth(authorization, x_api_key)
    except HTTPException:
        raise
    vk_agent = (vk_rec or {}).get("agent")
    agent = (req.agent or "").strip() or vk_agent
    # 用 vk- 时 agent 以密钥归属为准（防伪造归属）
    if vk_rec is not None and vk_agent:
        req.agent = vk_agent
        agent = vk_agent

    messages, agent_resolved, role_name = _resolve_role_and_messages(req)
    params = _params_of(req)
    hub = _hub_instance()

    if req.stream:
        return _stream_response(hub, req, messages, agent_resolved, role_name, params)

    try:
        result = hub.chat(
            messages, model=req.model, agent=agent_resolved, role=role_name, stream=False, **params
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
                "modelhub": {
                    "request_id": result["request_id"],
                    "endpoint": result["endpoint"],
                    "failovers": result["failovers"],
                    "latency_ms": result["latency_ms"],
                    "agent": agent_resolved,
                    "role": role_name,
                    "requested_model": req.model,
                    "transient_retried": result.get("transient_retried", False),
                },
            },
            ensure_ascii=False,
        ),
        media_type="application/json",
    )


def _stream_response(
    hub: ModelHub, req: ChatRequest, messages: list[dict], agent: str | None, role: str | None,
    params: dict[str, Any],
):
    """流式 SSE：按主备链逐个模型尝试；首块前失败自动切下一个。

    降级兑底（竞品 LiteLLM 同款思路）：上游对 stream=true 返回 401/404 等能力性拒绝时
    （实测商汤 Token Plan 同一时刻非流式正常、流式被拒），回退为"非流式请求 + 合成 SSE
    分块"，客户端契约（text/event-stream + data: chunks + [DONE]）不变。
    台账 mode 字段记 degraded=true，可追溯。
    """
    request_id = uuid.uuid4().hex[:12]
    t0 = time.time()

    def _headers_for(entry):
        return entry

    candidates = hub._ordered_enabled()
    if req.model is not None:
        head = [e for e in candidates if e.name == req.model]
        rest = [e for e in candidates if e.name != req.model]
        candidates = head + rest

    if not candidates:
        raise HTTPException(status_code=502, detail="模型池当前没有可用模型")

    timeout = float(os.getenv("PMH_TIMEOUT", "60") or 60)

    def attempt_stream():
        """依次尝试候选模型；成功后返回 (model_name, chunk_gen, entry)。"""
        last_err: GatewayError | None = None
        for idx, entry in enumerate(candidates):
            if idx > 0:
                append_switch = True
            else:
                append_switch = False
            try:
                upstream_model, gen = stream_upstream(
                    entry.base_url, entry.api_key,
                    {"model": entry.upstream_model, "messages": messages, **params},
                    timeout,
                )
                # 取第一个 chunk 验证连接真正建立
                first = next(gen, None)
                if first is None:
                    raise GatewayError("流式连接建立但无数据")
                if append_switch and last_err is not None:
                    from pm.modelhub.ledger import append_switch_event

                    append_switch_event(
                        request_id=request_id, from_model=(prev.name if (prev := candidates[idx - 1]) else "?"),
                        to_model=entry.name, reason=(last_err.reason if last_err else "unknown"),
                        detail=f"stream failover #{idx}", failover_index=idx,
                    )
                return entry, first, gen
            except GatewayError as e:
                last_err = e
                hub._mark_failure(entry, f"stream: {type(e).__name__}: {e}")
                continue
        raise ModelPoolExhaustedError(
            f"池内 {len(candidates)} 个模型流式全部失败（尝试顺序：{', '.join(e.name for e in candidates)}）。最后错误：{last_err}"
        )

    try:
        entry, first, gen = attempt_stream()
        degraded = False
    except ModelPoolExhaustedError as stream_err:
        # 降级兑底：全部模型流式失败（常见为上游对 stream=true 能力性拒绝）→
        # 改走非流式（带 D-009 重试/切换全语义），把整段正文合成为 SSE 分块。
        try:
            result = hub.chat(
                messages, model=req.model, agent=agent, role=role, stream=False, **params
            )
        except ModelPoolExhaustedError as e:
            from pm.modelhub.ledger import append_call_event

            append_call_event(
                request_id=request_id, model="(stream)", endpoint="?", success=False,
                latency_ms=int((time.time() - t0) * 1000), attempts=len(candidates) * 2,
                failovers=max(0, len(candidates) - 1), http_status=None,
                error_type="ModelPoolExhaustedError", error_msg=f"stream+fallback both failed: {e}"[:300],
                agent=agent, role=role, content_chars=None,
            )
            raise HTTPException(status_code=502, detail=str(e))
        except GatewayError as e:
            raise HTTPException(
                status_code=(e.http_status if e.http_status and e.http_status >= 400 else 502),
                detail=str(e),
            )
        from pm.modelhub.ledger import append_call_event

        append_call_event(
            request_id=request_id, model=result["model"], endpoint=result["endpoint"],
            success=True, latency_ms=result["latency_ms"], attempts=result.get("attempts", 1),
            failovers=result.get("failovers", 0), http_status=200,
            error_type=None, error_msg=None, agent=agent, role=role,
            content_chars=len(result["content"]),
        )
        entry = None
        degraded = True
        stream_err_text = str(stream_err)[:200]

        cid = "chatcmpl-" + uuid.uuid4().hex[:20]
        chunk = {
            "id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
            "model": result["model"],
            "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
        }
        body_chunk = {
            "id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
            "model": result["model"],
            "choices": [{"index": 0, "delta": {"content": result["content"]}, "finish_reason": None}],
        }
        done_note = (
            f"data: {json.dumps({'modelhub': {'degraded': True, 'note': 'upstream rejected stream=true; '
                                      f'served non-stream + synthesized SSE', 'last_stream_error': stream_err_text,
                                      'request_id': request_id}}, ensure_ascii=False)}\n\n"
        )

        def synth_gen():
            yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8")
            yield f"data: {json.dumps(body_chunk, ensure_ascii=False)}\n\n".encode("utf-8")
            yield done_note.encode("utf-8")
            yield b"data: [DONE]\n\n"

        return StreamingResponse(
            synth_gen(), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                     "X-Modelhub-Request-Id": request_id, "X-Modelhub-Degraded": "true"},
        )

    def sse_gen():
        yield first
        ok = True
        content_chars = 0
        err_in_stream: str | None = None
        try:
            for chunk in gen:
                try:
                    text = chunk.decode("utf-8", errors="replace")
                    if text.startswith("data:") and "[DONE]" not in text:
                        obj = json.loads(text[5:].strip())
                        for ch in obj.get("choices") or []:
                            piece = (ch.get("delta") or {}).get("content") or ""
                            content_chars += len(piece)
                except Exception:  # noqa: BLE001
                    pass
                yield chunk
        except Exception as e:  # noqa: BLE001
            ok = False
            err_in_stream = f"{type(e).__name__}: {e}"[:200]
            # 首块之后断流：显式注入错误事件，不静默
            yield (
                "data: " + json.dumps({"error": {"message": f"stream interrupted: {err_in_stream}"}},
                                      ensure_ascii=False) + "\n\n"
            ).encode("utf-8")
        finally:
            if ok:
                hub._mark_success(entry)
            else:
                hub._mark_failure(entry, err_in_stream or "stream error")
            from pm.modelhub.ledger import append_call_event

            append_call_event(
                request_id=request_id, model=entry.name, endpoint=entry.base_url,
                success=ok, latency_ms=int((time.time() - t0) * 1000), attempts=1,
                failovers=0, http_status=200 if ok else None,
                error_type=err_in_stream, error_msg=err_in_stream,
                agent=agent, role=role, content_chars=content_chars or None,
            )

    return StreamingResponse(
        sse_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "X-Modelhub-Request-Id": request_id},
    )


# ---------------- 虚拟密钥（OPT-2） ----------------

@app.get("/v1/keys")
def list_keys(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _admin_auth(_key_from_header(authorization, x_api_key))
    return {"keys": get_vkey_store().list()}


@app.post("/v1/keys")
def issue_key(
    req: KeyIssueRequest,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _admin_auth(_key_from_header(authorization, x_api_key))
    try:
        rec = get_vkey_store().issue(req.agent, note=req.note)
    except ConfigError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return {"ok": True, "key": rec}


@app.patch("/v1/keys/{key}")
def patch_key(
    key: str, req: KeyPatchRequest,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _admin_auth(_key_from_header(authorization, x_api_key))
    try:
        rec = get_vkey_store().set_enabled(key, req.enabled)
    except ConfigError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"ok": True, "key": rec}


@app.delete("/v1/keys/{key}")
def delete_key(
    key: str,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _admin_auth(_key_from_header(authorization, x_api_key))
    ok = get_vkey_store().delete(key)
    if not ok:
        raise HTTPException(status_code=404, detail="虚拟密钥不存在")
    return {"ok": True, "removed": key[:10] + "…"}


# ---------------- 智能体（持久化版） ----------------

@app.get("/v1/agents")
def list_agents(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    try:
        _chat_auth(authorization, x_api_key)
    except HTTPException:
        _admin_auth(_key_from_header(authorization, x_api_key))
    return {"agents": get_agent_store().list(), "persisted": True}


@app.post("/v1/agents")
def register_agent(
    req: AgentRegisterRequest,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    _admin_auth(_key_from_header(authorization, x_api_key))
    if (req.default_role or "").strip():
        get_registry().get_role(req.default_role.strip())  # 不存在 → 422
    agent = get_agent_store().register(
        req.name, framework=req.framework, default_role=(req.default_role or "").strip() or None,
        default_model=req.default_model, description=req.description,
    )
    return {"ok": True, "agent": agent, "persisted": True}


@app.delete("/v1/agents/{name}")
def unregister_agent(
    name: str,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    _admin_auth(_key_from_header(authorization, x_api_key))
    ok = get_agent_store().unregister(name)
    if not ok:
        raise HTTPException(status_code=404, detail=f"智能体 {name} 未注册")
    return {"ok": True, "removed": name}


# ---------------- 用量与指标（OPT-3 / OPT-6） ----------------

@app.get("/v1/usage")
def usage(
    agent: str | None = None,
    model: str | None = None,
    since: str | None = None,
    until: str | None = None,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    try:
        _chat_auth(authorization, x_api_key)
    except HTTPException:
        _admin_auth(_key_from_header(authorization, x_api_key))
    rows = query_ledger(type="call", model=model, since=since, until=until, limit=2000)
    agg: dict[str, Any] = {}
    for r in rows:
        if agent and r.get("agent") != agent:
            continue
        a = r.get("agent") or "(unattributed)"
        m = r.get("model") or "?"
        k1 = agg.setdefault(a, {})
        k2 = k1.setdefault(m, {"calls": 0, "success": 0, "latency_ms_sum": 0, "content_chars_sum": 0, "failovers_sum": 0, "days": {}})
        k2["calls"] += 1
        if r.get("success"):
            k2["success"] += 1
        k2["latency_ms_sum"] += int(r.get("latency_ms") or 0)
        k2["content_chars_sum"] += int(r.get("content_chars") or 0)
        k2["failovers_sum"] += int(r.get("failovers") or 0)
        day = (r.get("ts") or "")[:10]
        k2["days"][day] = k2["days"].get(day, 0) + 1
    total_calls = sum(v["calls"] for a in agg.values() for v in a.values())
    total_success = sum(v["success"] for a in agg.values() for v in a.values())
    return {
        "window": {"since": since, "until": until, "note": "最近 2000 条 call 事件聚合"},
        "totals": {"calls": total_calls, "success": total_success,
                   "success_rate": round(total_success / total_calls, 4) if total_calls else None},
        "by_agent": agg,
    }


@app.get("/v1/metrics")
def metrics(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    stats = ledger_stats()
    pool = _hub_instance().status()
    models = {}
    for m in pool["models"]:
        models[m["name"]] = {
            "circuit": m["circuit"], "consecutive_failures": m["consecutive_failures"], "enabled": m["enabled"],
        }
    calls = stats.get("calls") or 0
    ok = stats.get("success") or 0
    return {
        "ts": time.time(),
        "modelhub_calls_total": calls,
        "modelhub_calls_success": ok,
        "modelhub_calls_failed": stats.get("failed") or 0,
        "modelhub_success_rate": round(ok / calls, 4) if calls else None,
        "modelhub_switches_total": stats.get("switches") or 0,
        "modelhub_pool_models": len(pool["models"]),
        "modelhub_pool_circuits_open": sum(1 for m in pool["models"] if m["circuit"] != "closed"),
        "modelhub_by_model": stats.get("by_model") or {},
        "modelhub_circuits": models,
    }


@app.get("/v1/pool/status")
def pool_status(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    try:
        _chat_auth(authorization, x_api_key)
    except HTTPException:
        _admin_auth(_key_from_header(authorization, x_api_key))
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
    try:
        _chat_auth(authorization, x_api_key)
    except HTTPException:
        _admin_auth(_key_from_header(authorization, x_api_key))
    succ: bool | None = None
    if success is not None:
        succ = success.lower() in ("1", "true", "yes")
    rows = query_ledger(model=model, success=succ, type=type, since=since, until=until,
                        request_id=request_id, limit=min(max(1, limit), 2000))
    return {"count": len(rows), "rows": rows}


@app.get("/v1/ledger/stats")
def ledger_stats_api(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    try:
        _chat_auth(authorization, x_api_key)
    except HTTPException:
        _admin_auth(_key_from_header(authorization, x_api_key))
    return ledger_stats()


@app.get("/v1/roles")
def list_roles(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    try:
        _chat_auth(authorization, x_api_key)
    except HTTPException:
        _admin_auth(_key_from_header(authorization, x_api_key))
    registry = get_registry()
    try:
        names = registry.role_names()
    except ConfigError as e:
        raise HTTPException(status_code=503, detail=str(e))
    return {"roles": names, "note": "角色系统提示词固定在 config/roles.json，模型切换不影响"}


@app.get("/")
def root() -> dict[str, Any]:
    return {
        "service": "ModelHub Gateway",
        "version": "1.1.0",
        "routes": [
            "GET /v1/models", "POST /v1/chat/completions（stream 可选）",
            "GET /v1/pool/status", "GET /v1/ledger", "GET /v1/ledger/stats",
            "GET /v1/usage", "GET /v1/metrics", "GET /v1/roles",
            "GET|POST /v1/keys  PATCH|DELETE /v1/keys/{key}",
            "GET|POST /v1/agents  DELETE /v1/agents/{name}",
            "GET /console", "GET /api/health",
        ],
        "agent_quickstart": {
            "base_url": "http://127.0.0.1:8687/v1",
            "note": "OpenAI 兼容；model 可省略（自动主备链）；支持 stream=true",
        },
    }


@app.get("/console", response_class=HTMLResponse)
def console() -> HTMLResponse:
    from .console import CONSOLE_HTML

    return HTMLResponse(CONSOLE_HTML)
