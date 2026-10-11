"""`ModelHub._http_post_json` —— 出网那一层的失败语义回归（假 urlopen，零出网）。

为什么单独一个文件：模块 docstring 开篇就承诺"失败语义"（连接错误/超时/401/403/404/
429/5xx/假成功都会判本模型不可用），但这条承诺**只在这个 30 行的函数里兑现**：
urlopen 抛来的原始异常全都要在它这里被翻译成 `GatewayError` + `http_status`/`reason`，
否则上层 `chat()` 按类型决定"原地重试还是切换"的整个分支图就没有输入。

它不是纯粹透传：URLError 里还要再嗅一次 reason 字符串（socket 超时裹在 URLError 里，
`reason` 是 `timed out` 文本而不是独立异常类型），这一层是"同一个真实故障在不同
Python/平台上表现不同"的适配点，也正是没人敢改的原因。这里的用例把它钉住：

- 每种网络故障各是什么异常类型、抛出来的 GatewayError 带什么 `reason`（写台账/切换依据）；
- 请求头/请求体到底发对了没（Bearer 拼错 ⇒ 症状是"那个端点一直 401"）；
- 200 但不是 JSON / 是 JSON 但不是对象 —— `chat()` 靠 `_validate_chat_response` 判假成功。
"""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
from typing import Any

import pytest
from pm.modelhub import pool as P
from pm.modelhub.pool import GatewayError, ModelHub

PAYLOAD = {"model": "up-m", "messages": [{"role": "user", "content": "你好"}]}


class _FakeResponse(io.BytesIO):
    """够用的 `urlopen` 上下文管理器：默认 200，带 `.status`。"""

    status = 200


def _resp(body: bytes | str, status: int = 200) -> _FakeResponse:
    raw = body.encode("utf-8") if isinstance(body, str) else body
    return _FakeResponse(raw)


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> list[urllib.request.Request]:
    """抓住真正发出去的 `Request`：头/体对不对只能从这里看。"""
    seen: list[urllib.request.Request] = []

    def fake_urlopen(req: urllib.request.Request, timeout: float | None = None) -> _FakeResponse:
        seen.append(req)
        return _resp(json.dumps({"choices": [{"message": {"content": "ok"}}]}))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return seen


@pytest.fixture
def raise_from_upstream(monkeypatch: pytest.MonkeyPatch) -> Any:
    """把指定异常从 urlopen 抛出（模拟真实网络栈给我们的东西）。"""

    def install(exc: BaseException) -> None:
        def boom(req: urllib.request.Request, timeout: float | None = None) -> _FakeResponse:
            raise exc

        monkeypatch.setattr(urllib.request, "urlopen", boom)

    return install


# --------------------------------------------------------------------------
# 请求本身
# --------------------------------------------------------------------------
def test_sends_bearer_head_and_json_body(captured: list[urllib.request.Request]) -> None:
    """`Bearer <key>` 拼错一位、或漏了 Content-Type，症状都是"上游一直 401/415"。"""
    ModelHub._http_post_json("http://x/v1/chat/completions", "sk-secret", PAYLOAD, 5.0)
    req = captured[0]
    assert req.get_header("Authorization") == "Bearer sk-secret"
    assert req.get_header("Content-type") == "application/json"
    assert req.get_method() == "POST"
    assert json.loads(req.data.decode("utf-8")) == PAYLOAD


def test_non_utf8_body_does_not_explode(monkeypatch: pytest.MonkeyPatch) -> None:
    """上游回了非法 UTF-8：`errors="replace"` 保证走到"不是 JSON"而不是抛 UnicodeDecodeError ——
    后者不属于 GatewayError，上层会当未知异常直接崩掉整个请求。"""

    def fake_urlopen(req: Any, timeout: float | None = None) -> _FakeResponse:
        return _FakeResponse(b"\xff\xfe not json at all")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(GatewayError) as e:
        ModelHub._http_post_json("http://x/v1", "k", PAYLOAD, 5.0)
    assert "不是 JSON" in str(e.value)


