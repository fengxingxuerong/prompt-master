"""通知中心模块（M2.1）——会审台/看门狗共用。

能力：
- notify(source, message, level="info")：落盘 notifications.jsonl（既有行为保持）+
  可选 webhook 分发（读 data/notify_config.json 的 webhook_url，5s 超时静默失败）
- 配置格式：{"webhook_url": "https://...", "enabled": true}；无配置/未启用=仅落盘

线程安全：文件追加与 webhook 均在调用线程内完成（webhook 失败绝不抛出）。
"""
from __future__ import annotations

import json
import threading
import time
import urllib.request
from pathlib import Path

_lock = threading.Lock()

DEFAULT_CONFIG = {"webhook_url": "", "enabled": False}


def _paths(base_dir: Path):
    base_dir.mkdir(parents=True, exist_ok=True)
    return base_dir / "notifications.jsonl", base_dir / "notify_config.json"


def load_config(base_dir: Path) -> dict:
    _, cfg = _paths(base_dir)
    if not cfg.exists():
        return dict(DEFAULT_CONFIG)
    try:
        c = json.loads(cfg.read_text(encoding="utf-8"))
        return {**DEFAULT_CONFIG, **(c if isinstance(c, dict) else {})}
    except Exception:  # noqa: BLE001 - 损坏配置按默认处理
        return dict(DEFAULT_CONFIG)


def save_config(base_dir: Path, cfg: dict) -> None:
    _, cfg_path = _paths(base_dir)
    with _lock:
        cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")


def notify(source: str, message: str, level: str = "info", base_dir: Path | None = None) -> dict:
    """发一条通知：总是落盘；配置了 webhook 且 enabled 时额外推送。

    返回 {"file": bool, "webhook": "sent"|"skipped"|"failed"}，方便端点回显。
    """
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "source": source,
           "level": level, "message": message}
    base = base_dir or Path(__file__).resolve().parent / "data"
    ledger, _cfg_path = _paths(base)

    file_ok = False
    with _lock:
        try:
            with open(ledger, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            file_ok = True
        except Exception:  # noqa: BLE001
            file_ok = False

    webhook_state = "skipped"
    cfg = load_config(base)
    url = (cfg.get("webhook_url") or "").strip()
    if cfg.get("enabled") and url:
        try:
            payload = json.dumps(rec, ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(url, data=payload, method="POST",
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=5)
            webhook_state = "sent"
        except Exception:  # noqa: BLE001 - webhook 失败静默（通知不阻断业务）
            webhook_state = "failed"
    return {"file": file_ok, "webhook": webhook_state}


def list_notifications(base_dir: Path, limit: int = 50) -> list:
    ledger, _ = _paths(base_dir)
    if not ledger.exists():
        return []
    out = []
    for line in ledger.read_text(encoding="utf-8").strip().splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except Exception:  # noqa: BLE001 - 跳过坏行
                pass
    return out[-limit:]
