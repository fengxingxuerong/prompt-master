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
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from pm.modelhub import server as S
from pm.modelhub.pool import ModelEntry
from pm.modelhub.server import ChatMessage, ChatRequest

_REQ = ChatRequest(messages=[ChatMessage(role="user", content="你好")], stream=True)


@pytest.fixture(autouse=True)
def isolate_ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """台账/用量写到 tmp_path，测试不往真实 data/ 里追加事件。"""
    monkeypatch.setenv("PMH_DATA_DIR", str(tmp_path))


class _FakeHub:
    """只实现 _stream_response 用到的三个方法。"""

    def __init__(self, entries: list[ModelEntry]) -> None:
        self._entries = entries
        self.success: list[str] = []
        self.failure: list[tuple[str, str]] = []

    def _ordered_enabled(self) -> list[ModelEntry]:
        return self._entries

    def _mark_success(self, entry: ModelEntry) -> None:
        self.success.append(entry.name)

    def _mark_failure(self, entry: ModelEntry, reason: str) -> None:
        self.failure.append((entry.name, reason))


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