# --------------------------------------------------------------------------
# HTTP 状态码
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "status",
    [400, 401, 403, 404, 429, 500, 502, 503, 504],
)
def test_http_error_carries_status_and_reason(raise_from_upstream: Any, status: int) -> None:
    """401/403/404/429/5xx/4xx 都必须带上 http_status —— `chat()` 的"瞬时 or 切换"判定、
    台账的 `reason` 全靠它，丢了就把可切换故障降级成未知故障。"""
    err = urllib.error.HTTPError("http://x/v1", status, "boom", {}, io.BytesIO(b'{"error":"nope"}'))
    raise_from_upstream(err)
    with pytest.raises(GatewayError) as e:
        ModelHub._http_post_json("http://x/v1", "k", PAYLOAD, 5.0)
    assert e.value.http_status == status
    assert f"HTTP {status}" in str(e.value)
    assert "nope" in str(e.value), "响应体片段要进错误文本（运维拿它定位上游）"
    assert (
        e.value.reason
        == {
            400: "gateway_error",
            401: "auth_error",
            403: "auth_error",
            404: "model_not_found",
            429: "rate_limited",
            500: "upstream_5xx",
            502: "upstream_5xx",
            503: "upstream_5xx",
            504: "upstream_5xx",
        }[status]
    )


def test_http_error_still_gets_a_reason_when_body_is_empty(
    raise_from_upstream: Any,
) -> None:
    """上游 500 且不回正文（裸 kill）时不能因为没有 detail 就丢掉 reason。"""
    raise_from_upstream(urllib.error.HTTPError("http://x", 500, "boom", {}, io.BytesIO(b"")))
    with pytest.raises(GatewayError) as e:
        ModelHub._http_post_json("http://x/v1", "k", PAYLOAD, 5.0)
    assert e.value.http_status == 500
    assert e.value.reason == "upstream_5xx"


# --------------------------------------------------------------------------
# 连接层故障
# --------------------------------------------------------------------------
def test_connection_refused_is_a_connection_error(raise_from_upstream: Any) -> None:
    raise_from_upstream(urllib.error.URLError("connection refused"))
    with pytest.raises(GatewayError) as e:
        ModelHub._http_post_json("http://x/v1", "k", PAYLOAD, 5.0)
    assert e.value.reason == "connection_error"
    assert "连接失败" in str(e.value)


def test_urlerror_wrapping_a_timeout_is_classified_as_timeout(
    raise_from_upstream: Any,
) -> None:
    """socket 超时在 Python 里常以 `URLError(reason=timed out)` 出现而不是独立类型
    （Windows 上更常见）。嗅不出来 ⇒ 走"连接失败"分支 ⇒ `chat()` 认为非瞬时，直接
    切换模型而不是原地重试，白白跳过一次本可成功的复测。"""
    raise_from_upstream(urllib.error.URLError("timed out"))
    with pytest.raises(GatewayError) as e:
        ModelHub._http_post_json("http://x/v1", "k", PAYLOAD, 5.0)
    assert e.value.reason == "timeout"
    assert "读超时" in str(e.value)


def test_bare_timeout_error_is_a_timeout(raise_from_upstream: Any) -> None:
    raise_from_upstream(TimeoutError("read timed out"))
    with pytest.raises(GatewayError) as e:
        ModelHub._http_post_json("http://x/v1", "k", PAYLOAD, 5.0)
    assert e.value.reason == "timeout"


def test_generic_oserror_becomes_network_gateway_error(
    raise_from_upstream: Any,
) -> None:
    """Windows 上连接中止表现为 OSError(WSAECONNABORTED)，不是 URLError/TimeoutError。
    它必须仍是 GatewayError，否则 chat() 的 `except GatewayError` 捕不住，
    一次抖动就把整个请求炸出调用方。"""
    raise_from_upstream(OSError(10053, "远程主机强迫关闭了一个现有的连接"))
    with pytest.raises(GatewayError) as e:
        ModelHub._http_post_json("http://x/v1", "k", PAYLOAD, 5.0)
    assert "网络错误" in str(e.value)
    assert e.value.reason == "connection_error"


