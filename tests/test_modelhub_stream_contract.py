"""ModelHub 网关的流式（SSE）契约回归 —— 直接盯 `_stream_response` 的返回与分帧。

为什么单独一个文件（2026-09-25）：`stream=true` 是网关对外承诺的能力，但
`tests_modelhub/` 里那 5 个 `*_test.py` 全是 `python xxx_test.py` 脚本（函数名
不带 test_ 前缀），pytest 实测收集 0 条 ⇒ 这条通道在流水线里根本没有覆盖。
结果是一次真实的回归没人能拦住：`_stream_response()` 里 `sse_gen()` 定义完之后
**没有 return**，于是 stream=true 时 FastAPI 拿到 None、客户端收到 `null`，
而 `python -m pytest tests/` 全绿。

归因（2026-09-25 复核后更正过一版）：`pm/modelhub/server.py` 这个文件是 54d6f51（v1.4.0，
心跳保活那一版）**首次建立**的，建出来当时就没有这个 return —— 不是"先有、后来重构丢了"。
发布快照 `releases/v1.1.0/pm/modelhub/server.py:440` 里 return 还在，说明是**把旧版代码搬进
`pm/` 时丢了函数尾部**：走这条路径的流式从 v1.4.0 起就没通过过。唯一会当场抓住它的
`tests_modelhub/v111_test.py::V1_stream_sse` 是脚本（pytest 收集 0 条），所以跨三个版本没人重跑。

同一个函数尾部还有第二处：中断时给客户端的错误帧写成 `str + bytes` 拼接，
必抛 TypeError（见 test_interrupted_stream_emits_a_bytes_error_frame）。
"""

from __future__ import annotations

import asyncio
import importlib
import json
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException
from pm.modelhub import server as S
from pm.modelhub.pool import ModelEntry
from pm.modelhub.server import (
    ChatMessage,
    ChatRequest,
    _params_of,
    _resolve_role_and_messages,
)

_REQ = ChatRequest(messages=[ChatMessage(role="user", content="你好")], stream=True)


@pytest.fixture(autouse=True)
def isolate_ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """台账/用量写到 tmp_path，测试不往真实 data/ 里追加事件。"""
    monkeypatch.setenv("PMH_DATA_DIR", str(tmp_path))


class _FakeHub:
    """只实现 `_stream_response` 用到的几个方法。

    `chat` 是 2026-10-02 加的：降级兜底（全部流式失败 → 改走非流式再合成 SSE）
    要调它。默认**抛池耗尽**，这样既有用例（只测流式路径）行为完全不变；
    要测降级就显式传 `chat_result=` 或 `chat_error=`。
    """

    def __init__(
        self,
        entries: list[ModelEntry],
        *,
        chat_result: dict[str, Any] | None = None,
        chat_error: BaseException | None = None,
    ) -> None:
        self._entries = entries
        self._chat_result = chat_result
        self._chat_error = chat_error
        self.success: list[str] = []
        self.failure: list[tuple[str, str]] = []
        self.chat_calls: list[dict[str, Any]] = []

    def _ordered_enabled(self) -> list[ModelEntry]:
        return self._entries

    def _mark_success(self, entry: ModelEntry) -> None:
        self.success.append(entry.name)

    def _mark_failure(self, entry: ModelEntry, reason: str) -> None:
        self.failure.append((entry.name, reason))

    def chat(self, messages: list[dict[str, Any]], **kw: Any) -> dict[str, Any]:
        self.chat_calls.append({"messages": messages, **kw})
        if self._chat_error is not None:
            raise self._chat_error
        if self._chat_result is None:
            # 默认：非流式也全失败（既有用例的流向不变）
            from pm.modelhub.pool import ModelPoolExhaustedError

            raise ModelPoolExhaustedError("stub: 非流式也全失败")
        return self._chat_result


def _chat_ok(content: str = "兜底正文", model: str = "stub-model") -> dict[str, Any]:
    """`hub.chat` 成功时的返回体形状（与 `pool.chat` 真实返回一致）。"""
    return {
        "model": model,
        "endpoint": "http://127.0.0.1:9/v1",
        "content": content,
        "latency_ms": 12,
        "attempts": 2,
        "failovers": 1,
    }


def _entry(name: str = "stub-model") -> ModelEntry:
    return ModelEntry({"name": name, "base_url": "http://127.0.0.1:9/v1", "api_key": "sk-stub"})


def _call(
    monkeypatch: pytest.MonkeyPatch,
    hub: _FakeHub,
    upstream: Callable[[], Iterator[bytes]],
) -> tuple[Any, dict[str, Any]]:
    """跑一次 _stream_response，返回 (响应对象, 上游调用记录)。"""
    seen: dict[str, Any] = {}

    def fake_stream_upstream(
        base_url: str, api_key: str, payload: dict[str, Any], timeout: float
    ) -> tuple[str, Iterator[bytes]]:
        seen["base_url"] = base_url
        seen["payload_model"] = payload.get("model")
        return "upstream-model", upstream()

    monkeypatch.setattr(S, "stream_upstream", fake_stream_upstream)
    resp = S._stream_response(hub, _REQ, [{"role": "user", "content": "你好"}], None, None, {})
    return resp, seen


