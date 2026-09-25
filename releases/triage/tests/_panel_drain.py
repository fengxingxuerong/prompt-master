"""会审台测试共用的小工具：等后台会审线程收口，再让夹具退出。

为什么需要：`POST /api/reviews` 默认异步（G-07），会审线程里的 `append_ledger` /
`bump_health` 读的是**模块全局** `LEDGER` / `HEALTH_FILE`。monkeypatch 在夹具 teardown
时就还原了 ⇒ 线程还没跑完，记录就会被追加进**被 git 跟踪的** `releases/triage/data/`。
2026-09-25 深夜跑 CI 等价门禁时真的发生了：`ledger.jsonl` / `reviewer_health.json` /
`notifications.jsonl` 三个运营文件全被写脏。

看到 `done` 就等于写完了：终态由 `save_session` 落盘，而它排在 consensus 台账行之后。
"""

from __future__ import annotations

import sys
import threading
import time


def wait_panel_done(client, sid: str, headers: dict | None = None,
                    tries: int = 120, delay: float = 0.1) -> dict:
    """轮询到会话落终态才返回；超时抛错（不放行"可能还在写"的情况）。

    超时那条路会把还在跑的 `panel-*` 线程的栈打出来 —— 这个等待本身就用来定位
    "会话为什么没收口"，没有栈的话下一次还是猜。
    """
    for _ in range(tries):
        d = client.get(f"/api/sessions/{sid}", headers=headers or {}).json()
        if d.get("status") == "done":
            return d
        time.sleep(delay)
    raise AssertionError(
        f"后台会审 {tries * delay:.0f}s 内未收口：{sid}\n{_panel_stacks()}"
    )


def _panel_stacks() -> str:
    """所有 panel-* 线程当前卡在哪（超时现场，别让它只留一句"未收口"）。"""
    import traceback as _tb

    frames = sys._current_frames()
    out = []
    for t in threading.enumerate():
        if not t.name.startswith("panel"):
            continue
        stack = _tb.format_stack(frames.get(t.ident)) if t.ident in frames else []
        out.append(f"--- {t.name} (alive={t.is_alive()}) ---\n" + "".join(stack[-6:]))
    return "\n".join(out) or "(没有活的 panel-* 线程：会审线程已经退出但没写终态)"
