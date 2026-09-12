"""任务/赛马记录的存储抽象：内存实现（默认）与 SQLite 实现（多 worker 共享）。

为什么需要它：`TaskManager` 原本把任务表放在**进程内**字典里，于是服务只能单 worker ——
`--workers > 1` 时任务只存在于接到 POST 的那个进程，其他 worker 查 `run_id` 一律 404
（README 的「已知限制」里记着这条）。把记录层抽出来后，多进程共享同一个 SQLite 文件即可，
调度器自己不再关心记录存在哪里。

两个实现的取舍：
- `MemoryStore`：默认。对象直接存引用，无序列化开销，行为与旧版完全一致（单 worker）。
- `SqliteStore`：`PM_TASK_DB=<path>` 时启用。记录经 JSON 往返，因此**值必须是可 JSON 化的
  （不可序列化的对象会被 `default=str` 降级成字符串）**；`report_path` 存字符串、读回时还原成
  `Path`。WAL 模式 + 一把进程内锁，多进程读写同一个库。

注意这里只管**记录**，不管执行：线程池、Future、任务取消这些仍然属于进程内（谁提交谁执行）。
横向扩容的前提是负载均衡器把 `/api/status` 打到任意 worker 都能拿到同一份记录。
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

logger = logging.getLogger("pm.store")


@dataclass
class TaskRecord:
    run_id: str
    status: str = "pending"  # pending | running | finished | failed
    result: dict[str, Any] | None = None
    progress: dict[str, Any] | None = None  # 运行中的实时快照（H2）
    error: str | None = None
    report_path: Path | None = None


@dataclass
class RaceRecord:
    race_id: str
    name: str
    run_ids: list[str]
    spec_count: int


# 记录字段清单：序列化与「只更新指定字段」共用一份定义，避免两处漂移
_TASK_FIELDS = ("run_id", "status", "result", "progress", "error", "report_path")
_RACE_FIELDS = ("race_id", "name", "run_ids", "spec_count")


def _dump(rec: TaskRecord | RaceRecord) -> str:
    data: dict[str, Any] = {}
    for k in _TASK_FIELDS if isinstance(rec, TaskRecord) else _RACE_FIELDS:
        v = getattr(rec, k)
        if k == "report_path" and v is not None:
            v = str(v)
        data[k] = v
    # default=str：图状态里有 Path / datetime 这类不可 JSON 化的值，
    # 与 _save_artifacts 的落盘口径保持一致（宁可降级成字符串，不让记账炸主流程）
    return json.dumps(data, ensure_ascii=False, default=str)


def _load_task(raw: str) -> TaskRecord:
    data = json.loads(raw)
    path = data.get("report_path")
    return TaskRecord(
        run_id=str(data.get("run_id") or ""),
        status=str(data.get("status") or "pending"),
        result=data.get("result"),
        progress=data.get("progress"),
        error=data.get("error"),
        report_path=Path(path) if path else None,
    )


def _load_race(raw: str) -> RaceRecord:
    data = json.loads(raw)
    return RaceRecord(
        race_id=str(data.get("race_id") or ""),
        name=str(data.get("name") or ""),
        run_ids=[str(r) for r in (data.get("run_ids") or [])],
        spec_count=int(data.get("spec_count") or 0),
    )


class RecordStore(Protocol):
    """记录存储协议。调度器只依赖这些方法。"""

    def put_task(self, rec: TaskRecord) -> None: ...

    def update_task(self, run_id: str, **fields: Any) -> None: ...

    def get_task(self, run_id: str) -> TaskRecord | None: ...

    def list_task_ids(self) -> list[str]: ...

    def count_tasks(self) -> int: ...

    def put_race(self, rec: RaceRecord) -> None: ...

    def get_race(self, race_id: str) -> RaceRecord | None: ...

    def count_races(self) -> int: ...

    def evict(self, limit: int) -> None: ...

    def close(self) -> None: ...


class MemoryStore:
    """进程内实现（默认）。对象按引用存放，行为与旧版字典完全一致。"""

    def __init__(self) -> None:
        self._tasks: dict[str, TaskRecord] = {}
        self._races: dict[str, RaceRecord] = {}
        self._lock = threading.Lock()

    def put_task(self, rec: TaskRecord) -> None:
        with self._lock:
            self._tasks[rec.run_id] = rec

    def update_task(self, run_id: str, **fields: Any) -> None:
        with self._lock:
            rec = self._tasks.get(run_id)
            if rec is None:
                return
            for k, v in fields.items():
                if k in _TASK_FIELDS:
                    setattr(rec, k, v)

    def get_task(self, run_id: str) -> TaskRecord | None:
        with self._lock:
            return self._tasks.get(run_id)

    def list_task_ids(self) -> list[str]:
        with self._lock:
            return list(self._tasks)

    def count_tasks(self) -> int:
        with self._lock:
            return len(self._tasks)

    def put_race(self, rec: RaceRecord) -> None:
        with self._lock:
            self._races[rec.race_id] = rec

    def get_race(self, race_id: str) -> RaceRecord | None:
        with self._lock:
            return self._races.get(race_id)

    def count_races(self) -> int:
        with self._lock:
            return len(self._races)

    def evict(self, limit: int) -> None:
        """按插入序淘汰最旧记录（两个表各自独立）。"""
        with self._lock:
            for table in (self._tasks, self._races):
                while len(table) > limit:
                    table.pop(next(iter(table)), None)

    def close(self) -> None:
        return None


class SqliteStore:
    """SQLite 实现：多进程共享一份记录，是 `--workers > 1` 的前提。

    - `seq` 自增主键当插入序（更新时不动 seq，否则淘汰顺序会被刷新打乱）；
    - 一把进程内锁 + `check_same_thread=False`：调度器在多个线程里读写；
    - WAL：读写并发不互斥，多进程读同一份记录不阻塞。
    """

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        parent = Path(self.path).parent
        if str(parent) not in ("", "."):
            parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS records (
                    seq     INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind    TEXT NOT NULL,
                    id      TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    UNIQUE(kind, id)
                )
                """
            )
            self._conn.commit()

    # ---- 内部 ----
    def _put(self, kind: str, rec_id: str, payload: str) -> None:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE records SET payload=? WHERE kind=? AND id=?", (payload, kind, rec_id)
            )
            if cur.rowcount == 0:
                self._conn.execute(
                    "INSERT INTO records (kind, id, payload) VALUES (?,?,?)",
                    (kind, rec_id, payload),
                )
            self._conn.commit()

    def _get(self, kind: str, rec_id: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload FROM records WHERE kind=? AND id=?", (kind, rec_id)
            ).fetchone()
        return row[0] if row else None

    def _count(self, kind: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM records WHERE kind=?", (kind,)
            ).fetchone()
        return int(row[0]) if row else 0

    # ---- 任务 ----
    def put_task(self, rec: TaskRecord) -> None:
        self._put("task", rec.run_id, _dump(rec))

    def update_task(self, run_id: str, **fields: Any) -> None:
        """只更新传入的字段（读-改-写）。进度刷新高频调用，避免重写整条大记录。"""
        if not fields:
            return
        raw = self._get("task", run_id)
        if raw is None:
            return
        data = json.loads(raw)
        for k, v in fields.items():
            if k in _TASK_FIELDS and k != "run_id":
                data[k] = str(v) if (k == "report_path" and v is not None) else v
        self._put("task", run_id, json.dumps(data, ensure_ascii=False, default=str))

    def get_task(self, run_id: str) -> TaskRecord | None:
        raw = self._get("task", run_id)
        return _load_task(raw) if raw else None

    def list_task_ids(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id FROM records WHERE kind='task' ORDER BY seq"
            ).fetchall()
        return [r[0] for r in rows]

    def count_tasks(self) -> int:
        return self._count("task")

    # ---- 赛马 ----
    def put_race(self, rec: RaceRecord) -> None:
        self._put("race", rec.race_id, _dump(rec))

    def get_race(self, race_id: str) -> RaceRecord | None:
        raw = self._get("race", race_id)
        return _load_race(raw) if raw else None

    def count_races(self) -> int:
        return self._count("race")

    # ---- 淘汰 / 关闭 ----
    def evict(self, limit: int) -> None:
        limit = max(1, int(limit))
        with self._lock:
            for kind in ("task", "race"):
                self._conn.execute(
                    "DELETE FROM records WHERE kind=? AND seq NOT IN "
                    "(SELECT seq FROM records WHERE kind=? ORDER BY seq DESC LIMIT ?)",
                    (kind, kind, limit),
                )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class RecordView(Mapping[str, TaskRecord | RaceRecord]):
    """把 store 包装成只读 Mapping，供 `tm._tasks[rid]` 这类旧访问方式继续可用。

    存在的理由：任务表内部结构变了（字典 → 存储层），但历史测试与调试习惯都在按
    Mapping 访问它。与其让调用方各处改，不如在边界上保留旧接口。
    """

    def __init__(self, store: RecordStore, kind: str) -> None:
        self._store = store
        self._kind = kind

    def __getitem__(self, key: str) -> Any:
        rec = self._store.get_task(key) if self._kind == "task" else self._store.get_race(key)
        if rec is None:
            raise KeyError(key)
        return rec

    def __iter__(self) -> Iterator[str]:
        return iter(self._store.list_task_ids()) if self._kind == "task" else iter(())

    def __len__(self) -> int:
        return self._store.count_tasks() if self._kind == "task" else self._store.count_races()


def store_from_env() -> RecordStore:
    """按 `PM_TASK_DB` 构造存储：设了就落 SQLite（可多 worker），否则进程内。"""
    path = (os.getenv("PM_TASK_DB") or "").strip()
    if not path:
        return MemoryStore()
    logger.info("任务记录存储：SQLite（%s）—— 支持多 worker 共享状态", path)
    return SqliteStore(path)


def make_view(store: RecordStore, kind: str) -> RecordView:
    return RecordView(store, kind)
