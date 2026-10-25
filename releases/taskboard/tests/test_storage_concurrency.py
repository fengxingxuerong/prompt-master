"""丢失更新 / 重复撤销 / 截止日未校验的回归（2026-10-31 只读审计实测）。

三条都是"接口回 200 但台账不对"那一类，靠读代码看不出来：

1. **丢任务**：旧写路径 `with _LOCK: data=_load(); 改; _save(data)` 里的
   `_save` 是 `DELETE FROM tasks` + 全量 re-INSERT，而 `_LOCK` 只是**进程内**锁
   ⇒ 两个实例（≈ 两个进程）并行创建时后写的那份把前一份覆盖；`seq` 也在同一把
   进程内锁里自增，两进程会算出同一个 `TASK-…-001` 被 `INSERT OR REPLACE` 静默合并。
2. **连按两次撤销**：旧版只跳过 `action == "撤销"` 的记录，而目标那条仍 `before is not None`
   ⇒ 第二次又回滚同一步，却照样 `ok:true`、`restored_to` 相同。
3. **PATCH due 未校验**：创建端有 `YYYY-MM-DD` 正则、PATCH 端没有 ⇒ `{"due": "下周五"}`
   能写进台账，而看板用字符串比 `due < today` 判逾期 ⇒ 逾期恒 0。
"""

from __future__ import annotations

import importlib
import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app as board_app  # noqa: E402


@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = tmp_path / "tasks.db"
    monkeypatch.setattr(board_app, "DB", path)
    monkeypatch.setattr(board_app, "TASKS", tmp_path / "tasks.json")
    monkeypatch.delenv("TASKBOARD_TOKEN", raising=False)
    board_app._init_db(board_app._db_connect())
    return path


def _rows(path: Path) -> dict[str, dict]:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        return {r["task_id"]: json.loads(r["data"]) for r in conn.execute("SELECT task_id, data FROM tasks")}
    finally:
        conn.close()


def _create(client, title: str) -> str:
    r = client.post(
        "/api/tasks",
        json={
            "title": title,
            "desc": "",
            "assignee": "Agent-AutoClaw",
            "priority": "P2",
            "due": "2026-12-31",
            "acceptance": "",
            "created_by": "t",
            "source": "",
        },
    )
    assert r.status_code == 200, r.text
    return r.json()["task_id"]


def test_upsert_only_touches_its_own_row(db: Path) -> None:
    """核心回归：写一条不许把别的行带走（旧版 `DELETE FROM tasks` + 全量 re-INSERT）。"""
    with board_app._txn() as conn:
        board_app._upsert_task(conn, {"task_id": "A", "status": "pending"})
        board_app._upsert_task(conn, {"task_id": "B", "status": "pending"})
    with board_app._txn() as conn:
        board_app._upsert_task(conn, {"task_id": "A", "status": "done"})
    rows = _rows(db)
    assert set(rows) == {"A", "B"}, f"另一条被写没了：{sorted(rows)}"
    assert rows["A"]["status"] == "done" and rows["B"]["status"] == "pending"


def test_seq_is_allocated_inside_the_write_transaction(db: Path) -> None:
    """号在事务里自增：两进程不会算出同一个 TASK-…-001（旧版 seq 在进程内锁里 +1）。"""
    with board_app._txn() as conn:
        assert board_app._next_seq(conn) == 1
        board_app._upsert_task(conn, {"task_id": "T-001", "status": "pending"})
    with board_app._txn() as conn:
        assert board_app._next_seq(conn) == 2
        board_app._upsert_task(conn, {"task_id": "T-002", "status": "pending"})
    assert set(_rows(db)) == {"T-001", "T-002"}


def test_begin_immediate_blocks_a_second_writer(db: Path) -> None:
    """机制证明：IMMEDIATE 下另一个写者拿不到写锁（deferred 挡不住"读完还没写"）。"""
    other = sqlite3.connect(str(db), timeout=0.05)
    try:
        with board_app._txn() as conn:
            assert conn.in_transaction
            with pytest.raises(sqlite3.OperationalError):
                other.execute("BEGIN IMMEDIATE")
    finally:
        other.close()


def test_undo_twice_rolls_back_two_different_steps(db: Path) -> None:
    """连按两次撤销必须回滚**两步**；旧版第二次又回滚同一步还报成功。"""
    from fastapi.testclient import TestClient

    client = TestClient(board_app.app)
    oid = _create(client, "标题")
    assert client.patch(f"/api/tasks/{oid}", json={"status": "in_progress"}).status_code == 200
    assert client.patch(f"/api/tasks/{oid}", json={"priority": "P1"}).status_code == 200

    r1 = client.post(f"/api/tasks/{oid}/undo")
    r2 = client.post(f"/api/tasks/{oid}/undo")
    assert r1.status_code == r2.status_code == 200, (r1.text, r2.text)

    task = client.get(f"/api/tasks/{oid}").json()
    # 两次撤销必须回滚**两步**：只按 restored_to 比会误判——两次变更可能落在同一秒里，
    # 时间戳分辨率就是 1s。所以比"回滚的是哪一条变更"（detail 里带 action）。
    undos = [h for h in task["history"] if h.get("action") == "撤销"]
    assert len(undos) == 2, f"两次撤销应当留下两条记录：{undos}"
    # 撤销总是回滚**最近一次**变更：先撤优先级（后做的），再撤状态（先做的）
    assert "优先级→P1" in undos[0]["detail"], undos[0]["detail"]
    assert "状态→进行中" in undos[1]["detail"], f"第二次又回滚了同一步：{undos[1]['detail']}"
    assert task["priority"] == "P2", "第二步（优先级）应当被第二次撤销回滚"
    assert task["status"] == "pending", "第一步（状态）应当被第一次撤销回滚"


def test_undo_after_undo_reports_nothing_left(db: Path) -> None:
    """撤销到没有可回滚的变更时，必须明说 422，而不是再"成功"一次。"""
    from fastapi.testclient import TestClient

    client = TestClient(board_app.app)
    oid = _create(client, "只有创建")
    assert client.post(f"/api/tasks/{oid}/undo").status_code == 422


def test_patch_rejects_a_non_iso_due(db: Path) -> None:
    """PATCH 的截止日必须与创建端同一把尺（旧版直接落盘，逾期判定恒 0）。"""
    from fastapi.testclient import TestClient

    client = TestClient(board_app.app)
    oid = _create(client, "标题")
    r = client.patch(f"/api/tasks/{oid}", json={"due": "下周五"})
    assert r.status_code == 422, f"非 ISO 截止日被写进去了：{r.status_code} {r.text}"
    assert client.get(f"/api/tasks/{oid}").json()["due"] == "2026-12-31"

    ok = client.patch(f"/api/tasks/{oid}", json={"due": "2026-11-15"})
    assert ok.status_code == 200 and ok.json()["task_id"] == oid
