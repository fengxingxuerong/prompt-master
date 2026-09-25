"""TASKBOARD_TOKEN 的鉴权回归（与 lobster/tests/test_auth.py 同一批补的证据，W7 豁免不变）。

顺带把 taskboard 中间件的**形状差异**钉成用例：lobster / triage 是"白名单精确路径豁免，
其余一律要令牌"，而这里是 `is_read = GET/HEAD 且路径不以 /api/tasks、/api/board 开头`
—— **按前缀排除**。于是 `GET /api/roster` 这类"新加的只读 API"默认是敞开的。
现在不算漏洞（roster 是花名册，且服务只在局域网内、token 缺省关闭），但它是那种
"以后加一个 `/api/xxx` 读接口就顺手漏掉鉴权"的形状，所以这里用断言把当前行为记下来：
哪天改成白名单式，`test_read_apis_outside_the_prefix_allowlist_are_open_today` 会红，
改动的人就会知道自己在改变暴露面，而不是悄悄改。

自包含设计（与 test_storage.py 同款，无 conftest）：importlib 按路径加载，
避免与 lobster 的 conftest 在单进程合跑时撞 import mismatch（D-011）。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_app_path = Path(__file__).resolve().parents[1] / "app.py"
_spec = importlib.util.spec_from_file_location("taskboard_auth_app", _app_path)
tb = importlib.util.module_from_spec(_spec)
sys.modules["taskboard_auth_app"] = tb
_spec.loader.exec_module(tb)

TASK = {"title": "鉴权用例任务", "assignee": "Agent-AutoClaw", "priority": "P2"}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(tb, "DB", tmp_path / "tasks.db")
    monkeypatch.setattr(tb, "TASKS", tmp_path / "tasks.json")  # 无快照 → 空库启动
    monkeypatch.delenv("TASKBOARD_TOKEN", raising=False)
    return TestClient(tb.app)


def test_no_token_open_access(client):
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/tasks").status_code == 200
    assert client.post("/api/tasks", json=TASK).status_code == 200


def test_token_set_health_pages_and_docs_exempt(client, monkeypatch):
    monkeypatch.setenv("TASKBOARD_TOKEN", "secret-1")
    for path in ("/api/health", "/", "/rules", "/exceptions", "/handoff"):
        assert client.get(path).status_code == 200, path


def test_token_set_writes_rejected_without_key(client, monkeypatch):
    monkeypatch.setenv("TASKBOARD_TOKEN", "secret-1")
    assert client.post("/api/tasks", json=TASK).status_code == 401
    assert client.patch("/api/tasks/TASK-1", json={"status": "done"}).status_code == 401
    assert client.post("/api/tasks/TASK-1/undo", json={}).status_code == 401


def test_token_set_task_reads_rejected_without_key(client, monkeypatch):
    """任务正文含客户与来源信息，读也要凭据（W8 同源教训）。"""
    monkeypatch.setenv("TASKBOARD_TOKEN", "secret-1")
    for path in ("/api/tasks", "/api/tasks/TASK-1", "/api/board"):
        assert client.get(path).status_code == 401, path


def test_read_apis_outside_the_prefix_allowlist_are_open_today(client, monkeypatch):
    """特征化断言：`GET /api/roster` 现在**不需要**令牌（中间件按前缀排除，不是白名单）。"""
    monkeypatch.setenv("TASKBOARD_TOKEN", "secret-1")
    assert client.get("/api/roster").status_code == 200


def test_token_set_valid_key_passes(client, monkeypatch):
    monkeypatch.setenv("TASKBOARD_TOKEN", "secret-1")
    h = {"X-API-Key": "secret-1"}
    assert client.get("/api/tasks", headers=h).status_code == 200
    assert client.post("/api/tasks", json=TASK, headers=h).status_code == 200


def test_token_whitespace_tolerant(client, monkeypatch):
    monkeypatch.setenv("TASKBOARD_TOKEN", "  secret-1  ")
    assert client.get("/api/tasks", headers={"X-API-Key": "secret-1"}).status_code == 200
    assert client.get("/api/tasks", headers={"X-API-Key": "  secret-1  "}).status_code == 200
    assert client.get("/api/tasks").status_code == 401


def test_empty_token_env_treated_as_unset(client, monkeypatch):
    monkeypatch.setenv("TASKBOARD_TOKEN", "   ")
    assert client.get("/api/tasks").status_code == 200