def _frames(resp: Any) -> list[bytes]:
    async def collect() -> list[bytes]:
        out: list[bytes] = []
        async for chunk in resp.body_iterator:
            out.append(chunk)
        return out

    return asyncio.run(collect())


def test_stream_response_returns_a_streaming_response(monkeypatch: pytest.MonkeyPatch) -> None:
    """回归本体：_stream_response 必须返回 StreamingResponse，不是 None。"""

    def upstream() -> Iterator[bytes]:
        yield b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
        yield b"data: [DONE]\n\n"

    resp, _seen = _call(monkeypatch, _FakeHub([_entry()]), upstream)
    assert resp is not None, "sse_gen() 定义完没 return —— stream=true 会返回 None"
    assert isinstance(resp, S.StreamingResponse)
    assert resp.media_type == "text/event-stream"


def test_stream_body_is_forwarded_verbatim_and_terminated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """客户端契约：上游分帧原样透传，末帧 [DONE]，且每帧都是 bytes。"""

    def upstream() -> Iterator[bytes]:
        yield b'data: {"choices":[{"delta":{"content":"A"}}]}\n\n'
        yield b'data: {"choices":[{"delta":{"content":"B"}}]}\n\n'
        yield b"data: [DONE]\n\n"

    hub = _FakeHub([_entry()])
    resp, seen = _call(monkeypatch, hub, upstream)
    frames = _frames(resp)
    assert all(isinstance(f, bytes) for f in frames), f"每帧都必须是 bytes：{frames}"
    assert frames[0].startswith(b"data: ") and frames[-1] == b"data: [DONE]\n\n"
    assert b'"A"' in b"".join(frames) and b'"B"' in b"".join(frames)
    assert seen["base_url"] == "http://127.0.0.1:9/v1"
    assert hub.success == ["stub-model"], "正常收尾要按成功记账"


def test_interrupted_stream_emits_a_bytes_error_frame(monkeypatch: pytest.MonkeyPatch) -> None:
    """上游中途炸：必须给客户端一帧 `data: {...error...}`（bytes），并记失败。

    旧写法是 `"data: " + json.dumps(...) + b"\\n\\n"`（str 拼 bytes）→ 当场 TypeError，
    于是"上游断流"和"网关自己崩了"对客户端长得一模一样，中断现场不可见。
    """

    def upstream() -> Iterator[bytes]:
        yield b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
        raise RuntimeError("上游 502 断流")

    hub = _FakeHub([_entry()])
    resp, _seen = _call(monkeypatch, hub, upstream)
    joined = b"".join(_frames(resp))
    assert b'"error"' in joined, f"没有把中断原因发给客户端：{joined!r}"
    assert b"502" in joined, f"错误帧要带上游异常原文：{joined!r}"
    assert hub.failure and "上游 502 断流" in hub.failure[0][1]
    assert not hub.success


def test_http_layer_returns_event_stream_not_null(monkeypatch: pytest.MonkeyPatch) -> None:
    """真 HTTP 层（进程内 TestClient，零出网）：stream=true 必须拿到 SSE 流，不是 `null`。

    上面几条测的是 `_stream_response` 这个函数；这一条测的是"路由把不把它的返回值交出去"。
    丢了 return 的那一版，FastAPI 在这里序列化出的是 200 + body=`null`——
    只有从 HTTP 层断言才看得见，函数级测试全绿也照样骗过去。
    """
    from fastapi.testclient import TestClient

    def upstream() -> Iterator[bytes]:
        yield b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
        yield b"data: [DONE]\n\n"

    monkeypatch.setattr(S, "stream_upstream", lambda *a, **k: ("upstream-model", upstream()))
    monkeypatch.setattr(S, "_hub_instance", lambda: _FakeHub([_entry()]))

    with TestClient(S.app).stream(
        "POST",
        "/v1/chat/completions",
        json={
            "model": "stub-model",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        },
    ) as r:
        assert r.status_code == 200, r.status_code
        assert r.headers.get("content-type", "").startswith("text/event-stream"), r.headers
        body = b"".join(r.iter_bytes())

    assert body != b"null", "FastAPI 把 None 序列化成了 null —— 流式通道没接上"
    assert b"[DONE]" in body and b"hi" in body, body


def test_request_model_keeps_tolerating_extra_openai_fields() -> None:
    """`extra="allow"` 是 OpenAI 客户端多带字段时不 422 的唯一原因，删不得。

    顺手盯住一次清理：`Field(alias="max_tokens")` 与字段同名、pydantic 每次 import 都报
    "no effect" 警告（真警告会被它盖掉），已删；`class Config` 换成 model_config。
    这两处改动唯一的回归风险就是"客户端字段进不来 / max_tokens 不再生效"，所以各断一条。
    """
    req = S.ChatRequest.model_validate(
        {
            "model": "stub-model",
            "messages": [{"role": "user", "content": "hi"}],
            "user": "end-user-1",
            "n": 2,
            "seed": 7,
            "max_tokens": 512,
        }
    )
    assert req.max_tokens == 512
    assert req.model_extra is not None and req.model_extra.get("user") == "end-user-1"
    # 未知字段不许被静默丢掉：网关把它们原样转给上游
    assert (
        S.ChatRequest.model_validate(
            {"model": "m", "messages": [{"role": "user", "content": "x"}], "stream": True}
        ).stream
        is True
    )


