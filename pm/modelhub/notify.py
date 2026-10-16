"""G-06 告警落盘：把"必须留痕但不必中断主流程"的事件追加进 logs/notifications.jsonl。

为什么单独成模块：留痕这件事在仓里已经出现两处独立实现的需求——

1. `usage_store` 日聚合损坏（2026-10-15 累计 554 条 ALERT）；
2. `ledger` 台账写失败（2026-10-16 补）：**台账是事实源**，`usage_store.rebuild_from_ledger`
   正是拿它重建日聚合的——它自己写失败却只 `print()` 一句，等于"事实源的洞没有洞"。

两处各写一份就会漂（本仓付过学费：同族实现抄第三遍必然分叉），所以放在一处。

设计口径（别改）：
- **只留痕，不抛**。告警路径绝不能反过来把主流程打断——台账写失败、聚合损坏都是旁路。
- **落盘优先于 stdout**：终端关掉就没了，`notifications.jsonl` 是唯一能活过一轮跑批的地方。
- 留痕本身失败就静默：告警系统自己炸了不能连累业务。
"""

from __future__ import annotations

import json
import time
from pathlib import Path


def notifications_path() -> Path:
    return Path(__file__).resolve().parents[2] / "logs" / "notifications.jsonl"


def _notify(msg: str, *, source: str) -> None:
    """追加一条告警（时间戳 + 来源 + 消息）。"""
    try:
        nlog = notifications_path()
        nlog.parent.mkdir(parents=True, exist_ok=True)
        with open(nlog, "a", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {
                        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                        "source": source,
                        "message": msg,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    except Exception:  # noqa: BLE001 - 留痕失败静默：告警系统不许反过来弄死业务
        pass
