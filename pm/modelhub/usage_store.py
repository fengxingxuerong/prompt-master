"""用量持久聚合（OPT：usage 持久化，替代"最近 2000 条事件"的易失口径）。

数据：data/usage_daily.json —— 结构 {"2026-09-21": {"calls": n, "success": n,
"latency_ms_sum": n, "content_chars_sum": n, "failovers_sum": n, "by_model": {...}}}
写入：原子写 + 文件锁，追加为增量合并（服务重启后历史保留）。

**"日"是哪个日**：`record_call` 用本地日期（`time.strftime`），所以重建也必须用本地日期
（见 `_day_key_from_ts`）——两边口径只要差一次时区换算，同一批调用就会被拆到两个键上，
增量记账与重建结果再也加不到一起。这个口径此前是隐含的，2026-10-16 显式化。

**本文件只是缓存，JSONL 台账才是事实源**：所以它坏了可以重建，见 `rebuild_from_ledger`。
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from .filelock import _atomic_write, _process_lock
from .notify import _notify
from .pool import _project_root

_LOCK = threading.Lock()

_SOURCE = "usage_store"


def _alert(msg: str) -> None:
    _notify(msg, source=_SOURCE)


def _path() -> Path:
    override = (os.getenv("PMH_DATA_DIR") or "").strip()
    root = Path(override) if override else _project_root() / "data"
    return root / "usage_daily.json"


def _day_key_from_ts(ts: str) -> str | None:
    """台账事件的 UTC 时间戳 → 本地日期键，与 `record_call` 的口径**必须一致**。

    台账写的是 UTC（`ledger._utcnow_iso`），日聚合记的是本地日——差这一次换算，
    凌晨那几个小时的调用会被重建到前一天去，`/v1/usage` 就与增量记账对不上。
    """
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.astimezone().strftime("%Y-%m-%d")


def rebuild_from_ledger() -> dict[str, Any]:
    """从 JSONL 台账**整份重算**日聚合（零调用、纯读）。

    为什么需要它：本文件是缓存，坏了原本只能"改名留存 + 从空重建"，也就是**当天及以前的
    日聚合全部归零**（实测 2026-10-15 前已累计 554 次这样的损坏）。而每一笔调用在台账里都
    有 `type=call` 的原文，重算得出来 ⇒ 损坏不该等于丢数据。

    口径与增量记账严格对齐：只吃 `type == "call"`；`success/latency_ms/content_chars/
    failovers` 逐个字段走同一个 `_merge`；坏行跳过（与 `query_ledger` 同款，不炸）。
    台账不存在或一行都读不出来时返回 `{}`——那时确实没得重建，调用方按空处理。
    """
    # 局部导入：ledger 与本模块互相都不在对方的导入链上（ledger 不用 usage_store）
    from . import ledger as LG

    ledger_path = LG.ledger_path

    path = ledger_path()
    out: dict[str, Any] = {}
    if not path.exists():
        return out
    try:
        raw = LG._read_text(path)  # 与 query_ledger 同款退避：并发写时不许把瞬时占用当损坏
    except OSError:
        return out
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue  # 坏行跳过：与 query_ledger 同口径，一条坏行不该毁掉整份重建
        if not isinstance(ev, dict) or ev.get("type") != "call":
            continue
        day_key = _day_key_from_ts(str(ev.get("ts") or ""))
        if day_key is None:
            continue
        _merge(
            out.setdefault(day_key, {}),
            success=bool(ev.get("success")),
            latency_ms=int(ev.get("latency_ms") or 0),
            failovers=int(ev.get("failovers") or 0),
            content_chars=int(ev.get("content_chars") or 0),
            model=str(ev.get("model") or "?"),
        )
    return out


def _load() -> dict[str, Any]:
    """加载日聚合；损坏时告警留存，并**先从台账重建**，重建不出来才退回空。

    不静默：损失/恢复都以两个途径可见——① 改名后的 .corrupt 文件本身；② stderr ALERT。
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
        rebuilt = rebuild_from_ledger()
        if rebuilt:
            # 重建结果当场落盘：下次读就是好文件，不用每次损坏都重扫一遍台账。
            # 这里刻意**不拿跨进程锁**——本函数已被 record_call 在临界区内调用，
            # 再取一次同文件的字节锁会把自己锁死（Windows 上同进程另一句柄拿不到）。
            try:
                _atomic_write(p, rebuilt)
            except OSError as werr:
                print(f"[usage_store] 重建结果写盘失败：{werr}")
            msg = (
                "[usage_store] ALERT: usage_daily.json corrupted ("
                + str(e)
                + "), saved as .corrupt-"
                + ts
                + "; rebuilt from ledger: "
                + str(len(rebuilt))
                + " day(s), "
                + str(sum(int(v.get("calls", 0)) for v in rebuilt.values()))
                + " call(s)（JSONL 是事实源，本次无数据损失）"
            )
        else:
            msg = (
                "[usage_store] ALERT: usage_daily.json corrupted ("
                + str(e)
                + "), saved as .corrupt-"
                + ts
                + "; 台账里重建不出任何一天，退回空聚合——当天用量读数会归零"
            )
        print(msg)
        _alert(msg)  # G-06：告警落盘，可追溯可接通知渠道
        return rebuilt


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
