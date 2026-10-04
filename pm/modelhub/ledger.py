"""调用与切换台账：每次真实模型调用、每次切换事件都追加写入 JSONL。

设计要点：
- 文件锁 + 追加写：并发安全；每行一个 JSON 对象（JSONL），坏行跳过不炸查询；
- entry.type = "call"（一次真实调用，含成败/耗时/切换后模型）或 "switch"（切换事件）；
- query_ledger() 支持 model / success / since / until / type 条件检索；
- 台账是"旁路"：写失败只告警，绝不影响调用主流程。
"""

from __future__ import annotations

import json
import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_LEDGER_LOCK = threading.Lock()

# 事件保留条数上限（防止无限增长；台账可随时用文本工具检索，超过截断最旧的）
_MAX_EVENTS = 20000


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def ledger_path() -> Path:
    p = os.getenv("PMH_LEDGER_PATH", str(_project_root() / "logs" / "modelhub_ledger.jsonl"))
    return Path(p)


def _utcnow_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def append_event(event: dict[str, Any]) -> None:
    """追加一条台账事件（线程安全；写失败只告警不抛）。"""
    try:
        event = dict(event)
        event.setdefault("ts", _utcnow_iso())
        line = json.dumps(event, ensure_ascii=False)
        path = ledger_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with _LEDGER_LOCK, path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as e:  # noqa: BLE001 - 台账绝不影响主流程
        print(f"[modelhub] ledger write failed: {type(e).__name__}: {e}")


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
        text = path.read_text(encoding="utf-8", errors="replace")
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
        text = path.read_text(encoding="utf-8", errors="replace")
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
