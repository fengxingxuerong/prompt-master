"""三个静态/口径缺陷的回归：页面脚本越界、通知条数恒 0、历史页转义。

全是 2026-10-31 只读审计实测出来的，且都是"Python 侧全绿、页面在自己骗人"那一类：

1. `cost-board.html` 的 `startLive` 被写在 `</html>` **之后**（不在任何 `<script>` 里），
   于是 `startLive()` 必抛 ReferenceError，被 `.catch` 接住 ⇒ 整块成本看板变成
   "加载失败：startLive is not defined（需经会审台服务 :8793 访问本页）"——
   归因还是错的。打开即 100% 复现。
2. `/api/notifications` 的 `count` 硬编码 0：实测 3 条告警时 `items` 有 3 条而 `count=0`，
   前端按 count 判空/显示角标 ⇒ 通知中心永远显示"没有告警"。
3. `history.html` 把工单主题、人工反馈与 LLM 输出裸拼 innerHTML（存储型 XSS）。
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

from fastapi.testclient import TestClient

STATIC = Path(__file__).resolve().parents[1] / "static"
_APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_APP_DIR))  # notify_center 同目录导入

# 与 test_triage.py 共用同一个模块对象：两个同名但不同对象的 app 会各持一份 `DATA`，
# monkeypatch 打在 A 上而端点读 B —— 那种"改了没生效"会让用例静默失去意义。
if "triage_app" in sys.modules:
    triage = sys.modules["triage_app"]
else:
    _spec = importlib.util.spec_from_file_location("triage_app", _APP_DIR / "app.py")
    _mod = importlib.util.module_from_spec(_spec)
    sys.modules["triage_app"] = _mod
    _spec.loader.exec_module(_mod)
    triage = _mod


def _read(page: str) -> str:
    return (STATIC / page).read_text(encoding="utf-8")


def _js(page: str) -> str:
    return "\n".join(re.findall(r"<script>(.*?)</script>", _read(page), re.S))


def test_cost_board_script_does_not_leak_past_the_html() -> None:
    """页面结束标签之后不许再有可执行内容，且 `startLive` 必须在本页的 script 里。"""
    src = _read("cost-board.html")
    # 用 rsplit：注释里可能提到那个标签，取**最后一个**才是真文档结尾
    tail = src.rsplit("</html>", 1)[-1].strip()
    assert not tail, f"文档结束标签之后还有残留（浏览器当散落文本，函数等于不存在）：{tail[:80]!r}"
    blocks = re.findall(r"<script>(.*?)</script>", src, re.S)
    assert blocks, "页面没有 script 块"
    js = "\n".join(blocks)
    assert "function startLive" in js, "startLive 不在 script 内 ⇒ 调用必抛 ReferenceError"


def test_cost_board_never_calls_an_undefined_local_helper() -> None:
    """本页定义的函数必须都在 script 内（`startLive` 越界就是这一条的具体形态）。"""
    js = _js("cost-board.html")
    for name in ("startLive", "load"):
        if f"{name}(" in js:
            assert re.search(rf"function\s+{name}\s*\(", js), (
                f"{name}() 被调用但本页的 script 里没有它的定义"
            )


def test_notification_count_matches_the_items() -> None:
    """`count` 必须是实际条数（旧版硬编码 0 ⇒ 通知中心永远"没有告警"）。"""
    client = TestClient(triage.app)
    body = client.get("/api/notifications").json()
    assert body["count"] == len(body["items"]), f"count 与 items 不一致：{body}"


def test_notification_count_reflects_written_alerts(tmp_path: Path, monkeypatch) -> None:
    """有告警时 count 要大于 0（否则"恒为 0"这个缺陷会被空库掩盖）。"""

    base = tmp_path
    (base / "data").mkdir()
    notif = base / "data" / "notifications.jsonl"
    notif.write_text(
        "\n".join(
            json.dumps({"ts": f"2026-10-31T00:00:0{i}", "kind": "alert", "text": "x"})
            for i in range(3)
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(triage, "DATA", base / "data")
    body = TestClient(triage.app).get("/api/notifications").json()
    assert body["count"] == 3, f"3 条告警却报 {body['count']} 条"


def test_history_page_escapes_untrusted_text() -> None:
    """工单主题/人工反馈/LLM 输出都要过 esc()（存储型 XSS 的入口）。

    判据：把 `esc(...)` 剥掉之后，这些字段不许还留在 JS 里 —— 只要有一处裸拼
    （`${s.subject}` / `${v.parse_error}`），一条 `<img src=x onerror=…>` 的工单主题
    就能让运营者打开历史页即执行。
    """
    src = _read("history.html")
    assert "function esc" in src, "历史页没有转义函数"
    js = _js("history.html")
    # 直接钉"裸插值形态"：比先剥 esc() 再匹配稳（esc 里可能有嵌套括号）
    for dangerous in (
        ">${s.subject",
        "${v.parse_error",
        "${v.error",
        "${JSON.stringify(vd",
        "${s.feedback.comment",
        "${c.action",
    ):
        assert dangerous not in js, f"发现未转义的插值 `{dangerous}`：存储型 XSS 入口"
    assert "esc(s.subject" in js and "esc(JSON.stringify(vd" in js, "转义没有真的接上去"
