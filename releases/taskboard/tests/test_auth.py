"""TASKBOARD_TOKEN 的鉴权回归 —— W7 豁免 2026-11-20 收回后的口径。

收回做了两件事：
1. **缺配不再全开**：未配令牌时只有本机同源调用放行，跨机 / 跨站一律 401；
   要无条件全开须显式 `TASKBOARD_ALLOW_OPEN=1`。
2. **收掉「按前缀排除」的形状**（W7′）：旧实现是"GET 且路径不以 /api/tasks、
   /api/board 开头就放行"，于是 `GET /api/roster` 这类新加的只读 API 默认敞开。
   现在改成白名单：OPEN_PATHS 之外一律要令牌，以后加读接口不会顺手漏鉴权。
   原来那条 `test_read_apis_outside_the_prefix_allowlist_are_open_today` 就此转红 ——
   它是被设计成"改暴露面时提醒你"的，这里按设计转成收紧后的新断言。

自包含设计（与 test_storage.py 同款）：importlib 按路径加载 app.py，
避免与 lobster 的 conftest 在单进程合跑时撞 import mismatch（D-011）。
本目录的 conftest.py 会给业务用例设 ALLOW_OPEN，但按文件名跳过本文件。
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
    monkeypatch.delenv("TASKBOARD_ALLOW_OPEN", raising=False)
    # 缺省来源是 `testclient`（非回环）⇒ 会被 fail-closed 拦掉；这里钉成"本机调用"，
    # 缺省态的语义才与被测行为对齐。跨机场景由 _as() 显式指定。
    return TestClient(tb.app, client=("127.0.0.1", 50000))


def _as(host: str) -> TestClient:
    """指定来源地址的客户端：模拟本机 / 局域网另一台机器。"""
    return TestClient(tb.app, client=(host, 50000))


# ---------------------------------------------------------------------------
# 一、未配令牌 + 本机同源 —— 放行（本地体验不变）
# ---------------------------------------------------------------------------
def test_no_token_local_caller_passes(client):
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/tasks").status_code == 200
    assert client.post("/api/tasks", json=TASK).status_code == 200


# ---------------------------------------------------------------------------
# 二、未配令牌 + 跨机 / 跨站 —— 401
# ---------------------------------------------------------------------------
def test_no_token_remote_caller_rejected(client):
    c = _as("192.168.1.9")
    assert c.post("/api/tasks", json=TASK).status_code == 401
    assert c.get("/api/tasks").status_code == 401
    assert c.get("/api/health").status_code == 200


def test_no_token_cross_site_origin_rejected(client):
    h = {"origin": "http://evil.example", "host": "127.0.0.1:8792"}
    assert _as("127.0.0.1").post("/api/tasks", json=TASK, headers=h).status_code == 401


def test_allow_open_env_restores_legacy_open(client, monkeypatch):
    monkeypatch.setenv("TASKBOARD_ALLOW_OPEN", "1")
    assert _as("192.168.1.9").post("/api/tasks", json=TASK).status_code == 200


# ---------------------------------------------------------------------------
# 三、配了令牌 —— 白名单之外一律要 X-API-Key
# ---------------------------------------------------------------------------
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


def test_new_read_apis_require_token_after_whitelist(client, monkeypatch):
    """W7′ 收口：`GET /api/roster` 这类白名单外的读接口，配了令牌就得带 Key。

    旧版按前缀排除，roster 是敞开的；改成白名单后它必须 401 —— 这正是
    「以后加一个读接口就顺手漏鉴权」那个形状被堵住的证据。
    """
    monkeypatch.setenv("TASKBOARD_TOKEN", "secret-1")
    assert client.get("/api/roster").status_code == 401
    assert client.get("/api/roster", headers={"X-API-Key": "secret-1"}).status_code == 200


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
    """空白令牌 = 未设：既不是凭据，也不改变本机放行。"""
    monkeypatch.setenv("TASKBOARD_TOKEN", "   ")
    assert client.get("/api/tasks").status_code == 200
    assert _as("192.168.1.9").get("/api/tasks").status_code == 401
