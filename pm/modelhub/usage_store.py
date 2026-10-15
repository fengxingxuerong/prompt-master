"""用量持久聚合（OPT：usage 持久化，替代"最近 2000 条事件"的易失口径）。

数据：data/usage_daily.json —— 结构 {"2026-09-21": {"calls": n, "success": n,
"latency_ms_sum": n, "content_chars_sum": n, "failovers_sum": n, "by_model": {...}}}
写入：原子写 + 文件锁，追加为增量合并（服务重启后历史保留）。
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, cast

from .filelock import _atomic_write, _process_lock
from .pool import _project_root

_LOCK = threading.Lock()


def _notify(msg: str) -> None:
    """G-06 告警落盘：追加 logs/notifications.jsonl（时间戳+来源+消息）。"""
    import json as _json
    from pathlib import Path as _P

    try:
        nlog = _P(__file__).resolve().parents[2] / "logs" / "notifications.jsonl"
        nlog.parent.mkdir(parents=True, exist_ok=True)
        with open(nlog, "a", encoding="utf-8") as f:
            import time as _t

            f.write(
                _json.dumps(
                    {
                        "ts": _t.strftime("%Y-%m-%dT%H:%M:%S"),
                        "source": "usage_store",
                        "message": msg,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    except Exception:  # noqa: BLE001 - 通知失败静默
        pass


def _path() -> Path:
    override = (os.getenv("PMH_DATA_DIR") or "").strip()
    root = Path(override) if override else _project_root() / "data"
    return root / "usage_daily.json"


def _load() -> dict[str, Any]:
    """加载日聚合；损坏时告警化处理：坏文件改名留存（.corrupt-<ts>），从空重建。

    不静默：损失以两个途径可见——① 改名后的 .corrupt 文件本身；② stderr ALERT 输出。
    （JSONL 台账始终是完整事实源，本文件只是聚合缓存。）
    """
    p = _path()
    if not p.exists():
        return {}
    try:
        return cast("dict[str, Any]", json.loads(p.read_text(encoding="utf-8")))
    except json.JSONDecodeError as e:
        ts = time.strftime("%Y%m%d-%H%M%S")
        try:
            p.replace(p.with_suffix(".json.corrupt-" + ts))
        except OSError:
            pass
        msg = (
            "[usage_store] ALERT: usage_daily.json corrupted ("
            + str(e)
            + "), saved as .corrupt-"
            + ts
            + "; rebuilt empty; JSONL source-of-truth unaffected"
        )
        print(msg)
        _notify(msg)  # G-06：告警落盘，可追溯可接通知渠道
        return {}


def _merge(
    day: dict[str, Any],
    *,
    success: bool,
    latency_ms: int,
    failovers: int,
    content_chars: int,
    model: str,
) -> None:
    day["calls"] = day.get("calls", 0) + 1
    if success:
        day["success"] = day.get("success", 0) + 1
    day["latency_ms_sum"] = day.get("latency_ms_sum", 0) + int(latency_ms or 0)
    day["content_chars_sum"] = day.get("content_chars_sum", 0) + int(content_chars or 0)
    day["failovers_sum"] = day.get("failovers_sum", 0) + int(failovers or 0)
    bm = day.setdefault("by_model", {})
    ent = bm.setdefault(model, {"calls": 0, "success": 0})
    ent["calls"] += 1
    if success:
        ent["success"] += 1


def record_call(
    *,
    model: str,
    agent: str,
    role: str,
    success: bool,
    latency_ms: int,
    failovers: int,
    content_chars: int,
) -> None:
    """追加一笔调用到当日聚合（进程内加锁 + 跨进程锁 + 原子写）。

    2026-10-15 修：原先只锁本进程（`_LOCK`）、临时文件名又是**固定的**
    `usage_daily.json.tmp`。多进程部署下两个写者共用一个临时文件，交错写出的
    是"两个 JSON 对象首尾相接"的半截内容，replace 之后整份日聚合当场不可解析 ——
    实测当天 22:25~23:02 连着 5 条 `usage_store ALERT`（JSONL 事实源没丢，
    但 `/v1/usage` 与看板读到的日聚合被整份清空重建）。
    这与 vkeys 2026-09-25 / 10-13 那两次是同族事故，故直接共用 `filelock` 那份实现。
    """
    day_key = time.strftime("%Y-%m-%d")
    p = _path()
    with _LOCK, _process_lock(p):
        data = _load()
        day = data.setdefault(day_key, {})
        _merge(
            day,
            success=success,
            latency_ms=latency_ms,
            failovers=failovers,
            content_chars=content_chars,
            model=model,
        )
        _atomic_write(p, data)


def load_daily() -> dict[str, Any]:
    with _LOCK:
        return _load()
