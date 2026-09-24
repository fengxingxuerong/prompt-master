"""M3.6 IM 通知通道测试（2026-09-24）。

覆盖：三种 IM 平台 payload 渲染（飞书/企微/钉钉）、generic 向后兼容、
本地 mock 端点实收验证（零外部网络）、webhook 失败静默、禁用跳过。
自包含加载（无 conftest，避免与 lobster 的 conftest.py 合跑冲突）。
"""
from __future__ import annotations

import importlib.util
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

_nc_path = Path(__file__).resolve().parents[1] / "notify_center.py"
_spec = importlib.util.spec_from_file_location("notify_center_mod", _nc_path)
nc = importlib.util.module_from_spec(_spec)
sys.modules["notify_center_mod"] = nc
_spec.loader.exec_module(nc)


# ---------------------------------------------------------------------------
# payload 渲染
# ---------------------------------------------------------------------------
def test_render_feishu():
    rec = {"level": "warn", "source": "watchdog", "message": "服务掉线"}
    p = nc.render_payload(rec, "feishu")
    assert p["msg_type"] == "text" and p["content"]["text"] == "[warn][watchdog] 服务掉线"


def test_render_wecom_and_dingtalk():
    rec = {"level": "info", "source": "triage", "message": "会审完成"}
    w = nc.render_payload(rec, "wecom")
    assert w["msgtype"] == "text" and w["text"]["content"] == "[info][triage] 会审完成"
    d = nc.render_payload(rec, "dingtalk")
    assert d["msgtype"] == "text" and d["text"]["content"] == "[info][triage] 会审完成"


def test_render_generic_backward_compat():
    """generic 与未知类型：原样返回记录 JSON（既有行为不变）。"""
    rec = {"level": "info", "source": "x", "message": "m"}
    assert nc.render_payload(rec, "generic") is rec
    assert nc.render_payload(rec, "unknown-type") is rec
    assert nc.render_payload(rec, "") is rec


# ---------------------------------------------------------------------------
# mock 端点实收验证（零外部网络）
# ---------------------------------------------------------------------------
class _Capture(BaseHTTPRequestHandler):
    received: ClassVar[list[bytes]] = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.__class__.received.append(self.rfile.read(length))
        self.send_response(200)
        self.end_headers()

    def log_message(self, *a):
        pass


def test_webhook_sent_with_im_payload(tmp_path):
    _Capture.received = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Capture)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/hook"
        nc.save_config(tmp_path, {"webhook_url": url, "enabled": True, "webhook_type": "feishu"})
        r = nc.notify("watchdog", "IM 通道测试", level="warn", base_dir=tmp_path)
        assert r["file"] is True and r["webhook"] == "sent"
        assert len(_Capture.received) == 1
        body = json.loads(_Capture.received[0].decode("utf-8"))
        assert body["msg_type"] == "text"
        assert "[warn][watchdog]" in body["content"]["text"]
    finally:
        server.shutdown()
        server.server_close()


def test_webhook_failure_silent(tmp_path):
    """不可达 webhook：failed 标记，绝不抛出（通知不阻断业务）。"""
    nc.save_config(tmp_path, {"webhook_url": "http://127.0.0.1:9/nope",
                              "enabled": True, "webhook_type": "wecom"})
    r = nc.notify("triage", "会失败", base_dir=tmp_path)
    assert r["file"] is True and r["webhook"] == "failed"


def test_disabled_skips_webhook(tmp_path):
    nc.save_config(tmp_path, {"webhook_url": "http://127.0.0.1:9/nope",
                              "enabled": False, "webhook_type": "feishu"})
    r = nc.notify("triage", "被跳过", base_dir=tmp_path)
    assert r["webhook"] == "skipped" and r["file"] is True
