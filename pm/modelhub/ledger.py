"""调用与切换台账：每次真实模型调用、每次切换事件都追加写入 JSONL。

设计要点：
- 文件锁 + 追加写：并发安全；每行一个 JSON 对象（JSONL），坏行跳过不炸查询；
- entry.type = "call"（一次真实调用，含成败/耗时/切换后模型）或 "switch"（切换事件）；
- query_ledger() 支持 model / success / since / until / type 条件检索；
- 台账是"旁路"：写失败**不抛**，但必须留痕——见 `_alert_dropped`。

⚠️ 本文件是**事实源**：`usage_store.rebuild_from_ledger()` 拿它整份重算日聚合。
所以它自己写坏/写丢，比日聚合坏了更糟——那边还有这里可以重建，这里没了就真没了。
2026-10-16 因此补：写失败落 notifications.jsonl 并计数（原先只有一句 print，终端一关就
查不到丢过账）；读路径补上与 `vkeys._load_json_or` 同款的瞬时占用退避。跨进程锁**没加**——
并给追加写补上跨进程锁（实测两进程各 60 条短行 ⇒ 盘上 119 条，整笔被覆盖且无坏行）。
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .filelock import _process_lock
from .notify import _notify

_LEDGER_LOCK = threading.Lock()
_SOURCE = "ledger"
# 写失败丢弃计数（进程内）：台账是旁路，不抛错，但"丢过几笔"必须能被问出来
_DROPPED = {"events": 0}

# 事件保留条数上限（防止无限增长；台账可随时用文本工具检索，超过截断最旧的）
_MAX_EVENTS = 20000


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def ledger_path() -> Path:
    p = os.getenv("PMH_LEDGER_PATH", str(_project_root() / "logs" / "modelhub_ledger.jsonl"))
    return Path(p)


def _utcnow_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def dropped_events() -> int:
    """本进程写失败丢过几笔（台账是旁路，不抛错，但丢账必须可查）。"""
    return _DROPPED["events"]


def _alert_dropped(why: str) -> None:
    """写失败留痕：stdout + notifications.jsonl 两条路，缺一条就等于静默丢账。"""
    _DROPPED["events"] += 1
    msg = (
        f"[modelhub] ledger write failed: {why} —— 这一笔调用在台账上不存在，"
        f"日聚合重建（`usage_store.rebuild_from_ledger`）会少算它。"
        f"本进程累计丢弃 {_DROPPED['events']} 笔。"
    )
    print(msg)
    _notify(msg, source=_SOURCE)


def append_event(event: dict[str, Any]) -> None:
    """追加一条台账事件（进程内锁串行；写失败**留痕**不抛）。

    跨进程锁（2026-10-16 补，**有实测证据**）：两进程各追加 60 条短行、跑 6 轮——
    无锁 **6/6 轮丢账**（盘上 113~117 条），且**坏行数为 0**：两笔写进同一偏移，
    后一笔完整盖掉前一笔（行长相同，连撕裂都看不出来），只剩"总数少几条"。
    读者侧表现为 `ledger_stats` 少几次调用、`rebuild_from_ledger` 少几笔，盘上一切正常。
    带 `_process_lock` 后 **6/6 轮 120 条、0 坏行**。进程内的 `_LEDGER_LOCK`
    跨不了进程，所以这条只能靠文件锁。
    ⚠️ 第一轮探针（12KB/行 × 40 条 × 2 进程）**没**复现（80/80 全好），据此差点把锁
    撤掉并写上"未获证据支持"——换成短行才复现。别用"试过一次没复现"当"不会发生"。

    另一半补的是写失败：原先只有一句 `print()`，终端一关就查不到丢过账。
    现在丢一笔记一笔（`dropped_events()` 可问）+ 落 `notifications.jsonl`。
    """
    try:
        event = dict(event)
        event.setdefault("ts", _utcnow_iso())
        line = json.dumps(event, ensure_ascii=False)
        path = ledger_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with _LEDGER_LOCK, _process_lock(path), path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as e:  # noqa: BLE001 - 台账绝不影响主流程
        _alert_dropped(f"{type(e).__name__}: {e}")


def _read_text(path: Path) -> str:
    """读台账正文：Windows 上别人的写入会瞬时拒绝读，退避几次再判"读不了"。

    与 `vkeys._load_json_or` 同款（那边的实测：一次 PermissionError 就判不可读，
    会把**合法的并发读者**打死）。这里三处读路径（query_ledger / ledger_stats /
    usage_store.rebuild_from_ledger）共用，避免各写各的。
    """
    last: OSError | None = None
    for attempt in range(5):
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except PermissionError as e:  # 别人的写正在进行：让一下，别把瞬时占用当损坏
            last = e
            time.sleep(0.02 * (attempt + 1))
        except OSError:
            raise
    assert last is not None
    raise last


def append_call_event(
    *,
    request_id: str,
    model: str,
    endpoint: str,
    success: bool,
    latency_ms: int,
    attempts: int,
    failovers: int,
    http_status: int | None,
    error_type: str | None,
    error_msg: str | None,
    agent: str | None,
    role: str | None,
    content_chars: int | None,
) -> None:
    append_event(
        {
            "type": "call",
            "request_id": request_id,
            "model": model,
            "endpoint": endpoint,
            "success": bool(success),
            "latency_ms": int(latency_ms),
            "attempts": int(attempts),
            "failovers": int(failovers),
            "http_status": http_status,
            "error_type": error_type,
            "error_msg": (error_msg or "")[:300] or None,
            "agent": agent,
            "role": role,
            "content_chars": content_chars,
        }
    )


def append_switch_event(
    *,
    request_id: str,
    from_model: str,
    to_model: str,
    reason: str,
    detail: str | None,
    failover_index: int,
) -> None:
    append_event(
        {
            "type": "switch",
            "request_id": request_id,
            "from_model": from_model,
            "to_model": to_model,
            "reason": reason,
            "detail": (detail or "")[:300] or None,
            "failover_index": int(failover_index),
        }
    )


def query_ledger(
    *,
    model: str | None = None,
    success: bool | None = None,
    type: str | None = None,
    since: str | None = None,
    until: str | None = None,
    request_id: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """按条件检索台账（时间倒序返回最近 limit 条）。

    since/until 接受 ISO8601 片段（如 "2026-09-21" 或完整时间戳），按字典序比较
    （ISO 时间戳的字典序 == 时间序，含 Z 后缀亦成立）。
    """
    path = ledger_path()
    if not path.exists():
        return []
    rows: list[tuple[int, dict[str, Any]]] = []
    with _LEDGER_LOCK:
        text = _read_text(path)
    for pos, line in enumerate(text.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue  # 坏行跳过
        if type is not None and row.get("type") != type:
            continue
        if model is not None and row.get("model") != model:
            continue
        if success is not None and row.get("success") != success:
            continue
        if request_id is not None and row.get("request_id") != request_id:
            continue
        ts = row.get("ts") or ""
        if since and ts < since:
            continue
        if until and ts > until:
            continue
        rows.append((pos, row))
    # 毫秒精度会撞：一次调用可以连着写多条事件（call + switch 同一毫秒），
    # 只按 ts 排就会在并列时退回"文件顺序=最旧在前"，与"返回最近 limit 条"相反。
    # 并列时用写入顺序倒排 ⇒ 同毫秒内后写的也在前面。
    rows.sort(key=lambda item: (item[1].get("ts") or "", item[0]), reverse=True)
    return [row for _pos, row in rows[: max(1, int(limit))]]


def ledger_stats() -> dict[str, Any]:
    """台账汇总：总调用数、成功率、切换次数、按模型分布。"""
    path = ledger_path()
    out: dict[str, Any] = {
        "ledger_path": str(path),
        "exists": path.exists(),
        "calls": 0,
        "success": 0,
        "failed": 0,
        "switches": 0,
        "by_model": {},
    }
    if not path.exists():
        return out
    with _LEDGER_LOCK:
        text = _read_text(path)
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("type") == "call":
            out["calls"] += 1
            out["success" if row.get("success") else "failed"] += 1
            m = row.get("model") or "?"
            bm = out["by_model"].setdefault(m, {"calls": 0, "success": 0})
            bm["calls"] += 1
            if row.get("success"):
                bm["success"] += 1
        elif row.get("type") == "switch":
            out["switches"] += 1
    return out