# --------------------------------------------------------------------------
# 200 但正文不对
# --------------------------------------------------------------------------
def test_status_ok_with_non_json_body_is_a_gateway_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """上游把错误页/占位 HTML 当 200 返回：必须判失败，不能当成功传上去。"""
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda req, timeout=None: _resp("<html>维护中</html>"),
    )
    with pytest.raises(GatewayError) as e:
        ModelHub._http_post_json("http://x/v1", "k", PAYLOAD, 5.0)
    assert "不是 JSON" in str(e.value)
    assert "status=200" in str(e.value)


def test_status_ok_with_json_but_not_an_object_is_passed_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """200 + 合法 JSON 但不是对象（`[1,2]`）：本函数透出非对象是**有意设计**，
    形状校验归 `_validate_chat_response`。这里钉住"不能在这里自己抛别的异常"。"""
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: _resp("[1, 2, 3]"))
    out = ModelHub._http_post_json("http://x/v1", "k", PAYLOAD, 5.0)
    assert isinstance(out, list), "意外替调用方做了形状校验（契约在 _validate_chat_response）"


def test_status_200_non_utf8_body_is_not_a_gateway_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`errors="replace"` 不能把合法但奇特的 200 变成异常，只要 JSON 能解出来就当成功。"""
    payload = json.dumps({"choices": [{"message": {"content": "你好"}}]}).encode("utf-8")
    # 前面塞一段 latin-1 噪音：JSON 解析器会忽略前导空白之外的字符吗？不会 ——
    # 所以这里测的是"UTF-8 正常但夹带不可打印字符"，替换后仍必须是合法 JSON。
    body = payload.replace(b'"', b'"')
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: _resp(body))
    out = ModelHub._http_post_json("http://x/v1", "k", PAYLOAD, 5.0)
    assert out["choices"][0]["message"]["content"] == "你好"


def test_http_error_body_read_failure_falls_back_to_status_only(
    raise_from_upstream: Any,
) -> None:
    """上游 401/500 且 body 句柄已坏（read() 自己抛）：不能因为"摘详细原因"这一步失败，
    就把真正的 HTTP 状态码故障一起吃成裸 BUG。`# noqa: BLE001` 那个 except 就是钉这个。"""
    err = urllib.error.HTTPError("http://x", 401, "boom.io", {}, io.BytesIO(b""))

    def _boom(*a: Any, **k: Any) -> None:
        raise ValueError("body handle already closed")

    err.read = _boom  # type: ignore[method-assign]
    raise_from_upstream(err)
    with pytest.raises(GatewayError) as e:
        ModelHub._http_post_json("http://x/v1", "k", PAYLOAD, 5.0)
    assert e.value.http_status == 401
    assert e.value.reason == "auth_error"


def test_config_env_none_of_this_needs_a_live_network(tmp_path: Any) -> None:
    """构造 ModelHub 不需要出网（读本地配置），本文件也不该有真实请求 ——
    这里断言兜底：如果哪天有人把上面某个假 urlopen 改成真打，这条会先红。"""

    class _Guard:
        def __call__(self, *args: Any, **kwargs: Any) -> None:
            raise AssertionError("本文件的用例不允许出网")

    cfg = tmp_path / "modelhub.json"
    cfg.write_text(
        json.dumps({"pool": [{"name": "a", "base_url": "http://127.0.0.1:9/v1", "api_key": "k"}]}),
        encoding="utf-8",
    )
    import contextlib

    with contextlib.ExitStack() as stack:
        stack.enter_context(pytest.MonkeyPatch.context())
        P.load_dotenv = None  # 不读项目 .env，避免本机 PMH_* 污染
        hub = ModelHub(config_path=cfg)
        assert hub.list_models()


def test_pool_gateway_reason_prefers_status_over_text() -> None:
    """`reason` 的优先级：有状态码就以状态码为准（文本里出现"超时"二字也不能覆盖）。"""
    assert GatewayError("连接超时", http_status=500).reason == "upstream_5xx"
    assert GatewayError("上游 500 超时重试").reason == "timeout"
    assert GatewayError("未知怪事").reason == "gateway_error"
