"""丢失更新（lost update）的回归：写路径必须是「事务内读改写 + 只写那一条」。

实测缺陷（2026-10-31 只读审计）：旧写路径是
`with _LOCK: data=_load(); 改; _save(data)`，而 `_save` 是 `DELETE FROM orders` + 全量
re-INSERT。`_LOCK` 只是**进程内**锁，于是两个实例（≈ 两个进程）各自 load→append→save
时，后写的那份把前一份整份覆盖 —— 实测并行新增 2 条只剩 1 条，两个请求都回 200。

这里不去起两个真进程（那要动真实数据目录，风险不值得），而是**直接钉住机制**：
1. `_upsert` 只碰自己那一行（旧版 `_save` 删全表，这一条必红）；
2. 写路径在 `BEGIN IMMEDIATE` 事务里（否则"读完还没写"的窗口不受保护）；
3. 老记录缺 `history` 字段时不许 500。
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app as lobster_app  # noqa: E402


@pytest.fixture()
def db(tmp_path, monkeypatch):
    """把真值源与 JSON 快照都指到 tmp（不碰 releases/lobster/data/）。"""
    path = tmp_path / "orders.db"
    monkeypatch.setattr(lobster_app, "DB", path)
    monkeypatch.setattr(lobster_app, "ORDERS", tmp_path / "orders.json")
    monkeypatch.delenv("LOBSTER_TOKEN", raising=False)
    lobster_app._init_db(lobster_app._db_connect())
    return path


def _rows(path: Path) -> dict[str, dict]:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        return {
            r["order_id"]: json.loads(r["data"])
            for r in conn.execute("SELECT order_id, data FROM orders")
        }
    finally:
        conn.close()


def test_upsert_only_touches_its_own_row(db: Path) -> None:
    """核心回归：写一条不许把别的行带走（旧版 `DELETE FROM orders` + 全量 re-INSERT）。"""
    with lobster_app._txn() as conn:
        lobster_app._upsert(conn, {"order_id": "A", "status": "pending"})
        lobster_app._upsert(conn, {"order_id": "B", "status": "pending"})
    with lobster_app._txn() as conn:
        lobster_app._upsert(conn, {"order_id": "A", "status": "confirmed"})

    rows = _rows(db)
    assert set(rows) == {"A", "B"}, f"另一条被写没了：{sorted(rows)}"
    assert rows["A"]["status"] == "confirmed" and rows["B"]["status"] == "pending"


def test_write_path_uses_begin_immediate(db: Path) -> None:
    """事务必须是 IMMEDIATE：deferred 只在写时取锁，"读完还没写"的窗口不受保护。"""
    with lobster_app._txn() as conn:
        # 事务内**读**也看得见已提交的数据，且此刻已经拿着写锁
        assert conn.in_transaction, "读改写没有跑在事务里"
        lobster_app._upsert(conn, {"order_id": "C", "status": "pending"})
    assert "C" in _rows(db)

    # 另一个写者此刻拿不到写锁（busy_timeout 之内会等，超时则报 locked）——
    # 这条是"并发写不会互相覆盖"的机制证明：谁先拿到 IMMEDIATE 谁先改。
    other = sqlite3.connect(str(db), timeout=0.05)
    try:
        with lobster_app._txn():
            with pytest.raises(sqlite3.OperationalError):
                other.execute("BEGIN IMMEDIATE")
    finally:
        other.close()


def test_transition_survives_a_record_without_history(db: Path) -> None:
    """老记录没有 history 字段时不许 500（审计实测过的 KeyError；`_get_one` + setdefault）。"""
    from fastapi.testclient import TestClient

    with lobster_app._txn() as conn:
        lobster_app._upsert(conn, {"order_id": "OLD-1", "status": "pending"})

    client = TestClient(lobster_app.app)
    r = client.patch("/api/orders/OLD-1", json={"status": "confirmed"})
    assert r.status_code == 200, r.text
    assert _rows(db)["OLD-1"]["status"] == "confirmed"
    assert _rows(db)["OLD-1"]["history"], "状态流转应当补出一条 history"


def test_missing_order_still_404(db: Path) -> None:
    from fastapi.testclient import TestClient

    client = TestClient(lobster_app.app)
    assert client.patch("/api/orders/NOPE", json={"status": "confirmed"}).status_code == 404
