"""`pm/modelhub/streaming.py` 的回归 —— 对**本机真 HTTP 服务**打流式请求（零外部出网）。

为什么值得单独一个文件：这个模块实测覆盖率原本只有 7%，而它是"网关对外承诺 stream=true"
的**上游侧**。2026-09-25 排查流式通道时（`tests/test_modelhub_stream_contract.py` 那条
丢了 `return` 的缺陷）顺带看清了另一半：上游可能 ① 不按 SSE 回（直接给 JSON）、
② 没发 [DONE] 就断、③ 用 `: comment` 做心跳、④ 直接 401/404 拒绝流式能力
（实测商汤 Token Plan 同一时刻非流式正常、流式被拒）。这四条当时**一条都没有用例**，
只写在 docstring 里。

本机起一个真 HTTP 服务而不是 monkeypatch urlopen：这个模块的全部难点都在
"字节怎么从 socket 变成帧"，把 urlopen 换掉就等于把被测对象换掉了。
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from pm.modelhub.pool import GatewayError
from pm.modelhub.streaming import sse_first_content, stream_upstream


def _chunk(text: str, model: str = "up-1", finish: str | None = None) -> str:
    obj = {
        "id": "chatcmpl-1",
        "object": "chat.completion.chunk",
        "model": model,
        "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": finish}],
    }
    return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"


class _Handler(BaseHTTPRequestHandler):
    """按路径**首段**分派剧本。每个剧本对应上游的一种真实形态。

    两个刻意的设计：
    - stream_upstream 会往 base_url 尾巴接 `/chat/completions`，所以完整 path 形如
      `/ok/chat/completions`。上一版用例直接比 `path == "/ok"`，全部落进兜底分支还"绿"了
      —— 分派必须落到真实结构上。
    - **认不出的剧本返回 404，绝不兜底**：兜底会让写错的用例拿到别的答案通过（就是上一版的事故）。
    """

    def log_message(self, *args: object) -> None:  # 静默，别把测试输出淹掉
        return

    def _drain_request_body(self) -> None:
        """回响应之前先把请求体读干净（stub 正确性；**不是**已证实的抖动解药，见下）。

        不读的后果：`BaseHTTPRequestHandler` 默认 HTTP/1.0，写完响应就关连接，
        而客户端的请求体可能还在路上 ⇒ Windows 直接 RST ⇒ 客户端读到
        `OSError: [WinError 10053]`，用例报成"拿到的不是预期的 GatewayError"。

        实测这个抖动有多稀疏（同一台机器、同一个文件）：
        - 加这条之前：一次全量套件 3/11 红、一次单跑 1/11 红（三条红**同一签名** 10053）；
        - 只摘掉这一行、其余不动：12 连跑红 1 次，再 25 连跑红 0 次；
        - 带着这一行：12 + 25 连跑全绿，全量套件全绿。
        样本太小，**不足以证明它就是把抖动修好的那一样东西**。所以这条按"stub 本该读请求体"
        留着，同时把话说在前头：本文件将来再红，先查签名是不是 10053（连接层），
        别把它当成 `pm/modelhub/streaming.py` 坏了。
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > 0:
            self.rfile.read(length)

    def _sse(self, body: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for line in body.splitlines(keepends=True):
            self.wfile.write(line.encode("utf-8"))
            self.wfile.flush()  # 真分块下发：不分块就测不出"逐行解析"

    def do_POST(self) -> None:
        self._drain_request_body()
        script = self.path.strip("/").split("/")[0]
        if script == "401":
            body = b'{"error":{"message":"stream not supported"}}'
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if script == "slow":
            time.sleep(0.6)  # 只需盖过用例里 0.1s 的客户端超时；挂久了会污染同一监听口上的后续连接
            self._sse(_chunk("too late") + "data: [DONE]\n\n")
            return
        if script == "json":  # 上游忽略 stream=true，直接给一个非流式 JSON
            payload = json.dumps(
                {
                    "id": "chatcmpl-j",
                    "model": "up-json",
                    "choices": [
                        {"index": 0, "message": {"role": "assistant", "content": "整段正文"}}
                    ],
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if script == "garbage":  # 声称 JSON 却给不可解析的字节
            body = b"not json at all"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        scripts = {
            # 正常：两帧 + [DONE]
            "ok": _chunk("你好") + _chunk("世界") + "data: [DONE]\n\n",
            # 上游没发 [DONE] 就断流（正文已透传，网关要替客户端补收尾）
            "no-done": _chunk("半段") + _chunk("就断"),
            # SSE 注释心跳必须被丢掉，不能当正文透传
            "comment": ": keep-alive 1\n\n" + _chunk("真内容") + "data: [DONE]\n\n",
            # 无空格写法 data:{...} 与 data:[DONE] 也是上游实测会发的形态
            "compact": "data:"
            + json.dumps({"model": "up-c", "choices": [{"index": 0, "delta": {"content": "x"}}]})
            + "\n\ndata:[DONE]\n\n",
        }
        if script not in scripts:
            body = f'{{"error":"unknown script {script}"}}'.encode()
            self.send_response(404)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self._sse(scripts[script])


class _StubServer(ThreadingHTTPServer):
    """`slow` 剧本会让客户端提前挂断 —— 那是**被测行为**，不是服务器的错误。

    默认实现在服务端打整段 traceback（`Exception occurred during processing of request
    from (...)`），混在 pytest 输出里就像真出了事。只吞掉"对端先走了"这一类，
    其余异常仍然交给基类（否则这条静默会把真故障一起藏掉）。
    """

    def handle_error(self, request: object, exception: Exception) -> None:
        if isinstance(exception, ConnectionError):
            return
        super().handle_error(request, exception)


@pytest.fixture(scope="module")
def origin() -> Iterator[str]:
    """本机 stub 的源地址（`/剧本名` 由 stream_upstream 自己补成 .../chat/completions）。"""
    server = _StubServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="sse-stub")
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def _collect(chunks: Iterator[bytes]) -> list[bytes]:
    return list(chunks)


def test_unknown_script_is_404_not_a_silent_fallback(origin: str) -> None:
    """这条是给测试自己用的守卫：剧本名拼错时必须炸，不能拿别的剧本悄悄通过。"""
    with pytest.raises(GatewayError) as e:
        stream_upstream(origin + "/typoed-script-name", "sk-x", {"model": "m"}, 5)
    assert e.value.http_status == 404


def test_normal_sse_is_passed_through_frame_by_frame(origin: str) -> None:
    model, gen = stream_upstream(origin + "/ok", "sk-x", {"model": "m"}, 10)
    frames = _collect(gen)
    assert model == "m"
    assert len(frames) == 3, f"两帧正文 + 一条 DONE：{frames}"
    assert frames[-1] == b"data: [DONE]\n\n"
    body = b"".join(frames).decode()
    assert "你好" in body and "世界" in body


def test_stream_ending_without_done_still_terminates(origin: str) -> None:
    """上游断在半路：客户端仍要收到 [DONE]，否则它一直等（正文其实已完整下发）。"""
    _model, gen = stream_upstream(origin + "/no-done", "sk-x", {"model": "m"}, 10)
    frames = _collect(gen)
    assert frames[-1] == b"data: [DONE]\n\n", frames
    assert len(frames) == 3


def test_sse_comment_frames_are_dropped(origin: str) -> None:
    """`: keep-alive` 是心跳注释，透传给客户端会被当成数据帧解析。"""
    _model, gen = stream_upstream(origin + "/comment", "sk-x", {"model": "m"}, 10)
    frames = _collect(gen)
    assert not any(f.startswith(b":") for f in frames), frames
    assert len(frames) == 2, f"只剩正文帧 + DONE：{frames}"


def test_compact_data_form_without_space_is_accepted(origin: str) -> None:
    _model, gen = stream_upstream(origin + "/compact", "sk-x", {"model": "m"}, 10)
    frames = _collect(gen)
    assert frames[-1] == b"data: [DONE]\n\n"
    assert b"x" in frames[0]


def test_upstream_ignoring_stream_returns_synth_frame(origin: str) -> None:
    """上游把 stream=true 当没看见（实测商汤某些套餐）：读完整段 JSON，合成一帧 + DONE，
    客户端契约不变。"""
    model, gen = stream_upstream(origin + "/json", "sk-x", {"model": "req-m"}, 10)
    frames = _collect(gen)
    assert model == "up-json"
    assert len(frames) == 2 and frames[-1] == b"data: [DONE]\n\n"
    obj = json.loads(frames[0].decode()[len("data:") :].strip())
    assert obj["object"] == "chat.completion.chunk"
    assert obj["choices"][0]["delta"]["content"] == "整段正文"
    assert obj["choices"][0]["finish_reason"] == "stop"


def test_capability_rejection_becomes_gateway_error_with_status(origin: str) -> None:
    """401/404 这类"能力性拒绝"必须带上 http_status —— 上层降级兜底靠它区分。"""
    with pytest.raises(GatewayError) as e:
        stream_upstream(origin + "/401", "sk-x", {"model": "m"}, 10)
    assert e.value.http_status == 401
    assert "stream not supported" in str(e.value)


def test_unparsable_non_stream_body_is_a_gateway_error(origin: str) -> None:
    with pytest.raises(GatewayError) as e:
        stream_upstream(origin + "/garbage", "sk-x", {"model": "m"}, 10)
    assert "不是 JSON" in str(e.value)


def test_unreachable_upstream_is_a_gateway_error() -> None:
    """端口没人听：必须抛语义异常而不是把 URLError 透给上层（上层按类型决定切不切）。"""
    with pytest.raises(GatewayError):
        stream_upstream("http://127.0.0.1:9/v1", "sk-x", {"model": "m"}, 3)


def test_sse_first_content_aggregates_and_reports_done(origin: str) -> None:
    _model, gen = stream_upstream(origin + "/ok", "sk-x", {"model": "m"}, 10)
    content, seen_model, done = sse_first_content(gen)
    assert done is True
    assert content == "你好世界"
    assert seen_model == "up-1"


def test_slow_upstream_raises_gateway_error(origin: str) -> None:
    """超时也必须是 GatewayError。顺带把"报成什么话"钉住：urlopen 阶段 socket.timeout 是
    OSError 的子类（Windows 上表现为 10053 连接中止），走的是"网络错误"这条措辞而不是
    "读超时" —— 换措辞要连这条一起改。放文件最后：它会故意留下一条被中止的连接。"""
    with pytest.raises(GatewayError) as e:
        stream_upstream(origin + "/slow", "sk-x", {"model": "m"}, 0.1)
    msg = str(e.value)
    assert "网络错误" in msg or "超时" in msg, msg
