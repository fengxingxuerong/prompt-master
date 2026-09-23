"""M3.4a taskboard SQLite 存储回归（2026-09-24）。

覆盖：JSON 快照一次性导入（seeded 幂等）/ 空库启动 / 损坏快照 500 口径 /
跨连接持久性（真值源是 DB 而非 JSON）/ create→patch→undo 全链路。
全部走 tmp 隔离，不触碰真实台账。

自包含设计（无 conftest.py）：importlib 按路径加载（D-011 教训），避免与
lobster 的 conftest.py 在单进程合跑时撞 pytest import mismatch。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_app_path = Path(__file__).resolve().parents[1] / "app.py"
_spec = importlib.util.spec_from_file_location("taskboard_app", _app_path)
tb = importlib.util.module_from_spec(_spec)
sys.modules["taskboard_app"] = tb
_spec.loader.exec_module(tb)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(tb, "DB", tmp_path / "tasks.db")
    monkeypatch.setattr(tb, "TASKS", tmp_path / "tasks.json")  # 默认无快照 → 空库启动
    monkeypatch.delenv("TASKBOARD_TOKEN", raising=False)
    return TestClient(tb.app)


def _snapshot_task(num: int) -> dict:
    return {
        "task_id": f"TASK-20260901-{num:03d}",
        "created_at": "2026-09-01 10:00:00",
        "created_by": "Agent-AutoClaw",
        "title": f"旧任务{num}",
        "desc": "",
        "assignee": "Agent-AutoClaw",
        "priority": "P2",
        "due": "2026-09-30",
        "status": "pending",
        "acceptance": "",
        "source": "",
        "history": [],
    }


def test_import_from_json_snapshot_sequential_ids(client, tmp_path):
    """首次启动导入 JSON 快照：旧任务可见，新建任务 seq 续号不断档。"""
    snap = {"version": 1, "seq": 2, "tasks": [_snapshot_task(1), _snapshot_task(2)]}
    (tmp_path / "tasks.json").write_text(json.dumps(snap, ensure_ascii=False), encoding="utf-8")
    r = client.get("/api/tasks")
    assert r.status_code == 200 and r.json()["count"] == 2
    assert client.get("/api/health").json()["storage"] == "sqlite(wal)"
    new = client.post("/api/tasks", json={"title": "新任务", "assignee": "Agent-AutoClaw"})
    assert new.status_code == 200
    assert new.json()["task_id"].endswith("-003"), "seq 应从快照的 2 续到 3"
    assert client.get("/api/health").json()["tasks"] == 3


def test_import_happens_once_db_is_truth(client, tmp_path):
    """seeded 旗标：导入后 DB 是唯一真值源——删掉 JSON 快照数据仍在，不会重复导入覆盖。"""
    (tmp_path / "tasks.json").write_text(
        json.dumps({"version": 1, "seq": 1, "tasks": [_snapshot_task(1)]}, ensure_ascii=False),
        encoding="utf-8")
    assert client.get("/api/tasks").json()["count"] == 1
    client.post("/api/tasks", json={"title": "DB 新增", "assignee": "Agent-AutoClaw"})
    (tmp_path / "tasks.json").unlink()  # 快照删了也不影响
    fresh = tb._load()  # 模拟新连接（重启后首次读）
    assert len(fresh["tasks"]) == 2
    assert fresh["seq"] == 2


def test_no_snapshot_empty_start(client):
    """无快照：空库启动，创建后计数正确。"""
    h = client.get("/api/health").json()
    assert h["tasks"] == 0 and h["storage"] == "sqlite(wal)"
    r = client.post("/api/tasks", json={"title": "首个任务", "assignee": "Agent-AutoClaw"})
    assert r.status_code == 200
    assert client.get("/api/tasks").json()["count"] == 1


def test_corrupt_snapshot_unseeded_returns_500(client, tmp_path):
    """损坏快照且未建库：沿用旧口径 500 + 指引备份（不静默丢数据）。"""
    (tmp_path / "tasks.json").write_text("bad{", encoding="utf-8")
    r = client.get("/api/tasks")
    assert r.status_code == 500
    assert "损坏" in r.json()["detail"]


def test_create_patch_undo_roundtrip(client):
    """业务闭环在 SQLite 上原样工作：创建→流转→撤销（history 留痕）。"""
    oid = client.post("/api/tasks",
                      json={"title": "闭环任务", "assignee": "Agent-AutoClaw"}).json()["task_id"]
    p = client.patch(f"/api/tasks/{oid}", json={"status": "in_progress", "note": "开工"})
    assert p.status_code == 200 and p.json()["status"] == "in_progress"
    u = client.post(f"/api/tasks/{oid}/undo")
    assert u.status_code == 200 and u.json()["status"] == "pending"
    detail = client.get(f"/api/tasks/{oid}").json()
    assert detail["history"][-1]["action"] == "撤销"
    assert client.get("/api/health").json()["tasks"] == 1