def test_importing_the_gateway_emits_no_pydantic_warnings() -> None:
    """导入期不许有"弃用/无效声明"类警告：警告堆多了，真那条就没人看
    （本次那条 `alias` no-effect 就是被一堆同类噪音盖住的）。"""
    import warnings

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        importlib.reload(S)
    noisy = [
        f"{type(w.message).__name__}: {w.message}"
        for w in caught
        if "Deprecated" in type(w.message).__name__ or "no effect" in str(w.message)
    ]
    assert not noisy, f"导入又带出弃用类警告：{noisy}"


def test_request_id_header_survives(monkeypatch: pytest.MonkeyPatch) -> None:
    """X-Modelhub-Request-Id 是对账用的（台账里同 request_id 能查到这一次）——别丢。"""

    def upstream() -> Iterator[bytes]:
        yield b"data: [DONE]\n\n"

    resp, _seen = _call(monkeypatch, _FakeHub([_entry()]), upstream)
    rid = resp.headers.get("X-Modelhub-Request-Id")
    assert rid and len(rid) >= 8, f"响应头缺少可对账的 request_id：{resp.headers}"
    assert _frames(resp)  # 必须真的消费完，台账是在流收尾时写的


def test_one_stream_request_writes_exactly_one_ledger_row_with_the_same_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """一次流式请求 = 台账里恰好一行，且它的 request_id 就是响应头那个。

    为什么单独钉：2026-09-25 用浏览器点控制台时看到过**两行** ts 相差 6ms、
    `latency_ms` 完全相同的 call 事件，而 request_id 不同。当时无法判断是
    "浏览器发了两次"还是"一次请求写了两行" —— 而 /v1/usage 的总数、成功率、
    平均耗时全建立在这张表上，多写一行就是把调用数虚报一次。
    所以这里在进程内把它钉死：一次 `_stream_response` 走完全程 ⇒ 恰好一行、id 对得上。
    """
    log = tmp_path / "ledger.jsonl"
    monkeypatch.setenv("PMH_LEDGER_PATH", str(log))

    def upstream() -> Iterator[bytes]:
        yield b'data: {"choices":[{"delta":{"content":"\xe4\xbd\xa0"}}]}\n\n'
        yield b'data: {"choices":[{"delta":{"content":"\xe5\xa5\xbd"}}]}\n\n'
        yield b"data: [DONE]\n\n"

    resp, _seen = _call(monkeypatch, _FakeHub([_entry()]), upstream)
    rid = resp.headers.get("X-Modelhub-Request-Id")
    _frames(resp)
    rows = [
        json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    calls = [r for r in rows if r.get("type") == "call"]
    assert len(calls) == 1, (
        f"一次请求写了 {len(calls)} 行 call 事件：{[c['request_id'] for c in calls]}"
    )
    assert calls[0]["request_id"] == rid, "台账 id 与响应头不一致 ⇒ 拿头里的 id 查不到这次调用"
    assert calls[0]["success"] is True and calls[0]["model"] == "stub-model"
    # 两帧都要计入台账：第一帧原来是 `yield first` 直接发出、不进计数循环，
    # 所以"正文全在第一帧"的短回答在台账里是 content_chars=None
    assert calls[0]["content_chars"] == 2, calls[0]
    assert calls[0]["latency_ms"] >= 0


def test_interrupted_stream_still_writes_one_row_and_marks_it_failed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """上游断流时那一条也不能丢：运维要能在台账里看到"这次是中断的"，而不是查无此调用。"""
    log = tmp_path / "ledger.jsonl"
    monkeypatch.setenv("PMH_LEDGER_PATH", str(log))

    def upstream() -> Iterator[bytes]:
        yield b'data: {"choices":[{"delta":{"content":"\xe5\x8d\x8a\xe6\xae\xb5"}}]}\n\n'
        raise RuntimeError("上游连接被重置")

    resp, _seen = _call(monkeypatch, _FakeHub([_entry()]), upstream)
    body = _frames(resp)
    assert any(b"stream interrupted" in chunk for chunk in body), body
    rows = [
        json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    calls = [r for r in rows if r.get("type") == "call"]
    assert len(calls) == 1, f"中断路径写了 {len(calls)} 行"
    assert calls[0]["success"] is False and calls[0]["http_status"] is None
    assert "上游连接被重置" in (calls[0]["error_msg"] or "")


# ---------------------------------------------------------------------------
# SSE 帧解析与心跳（2026-10-02 补）
#
# 这两块的共同点：都在 `sse_gen` 这个**闭包**里，只有真正把响应体迭代一遍才会执行到。
# 只断言"_stream_response 返回了 StreamingResponse"碰不到它们 ——
# 那正是它们长期是覆盖缺口的原因。
# ---------------------------------------------------------------------------
@pytest.fixture
def gateway_like(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """给"角色解析"类用例提供最小环境：一个角色文件 + 一次性的注册表/智能体库。

    直接复用 `test_modelhub_admin_routes.py` 的 `gateway` 夹具思路（环境变量 + 清单例），
    但**不建 TestClient** —— 这几条测的是纯函数，起 HTTP 反而绕远了。
    """
    from pm.modelhub import agents as AG
    from pm.modelhub import server as S
    from pm.modelhub import vkeys as VK

    store = tmp_path / "mh"
    store.mkdir(exist_ok=True)
    roles = tmp_path / "roles.json"
    roles.write_text(
        '{"roles": {"writer": {"system_prompt": "写"}, "judge": {"system_prompt": "评"}}}',
        encoding="utf-8",
    )
    monkeypatch.setenv("PMH_DATA_DIR", str(store))
    monkeypatch.setenv("PMH_ROLES", str(roles))
    monkeypatch.delenv("PMH_GATEWAY_TOKEN", raising=False)
    monkeypatch.setattr(VK, "_VK", None)
    monkeypatch.setattr(AG, "_REGISTRY", None)
    monkeypatch.setattr(S, "_hub", None)
    # 注册一个带默认角色的智能体，供"agent → default_role"那条分支使用。
    # ⚠️ `get_agent_store` 在 `vkeys` 里而不是 `agents` 里（试出来的，别猜）。
    # 真实方法是 `register(name, *, default_role=...)`（不是 put）——
    # 先查签名再写，省掉一轮试错。
    VK.get_agent_store().register("bot-role", default_role="writer")


def _call_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """读出台账里的 call 行（字段名与读法照抄本文件既有用例：`"type": "call"`）。"""
    log = tmp_path / "ledger.jsonl"
    monkeypatch.setenv("PMH_LEDGER_PATH", str(log))
    return log  # type: ignore[return-value]


def _rows_of(log: Path) -> list[dict[str, Any]]:
    if not log.exists():
        return []
    return [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines() if x.strip()]


def test_content_chars_counts_the_first_frame(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**首帧的正文必须计入 `content_chars`** —— 这是修过的真缺陷。

    `yield first` 曾发生在计数循环**之前**，于是第一帧永远不计：实测"网关连通"四个字
    一帧到达的短回答，在台账里就是 `content_chars=None`，`/v1/usage` 的
    `content_chars_sum` 跟着长期偏 0。
    这里用一个"只有一帧正文"的上游钉住它：首帧不计的话总数会是 0。
    """
    log = _call_rows(tmp_path, monkeypatch)

    def upstream() -> Iterator[bytes]:
        frame = json.dumps({"choices": [{"delta": {"content": "网关连通"}}]}, ensure_ascii=False)
        yield ("data: " + frame + "\n\n").encode("utf-8")
        yield b"data: [DONE]\n\n"

    resp, _ = _call(monkeypatch, _FakeHub([_entry()]), upstream)
    _frames(resp)
    calls = [r for r in _rows_of(log) if r.get("type") == "call"]
    assert calls, "没有落到台账的 call 行"
    assert calls[-1]["content_chars"] == 4, (
        f"首帧正文没被计入（期望 4 个字，实得 {calls[-1]['content_chars']}）"
    )


def test_content_chars_ignores_comment_and_done_frames(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """非 `data:` 帧（注释）与 `[DONE]` 帧都不计正文 —— 它们不是客户端看到的正文。

    上游常夹带 `: keep-alive` 注释行；算进正文字数会让用量读数虚高，
    而那是运维判断"网关有没有在干活"的唯一数字。
    """
    log = _call_rows(tmp_path, monkeypatch)

    def upstream() -> Iterator[bytes]:
        yield b": keep-alive 1\n\n"  # 非 data: 帧（宽度 >0，若被计入会明显偏大）
        yield b'data: {"choices":[{"delta":{"content":"AB"}}]}\n\n'
        yield b"data: [DONE]\n\n"

    resp, _ = _call(monkeypatch, _FakeHub([_entry()]), upstream)
    frames = _frames(resp)
    assert b"AB" in b"".join(frames), "内容帧没有透传"
    calls = [r for r in _rows_of(log) if r.get("type") == "call"]
    assert calls[-1]["content_chars"] == 2, f"注释/[DONE] 帧被算进了正文字数：{calls[-1]}"


def test_content_chars_survives_unparseable_frame(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """帧内容不是合法 JSON 时按 0 计并**继续转发**。

    上游偶尔发半截帧是常态；若这里抛错，一条记账问题会把整条流掐断 ——
    "记账解析失败绝不影响转发"就是那句 `except Exception` 的存在理由。
    """
    log = _call_rows(tmp_path, monkeypatch)

    def upstream() -> Iterator[bytes]:
        yield b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
        yield b"data: {not-json-at-all\n\n"
        yield b'data: {"choices":[{"delta":{"content":"!"}}]}\n\n'
        yield b"data: [DONE]\n\n"

    resp, _ = _call(monkeypatch, _FakeHub([_entry()]), upstream)
    joined = b"".join(_frames(resp))
    assert b"ok" in joined and b"!" in joined, "坏帧把后面的正常帧也带没了"
    calls = [r for r in _rows_of(log) if r.get("type") == "call"]
    assert calls[-1]["content_chars"] == 3, f"坏帧后应继续累计正常帧（2+1）：{calls[-1]}"


def test_upstream_error_sentinel_raises_instead_of_forwarding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """泵线程用 `ERR::` 哨兵传上游异常时，必须**抛出去并记失败**，不能当帧透传。

    `ERR::` 之外的 str 载荷则被丢弃（上游分帧一律 bytes，混进 text/event-stream
    的非 bytes 载荷会让客户端解析器崩）。
    """
    log = _call_rows(tmp_path, monkeypatch)

    def upstream() -> Iterator[bytes]:
        yield b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
        raise RuntimeError("上游 502 断流")

    hub = _FakeHub([_entry()])
    resp, _ = _call(monkeypatch, hub, upstream)
    joined = b"".join(_frames(resp))
    assert b'"error"' in joined, f"中断原因没有发给客户端：{joined!r}"
    assert hub.failure and "上游 502 断流" in hub.failure[0][1]
    calls = [r for r in _rows_of(log) if r.get("type") == "call"]
    assert calls and calls[-1]["success"] is False, "中断必须记失败"


def test_heartbeat_is_sent_while_upstream_is_slow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """上游慢于心跳间隔时，网关必须自己发 `: keep-alive` 注释行。

    不发的话，长时间无数据会触发中间层读超时把长连接掐断 ——
    而推理久的上游是常态（这就是 D-L2 心跳保活存在的理由）。

    `PMH_HEARTBEAT_SECONDS` 把间隔调到 0.05s：默认 15s 会让任何单测都不可接受，
    这也正是这条路径此前无法被测试触发的原因（该 env 于本轮加入，与
    `PMH_TIMEOUT` / `PMH_BREAK_THRESHOLD` 同族）。
    """
    log = _call_rows(tmp_path, monkeypatch)
    monkeypatch.setenv("PMH_HEARTBEAT_SECONDS", "0.05")

    def upstream() -> Iterator[bytes]:
        yield b'data: {"choices":[{"delta":{"content":"first"}}]}\n\n'
        time.sleep(0.25)  # 明显长于调小后的心跳间隔
        yield b'data: {"choices":[{"delta":{"content":"late"}}]}\n\n'
        yield b"data: [DONE]\n\n"

    resp, _ = _call(monkeypatch, _FakeHub([_entry()]), upstream)
    frames = _frames(resp)
    joined = b"".join(frames)
    assert b": keep-alive" in joined, f"慢上游期间没有发心跳：{frames}"
    assert b"first" in joined and b"late" in joined, "心跳把正常帧吃掉了"
    # 心跳是注释行：不该被算进正文字数
    calls = [r for r in _rows_of(log) if r.get("type") == "call"]
    assert calls[-1]["content_chars"] == len("first") + len("late"), (
        f"心跳被算进了正文字数：{calls[-1]['content_chars']}"
    )


def test_str_payload_from_upstream_is_not_forwarded_as_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """上游若产出 `str` 载荷（非 bytes），**不得原样 yield**。

    契约上上游分帧一律是 bytes，但泵线程显式处理了 str 这一支 ——
    说明真实上游出现过非 bytes 载荷（或将来会）。原样 yield 会把非 bytes 混进
    `text/event-stream` 响应，客户端解析器可能直接崩；正确做法是丢掉该帧并继续。

    这一支与"`ERR::` 哨兵"共用同一个 `isinstance(chunk, str)` 判断：
    哨兵抛异常、其余丢弃 —— 两条路都要走一遍，否则只测了其中一条。
    """

    def upstream() -> Iterator[Any]:
        yield b'data: {"choices":[{"delta":{"content":"A"}}]}\n\n'
        yield "这帧是 str，不是合法 SSE 帧"  # 非哨兵的 str：应被丢弃
        yield b'data: {"choices":[{"delta":{"content":"B"}}]}\n\n'
        yield b"data: [DONE]\n\n"

    resp, _ = _call(monkeypatch, _FakeHub([_entry()]), upstream)
    frames = _frames(resp)
    assert all(isinstance(f, bytes) for f in frames), f"混进了非 bytes 帧：{frames}"
    joined = b"".join(frames)
    assert b'"A"' in joined and b'"B"' in joined, "丢弃坏帧时把正常帧也带没了"
    assert "这帧是 str".encode() not in joined, "非 bytes 载荷被原样转发了"


def test_frame_content_chars_ignores_str_frame(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_frame_content_chars` 收到 str 时按 0 计（它只该收到 bytes）。

    这条覆盖那个 `return 0`：若它改成抛错或按字符数计，
    要么一条记账问题掐断整条流，要么把非正文载荷算进用量读数。
    """
    log = _call_rows(tmp_path, monkeypatch)

    def upstream() -> Iterator[Any]:
        yield "str 帧不该被计数"
        yield b'data: {"choices":[{"delta":{"content":"XY"}}]}\n\n'
        yield b"data: [DONE]\n\n"

    resp, _ = _call(monkeypatch, _FakeHub([_entry()]), upstream)
    _frames(resp)
    calls = [r for r in _rows_of(log) if r.get("type") == "call"]
    assert calls[-1]["content_chars"] == 2, (
        f"str 帧被算进了正文字数（期望只有 XY 的 2）：{calls[-1]['content_chars']}"
    )


# ---------------------------------------------------------------------------
# 请求参数拼装与角色解析（2026-10-02 补）
# ---------------------------------------------------------------------------
def test_params_of_only_forwards_explicitly_set_fields() -> None:
    """`_params_of` 只透传**显式设了值**的字段，不塞默认值。

    塞默认值会覆盖上游自己的默认（不同上游的 temperature 默认不同），
    而"没设"与"设成某个值"在这里是两件事 —— 前者应让上游决定。
    """

    msg = [ChatMessage(role="user", content="hi")]
    assert _params_of(ChatRequest(messages=msg)) == {}, "没设参数时不该塞任何键"
    assert _params_of(ChatRequest(messages=msg, temperature=0.5)) == {"temperature": 0.5}
    assert _params_of(ChatRequest(messages=msg, max_tokens=100)) == {"max_tokens": 100}
    both = _params_of(ChatRequest(messages=msg, temperature=0.0, max_tokens=1))
    # 0.0 与 1 是**显式设过**的值，不能被 `if x` 这类真假判断吃掉
    assert both == {"temperature": 0.0, "max_tokens": 1}, f"显式 0/1 被丢了：{both}"


def test_resolve_role_and_messages_injects_system_prompt(gateway_like) -> None:
    """角色解析：`agent` 的默认角色会带出 system prompt，并**前置**进 messages。

    `_resolve_role_and_messages` 此前零覆盖（9 行）。它决定目标模型收到什么 system：
    漏了这段，注册了默认角色的智能体拿到的请求里没有角色约束。
    """

    req = ChatRequest(
        messages=[ChatMessage(role="user", content="hi")], agent="bot-role", role="writer"
    )
    messages, agent, role = _resolve_role_and_messages(req)
    assert agent == "bot-role" and role == "writer"
    assert messages[0]["role"] == "system", f"system prompt 没有前置：{messages}"
    assert messages[0]["content"] == "写", f"system prompt 内容不对：{messages[0]}"
    assert messages[1] == {"role": "user", "content": "hi"}


def test_resolve_role_keeps_existing_system_message(gateway_like) -> None:
    """调用方自己带了 system 消息时**不重复注入**（否则会出两条互相打架的 system）。"""
    from pm.modelhub.server import ChatMessage, ChatRequest, _resolve_role_and_messages

    req = ChatRequest(
        messages=[
            ChatMessage(role="system", content="调用方自己的规则"),
            ChatMessage(role="user", content="hi"),
        ],
        role="writer",
    )
    messages, _agent, _role = _resolve_role_and_messages(req)
    systems = [m for m in messages if m["role"] == "system"]
    assert len(systems) == 1, f"注入了重复的 system：{messages}"
    assert systems[0]["content"] == "调用方自己的规则", "覆盖了调用方自己的 system"


def test_resolve_role_rejects_unknown_role_with_422(gateway_like) -> None:
    """角色不存在 → 422 并带上可用角色清单（不是 500）。

    422 = "你给的参数有问题"，500 = "网关崩了" —— 运维看这两个码动作不同；
    而"可用角色清单"是让调用方能自己改对的关键信息（`ConfigError` 里带着它）。
    """
    from pm.modelhub.server import ChatMessage, ChatRequest, _resolve_role_and_messages

    req = ChatRequest(messages=[ChatMessage(role="user", content="hi")], role="__不存在__")
    try:
        _resolve_role_and_messages(req)
        raise AssertionError("未知角色没有报错")
    except HTTPException as e:
        assert e.status_code == 422, f"应是 422 实际 {e.status_code}"


# ---------------------------------------------------------------------------
# 降级兜底：全部模型流式失败 → 改走非流式 + 合成 SSE（2026-10-02 补，84 行）
#
# 触发条件：`attempt_stream()` 抛 `ModelPoolExhaustedError`（所有候选流式都失败）。
# 真实场景：上游对 `stream=true` 做**能力性拒绝**（实测商汤 Token Plan 同一时刻
# 非流式正常、流式 401）—— 这时候对客户端正确的做法不是报错，
# 而是拿非流式结果合成一套合法的 SSE 分块，客户端契约不变。
# ---------------------------------------------------------------------------
def _fail_stream_upstream(monkeypatch: pytest.MonkeyPatch) -> None:
    """让所有流式尝试都失败（`stream_upstream` 抛 GatewayError）。"""

    def boom(*a: Any, **k: Any) -> Any:
        raise S.GatewayError("上游对 stream=true 能力性拒绝")

    monkeypatch.setattr(S, "stream_upstream", boom)


def test_degraded_stream_synthesizes_sse_from_non_stream_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """兜底路径的核心契约：客户端拿到**合法 SSE**，且如实标注 degraded。

    三件事一起钉：
    ① 帧序列是 `role 帧 → 正文帧 → degraded 说明 → [DONE]`（顺序即契约）；
    ② `degraded: true` 必须出现 —— 不标注的话，客户端以为自己拿到的是真流式，
       于是"首字延迟"这类指标会被静默污染；
    ③ 每帧都是 bytes（混进 str 会让客户端解析器崩）。
    """
    _fail_stream_upstream(monkeypatch)
    hub = _FakeHub([_entry()], chat_result=_chat_ok("兜底正文"))
    resp, _ = _call(monkeypatch, hub, lambda: iter(()))

    assert isinstance(resp, S.StreamingResponse), "兜底没有返回 SSE 响应"
    frames = _frames(resp)
    assert all(isinstance(f, bytes) for f in frames), f"混进了非 bytes 帧：{frames}"
    joined = b"".join(frames)

    assert frames[-1] == b"data: [DONE]\n\n", "末帧必须是 [DONE]"
    assert b'"assistant"' in joined, "缺少 role 帧（客户端靠它初始化消息）"
    assert "兜底正文".encode() in joined, "非流式拿到的正文没有进 SSE"
    assert b'"degraded": true' in joined or b'"degraded":true' in joined, (
        f"没有标注 degraded —— 客户端会把兜底当真实流式：{joined!r}"
    )
    # 兜底走的是**非流式**调用
    assert hub.chat_calls and hub.chat_calls[0]["stream"] is False, (
        f"兜底没走非流式：{hub.chat_calls}"
    )


def test_degraded_stream_records_one_successful_call_with_content_chars(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """兜底成功时台账要记一笔**成功**，且 `content_chars` = 正文长度。

    兜底对客户端是"成功"，台账也必须这么记 —— 记成失败会让成功率读数偏低；
    而不记 content_chars 会让用量统计少掉这部分正文（与首帧不计那次是同类问题）。
    """
    log = _call_rows(tmp_path, monkeypatch)
    _fail_stream_upstream(monkeypatch)
    hub = _FakeHub([_entry()], chat_result=_chat_ok("一二三", model="m-degraded"))
    resp, _ = _call(monkeypatch, hub, lambda: iter(()))
    _frames(resp)

    calls = [r for r in _rows_of(log) if r.get("type") == "call"]
    assert calls, "兜底没有落台账"
    last = calls[-1]
    assert last["success"] is True, f"兜底成功却记成了失败：{last}"
    assert last["model"] == "m-degraded", f"没记实际提供服务的模型：{last}"
    assert last["content_chars"] == 3, f"正文字数没记对：{last}"


def test_degraded_fallback_pool_exhausted_returns_502(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """流式与非流式**都**耗尽 → 502，且台账记下"两段都失败"的现场。

    这条是"网关确实无能为力"的路径。它必须明确报 502（不是 500、也不是空 200），
    并把 `stream+fallback both failed` 写进台账 ——
    否则运维只看到"客户端报错"，查不出是上游两段都不通。
    """
    log = _call_rows(tmp_path, monkeypatch)
    _fail_stream_upstream(monkeypatch)
    # chat_error 用默认（抛池耗尽）
    hub = _FakeHub([_entry()])
    # 这两条"兜底也失败"的路径是**在 `_stream_response` 里同步 raise** 的
    # （`attempt_stream()` 在函数体内就跑完了），不是等迭代响应体时才抛 ——
    # 我第一版把断言写在 `_frames()` 上，抛点对不上。
    with pytest.raises(HTTPException) as ei:
        _call(monkeypatch, hub, lambda: iter(()))
    assert ei.value.status_code == 502, f"应是 502 实际 {ei.value.status_code}"

    calls = [r for r in _rows_of(log) if r.get("type") == "call"]
    assert calls and calls[-1]["success"] is False
    assert "both failed" in (calls[-1]["error_msg"] or ""), f"台账没记下两段都失败：{calls[-1]}"


def test_degraded_fallback_gateway_error_uses_upstream_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """兜底抛 `GatewayError` 时用**上游的 http_status**（>=400 才用，否则 502）。

    保留上游状态码是让调用方能区分"我的请求有问题(4xx)"与"网关/上游坏了(502)"；
    一律 502 会把可自纠的错误也说成网关故障。
    """
    _fail_stream_upstream(monkeypatch)
    err = S.GatewayError("上游 429 限流", http_status=429)
    hub = _FakeHub([_entry()], chat_error=err)
    with pytest.raises(HTTPException) as ei:
        _call(monkeypatch, hub, lambda: iter(()))
    assert ei.value.status_code == 429, f"上游状态码没被保留：{ei.value.status_code}"


def test_degraded_fallback_gateway_error_without_status_is_502(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """上游没给状态码（或给了 <400 的怪值）时统一 502。"""
    _fail_stream_upstream(monkeypatch)
    hub = _FakeHub([_entry()], chat_error=S.GatewayError("说不清的错"))
    with pytest.raises(HTTPException) as ei:
        _call(monkeypatch, hub, lambda: iter(()))
    assert ei.value.status_code == 502


def test_degraded_disclosure_carries_the_original_stream_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """degraded 说明里必须带上**原始流式失败原因**。

    只写 "degraded: true" 而不给原因，排障的人还得去翻上游日志；
    带上原文（截断到 200 字符）就能当场看出是"能力性拒绝"还是别的。
    """
    _fail_stream_upstream(monkeypatch)
    hub = _FakeHub([_entry()], chat_result=_chat_ok("x"))
    resp, _ = _call(monkeypatch, hub, lambda: iter(()))
    joined = b"".join(_frames(resp))
    assert b"last_stream_error" in joined, f"没有带原始失败原因：{joined!r}"
    assert b"stream=true" in joined, "失败原因的内容没进去"


def test_degraded_synthesis_survives_non_ascii_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """合成 SSE 时中文/非 ASCII 必须按 UTF-8 编码（不能走默认 ascii）。"""
    _fail_stream_upstream(monkeypatch)
    text = "【结论】华东 120 万，同比 +12% 🎯"
    hub = _FakeHub([_entry()], chat_result=_chat_ok(text))
    resp, _ = _call(monkeypatch, hub, lambda: iter(()))
    joined = b"".join(_frames(resp))
    assert text.encode("utf-8") in joined, "中文在合成帧里被改动了"


# ---------------------------------------------------------------------------
# 剩余流式路径分支（2026-10-02 补）
# ---------------------------------------------------------------------------
def test_stream_with_empty_pool_returns_502(monkeypatch: pytest.MonkeyPatch) -> None:
    """池内一个可用模型都没有 → 502（不是 500、也不是空流）。

    运维把这个码读作"池配置/上游全不可用"，与"某个模型失败"是不同量级的故障。
    """
    _fail_stream_upstream(monkeypatch)
    hub = _FakeHub([])  # 空池
    with pytest.raises(HTTPException) as ei:
        _call(monkeypatch, hub, lambda: iter(()))
    assert ei.value.status_code == 502
    assert "没有可用模型" in str(ei.value.detail)


def test_stream_failover_records_a_switch_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """第一个模型流式失败、第二个成功时，必须落一条 **switch 事件**。

    这条覆盖 `325`（append_switch 标记）与 `340-342`（记账）。
    切换事件是运维回答"为什么这次调用走了备用模型"的唯一依据 ——
    不记的话，备用模型的用量会凭空出现而无从解释。
    """
    log = _call_rows(tmp_path, monkeypatch)
    # ⚠️ 不能自己 monkeypatch `stream_upstream`：`_call` 内部**总会**把它换成
    # 自己的替身（传进去的 `upstream` 回调才是唯一注入点）——
    # 我第一版在外面又 patch 了一遍，被 `_call` 覆盖，第一次调用根本没失败，
    # 链路压根没走到切换分支。
    calls = {"n": 0}

    def upstream() -> Iterator[bytes]:
        calls["n"] += 1
        if calls["n"] == 1:
            raise S.GatewayError("第一个模型流式挂了")
        yield b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
        yield b"data: [DONE]\n\n"

    hub = _FakeHub([_entry("primary"), _entry("backup")])
    resp, _ = _call(monkeypatch, hub, upstream)
    assert calls["n"] >= 2, f"只试了一次，没走到切换：{calls}"
    joined = b"".join(_frames(resp))
    assert b"ok" in joined, "第二个模型没有接上"

    switches = [r for r in _rows_of(log) if r.get("type") == "switch"]
    assert switches, f"没有落 switch 事件：{[r.get('type') for r in _rows_of(log)]}"
    assert switches[-1]["to_model"] == "backup", f"切到哪儿没记对：{switches[-1]}"
    assert hub.failure and hub.failure[0][0] == "primary", "失败的那个没记失败"


def test_stream_empty_first_chunk_is_treated_as_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """连接建立但**没有首块数据** → 当成失败（覆盖 `338`）。

    上游可能"200 但空流"，此时不能把空流交给客户端（客户端会一直等）。
    正确做法是记为失败并切下一个/走兜底。
    """

    def empty_first(base_url: str, api_key: str, payload: dict[str, Any], timeout: float):
        return "upstream-model", iter(())  # 空生成器

    monkeypatch.setattr(S, "stream_upstream", empty_first)
    hub = _FakeHub([_entry()], chat_result=_chat_ok("兜底接住"))
    resp, _ = _call(monkeypatch, hub, lambda: iter(()))
    joined = b"".join(_frames(resp))
    assert "兜底接住".encode() in joined, f"空首块没有被当成失败走兜底：{joined!r}"
