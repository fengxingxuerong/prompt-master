"""会审台鉴权测试（M3.1 安全收口 2026-09-23）。

TRIAGE_TOKEN 环境变量门控，三态覆盖：
- 未设 token：行为与历史版本完全一致（本地便利优先，全开放）
- 设 token：/api/health 与页面（/、/static/*）GET 豁免，其余一律 401
- 设 token + 正确 X-API-Key：写操作与数据读恢复正常

零真实出网：call_llm 打桩为静默成功；POST /api/reviews 走异步模式，
后台会审线程写入的是 tmp_path 隔离目录，不触碰真实数据。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from _panel_drain import wait_panel_done

# 与 test_triage.py 同款：按文件路径加载，避开与 lobster app.py 的同名模块冲突（D-011）
_app_dir = Path(__file__).resolve().parents[1]
_app_path = _app_dir / "app.py"
sys.path.insert(0, str(_app_dir))  # notify_center 同目录导入
_spec = importlib.util.spec_from_file_location("triage_app", _app_path)
triage = importlib.util.module_from_spec(_spec)
sys.modules["triage_app"] = triage
_spec.loader.exec_module(triage)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """隔离数据目录 + 静默评委 + 强制还原 TRIAGE_TOKEN（防宿主环境泄漏进断言，W16 教训）。"""
    monkeypatch.setattr(triage, "DATA", tmp_path)
    monkeypatch.setattr(triage, "LEDGER", tmp_path / "ledger.jsonl")
    monkeypatch.setattr(triage, "SESSIONS", tmp_path / "sessions.json")  # 不存在 → 不导入真实快照
    monkeypatch.setattr(triage, "DB", tmp_path / "sessions.db")  # M3.4c：sessions 真值源
    monkeypatch.setattr(triage, "HEALTH_FILE", tmp_path / "health.json")
    # 成本看板那份是**产品侧**账本（只读，但读的是真实内容）：一起指到临时目录，
    # 免得"/api/usage-board 返回什么"取决于这台机器今天跑过多少任务。
    monkeypatch.setattr(triage, "USAGE_FILE", tmp_path / "usage_daily.json")
    monkeypatch.setattr(triage, "_model_cache", {"ts": 0.0, "names": set()})
    monkeypatch.setattr(
        triage,
        "call_llm",
        lambda *a, **k: {"ok": True, "latency_ms": 1, "content": "{}", "usage": {}},
    )
    # 白名单必须也钉住：test_triage 钉了、这里原来没钉 ⇒ 本机若正好跑着 ModelHub 网关
    # （8687 活着），后台会审线程会去连真网关取模型列表。表现为"面板什么时候收口"
    # 取决于宿主上有没有服务 —— 2026-09-25 深夜就是它让越期写几乎必然发生。
    monkeypatch.setattr(
        triage, "get_hub_models", lambda ttl=60: {rv["model"] for rv in triage.REVIEWERS}
    )
    monkeypatch.delenv("TRIAGE_TOKEN", raising=False)
    return TestClient(triage.app)


# ---------------------------------------------------------------------------
# 态一：未设 token —— 全开放，与历史行为零差异
# ---------------------------------------------------------------------------
def test_no_token_open_access(client):
    r = client.post("/api/reviews", json={"subject": "无令牌单", "body": "未设 token 应放行"})
    assert r.status_code == 200
    assert r.json()["status"] == "running"
    assert client.get("/api/sessions").status_code == 200
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/ledger").status_code == 200
    # 成本看板必须读夹具那份（本机真实账本今天有内容）：读到真值时 days 不会为空
    assert client.get("/api/usage-board").json()["days"] == []
    # 收口再退出：否则后台线程会在夹具还原后把记录写进被跟踪的 data/ledger.jsonl
    assert wait_panel_done(client, r.json()["session_id"])["status"] == "done"


# ---------------------------------------------------------------------------
# 态二：设 token —— health 与页面豁免，其余（写+数据读）全部 401
# ---------------------------------------------------------------------------
def test_token_set_health_and_pages_exempt(client, monkeypatch):
    monkeypatch.setenv("TRIAGE_TOKEN", "secret-1")
    assert client.get("/api/health").status_code == 200
    assert client.get("/").status_code == 200
    assert client.get("/static/cost-board.html").status_code == 200
    # 豁免路径上文件不存在仍应是 404（而非被 401 掩盖）
    assert client.get("/static/nope.html").status_code == 404


def test_token_set_writes_rejected_without_key(client, monkeypatch):
    monkeypatch.setenv("TRIAGE_TOKEN", "secret-1")
    assert client.post("/api/reviews", json={"subject": "s", "body": "b"}).status_code == 401
    assert client.post("/api/notify-test", json={"message": "x"}).status_code == 401
    assert (
        client.post(
            "/api/sessions/SES-X/feedback", json={"rating": 3}, headers={"X-API-Key": "wrong-key"}
        ).status_code
        == 401
    )


def test_token_set_data_reads_rejected_without_key(client, monkeypatch):
    """W8 教训：工单正文含客户内容，数据读也必须凭据化。"""
    monkeypatch.setenv("TRIAGE_TOKEN", "secret-1")
    for path in (
        "/api/sessions",
        "/api/ledger",
        "/api/notifications",
        "/api/sessions/SES-ANY",
        "/api/sessions/SES-ANY/events",
        "/api/usage-board",
        "/api/reviewer_health",
    ):
        assert client.get(path).status_code == 401, path


# ---------------------------------------------------------------------------
# 态三：设 token + 正确 X-API-Key —— 全链路恢复正常
# ---------------------------------------------------------------------------
def test_token_set_valid_key_passes(client, monkeypatch):
    monkeypatch.setenv("TRIAGE_TOKEN", "secret-1")
    h = {"X-API-Key": "secret-1"}
    r = client.post(
        "/api/reviews", json={"subject": "带令牌单", "body": "正确 key 应放行"}, headers=h
    )
    assert r.status_code == 200
    sid = r.json()["session_id"]
    assert client.get("/api/sessions", headers=h).status_code == 200
    assert client.get(f"/api/sessions/{sid}", headers=h).status_code == 200
    fb = client.post(f"/api/sessions/{sid}/feedback", json={"rating": 5}, headers=h)
    assert fb.status_code == 200 and fb.json()["ok"] is True
    assert wait_panel_done(client, sid, headers=h)["status"] == "done"


def test_token_whitespace_tolerant(client, monkeypatch):
    """两端去空白：env 带空格与 header 带空格都应与干净值匹配。"""
    monkeypatch.setenv("TRIAGE_TOKEN", "  secret-1  ")
    h = {"X-API-Key": "secret-1"}
    assert client.get("/api/sessions", headers=h).status_code == 200
    assert client.get("/api/sessions", headers={"X-API-Key": "  secret-1  "}).status_code == 200
    assert client.get("/api/sessions").status_code == 401


def test_empty_token_env_treated_as_unset(client, monkeypatch):
    """设了但为空白 = 未设（与 taskboard `or \"\"` 口径一致）。"""
    monkeypatch.setenv("TRIAGE_TOKEN", "   ")
    r = client.post("/api/reviews", json={"subject": "s", "body": "b"})
    assert r.status_code == 200
    assert wait_panel_done(client, r.json()["session_id"])["status"] == "done"
