"""`pm/modelhub/streaming.py` 的边界分支回归（假上游，零出网）。

为什么单独一个文件：`operations.md` 薄弱点清单（2026-10-02 实测）记过
"streaming 的是上游收尾的几条 `except OSError`……能用假上游/桩补，别照上一版的结论放弃"。
2026-10-11 实测该文件 89%（11 条 miss：50-51 / 56 / 96 / 113-114 / 134 / 144-147），
本轮用假 urlopen / 假 resp 把它们全部走到：

- HTTPError 的响应体读不出来（50-51）：detail 兜底为空串，GatewayError 照抛；
- URLError 的 reason 带 timed out（55-56）：必须判成"读超时"而不是"连接失败"；
- 首个 data 块之后混进垃圾行（96）：丢弃，不影响后续透传；
- resp.close() 抛错（113-114）：收尾失败不许污染已透传的流；
- `sse_first_content` 的三个边：非 data 前缀的块（134）、超过 max_chars 截断且
  done=False（143-145）、坏 JSON 的块跳过不炸（146-147）。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

import pytest
from pm.modelhub.pool import GatewayError
from pm.modelhub.streaming import sse_first_content, stream_upstream


class _BadBody:
    def read(self) -> bytes:
        raise OSError("body already consumed")

    def close(self) -> None:
        pass


class _FakeResp:
    def __init__(
        self,
        lines: list[bytes],
        content_type: str = "text/event-stream",
        close_raises: bool = False,
    ) -> None:
        self._lines = lines
        self.headers = {"Content-Type": content_type}
        self._close_raises = close_raises

    def __iter__(self):
        return iter(self._lines)

    def close(self) -> None:
        if self._close_raises:
            raise OSError("close failed")


def _data_line(content: str = "你好", model: str = "m") -> bytes:
    chunk = {"id": "x", "model": model, "choices": [{"delta": {"content": content}}]}
    return f"data: {json.dumps(chunk, ensure_ascii=False)}".encode()


# --------------------------------------------------------------------------
# 连接阶段：异常体的边
# --------------------------------------------------------------------------
def test_httperror_body_read_failure_still_raises_gateway_error(monkeypatch) -> None:
    """响应体读不出来也必须抛带状态码的 GatewayError，不能换成裸 OSError。"""

    def fake_urlopen(req: Any, timeout: float | None = None) -> Any:
        raise urllib.error.HTTPError("http://x/v1", 500, "boom", {}, _BadBody())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(GatewayError) as ei:
        stream_upstream("http://x/v1", "k", {"model": "m"}, 5.0)
    assert "HTTP 500" in str(ei.value)
    assert ei.value.http_status == 500


def test_urlerror_timeout_reason_maps_to_read_timeout(monkeypatch) -> None:
    """URLError 的 reason 带 timed out → 判"读超时"（带 timeout 数），不是"连接失败"。"""

    def fake_urlopen(req: Any, timeout: float | None = None) -> Any:
        raise urllib.error.URLError(reason="The read operation timed out")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(GatewayError) as ei:
        stream_upstream("http://x/v1", "k", {"model": "m"}, 7.0)
    assert "读超时" in str(ei.value)
    assert "7" in str(ei.value)


# --------------------------------------------------------------------------
# 流阶段：垃圾行与收尾
# --------------------------------------------------------------------------
def test_garbage_line_after_first_chunk_is_dropped(monkeypatch) -> None:
    """首个 data 块之后混进的非法行必须被丢弃，后续 data 透传不受影响。"""
    resp = _FakeResp([_data_line("第一段"), b"garbage line", _data_line("第二段"), b"data: [DONE]"])
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: resp)
    _, chunks = stream_upstream("http://x/v1", "k", {"model": "m"}, 5.0)
    content, model, done = sse_first_content(chunks)
    assert content == "第一段第二段"
    assert model == "m"
    assert done is True


def test_resp_close_failure_does_not_break_stream(monkeypatch) -> None:
    """resp.close() 抛错只许吞掉：正文已透传、[DONE] 已补发，收尾失败不许炸调用方。"""
    resp = _FakeResp([_data_line("正文"), b"data: [DONE]"], close_raises=True)
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: resp)
    _, chunks = stream_upstream("http://x/v1", "k", {"model": "m"}, 5.0)
    content, _, done = sse_first_content(chunks)
    assert content == "正文"
    assert done is True


# --------------------------------------------------------------------------
# sse_first_content 的三个边
# --------------------------------------------------------------------------
def test_sse_first_content_ignores_non_data_chunks() -> None:
    """不带 data: 前缀的块跳过，不算 done 也不炸。"""
    content, model, done = sse_first_content(iter([b"NOT DATA\n\n", _data_line("正文") + b"\n\n"]))
    assert content == "正文"
    assert model == "m"
    assert done is False


def test_sse_first_content_stops_at_max_chars_without_done() -> None:
    """累计正文达到 max_chars 即停止消费后续块，done=False（调用方要能看出没走完）；
    已累计的正文原样返回，不做二次裁剪。"""
    content, model, done = sse_first_content(
        iter(
            [_data_line("hello world") + b"\n\n", _data_line("更多") + b"\n\n", b"data: [DONE]\n\n"]
        ),
        max_chars=5,
    )
    assert content == "hello world"
    assert model == "m"
    assert done is False


def test_sse_first_content_skips_invalid_json() -> None:
    """坏 JSON 的 data 块跳过不炸，后续正常块照常累计。"""
    content, _, done = sse_first_content(
        iter([b"data: {invalid\n\n", _data_line("正文") + b"\n\n", b"data: [DONE]\n\n"])
    )
    assert content == "正文"
    assert done is True
