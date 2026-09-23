"""覆盖率补齐第二波：triage_app 未覆盖分支（Missing 集中区）。"""
import importlib.util
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_app_dir = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_app_dir))
_spec = importlib.util.spec_from_file_location("triage_app", _app_dir / "app.py")
triage = importlib.util.module_from_spec(_spec)
sys.modules["triage_app"] = triage
_spec.loader.exec_module(triage)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """本地密封夹具（本文件唯一的 API 级用例使用）：隔离存储路径 + 静默评委。"""
    monkeypatch.setattr(triage, "DATA", tmp_path)
    monkeypatch.setattr(triage, "LEDGER", tmp_path / "ledger.jsonl")
    monkeypatch.setattr(triage, "SESSIONS", tmp_path / "sessions.json")  # 不存在 → 不导入真实快照
    monkeypatch.setattr(triage, "DB", tmp_path / "sessions.db")
    monkeypatch.setattr(triage, "HEALTH_FILE", tmp_path / "health.json")
    monkeypatch.setattr(
        triage, "call_llm",
        lambda *a, **k: {"ok": True, "latency_ms": 1, "content": "{}", "usage": {}})
    monkeypatch.delenv("TRIAGE_TOKEN", raising=False)
    return TestClient(triage.app)


# ---------------------------------------------------------------------------
# triage_app：sessions 存储层（M3.4c SQLite 化）坏库兜底、roundtrip、导入语义
# ---------------------------------------------------------------------------
def test_load_sessions_corrupt_returns_empty(tmp_path, monkeypatch):
    """DB 文件损坏 → 返回空 dict（D-013 教训：坏存储不崩，等价旧版坏 JSON 兜底）。"""
    p = tmp_path / "sessions.db"
    p.write_text("{corrupted not a db", encoding="utf-8")
    monkeypatch.setattr(triage, "DB", p)
    monkeypatch.setattr(triage, "SESSIONS", tmp_path / "sessions.json")  # 不存在
    assert triage.load_sessions() == {}


def test_save_sessions_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(triage, "DB", tmp_path / "sessions.db")
    monkeypatch.setattr(triage, "SESSIONS", tmp_path / "sessions.json")  # 不存在
    triage.save_sessions({"SES-X": {"session_id": "SES-X"}})
    assert triage.load_sessions()["SES-X"]["session_id"] == "SES-X"


def test_sessions_import_once_db_is_truth(tmp_path, monkeypatch):
    """JSON 快照一次性导入：导入后 DB 是真值源，删快照/新增互不影响。"""
    import json as _json
    (tmp_path / "sessions.json").write_text(
        _json.dumps({"SES-OLD": {"session_id": "SES-OLD", "status": "done"}}), encoding="utf-8")
    monkeypatch.setattr(triage, "DB", tmp_path / "sessions.db")
    monkeypatch.setattr(triage, "SESSIONS", tmp_path / "sessions.json")
    assert triage.load_sessions()["SES-OLD"]["session_id"] == "SES-OLD"
    # DB 上新增（单行 upsert 不动 SES-OLD）
    triage.save_session({"session_id": "SES-NEW", "status": "running"})
    (tmp_path / "sessions.json").unlink()  # 快照删了也不影响
    fresh = triage.load_sessions()
    assert set(fresh) == {"SES-OLD", "SES-NEW"}


def test_give_feedback_single_row_write(client):
    """反馈单行读改写：目标会话更新、其他会话数据不受影响（写放大清零的行为面验证）。"""
    r1 = client.post("/api/reviews", json={"subject": "单A", "body": "b"})
    r2 = client.post("/api/reviews", json={"subject": "单B", "body": "b"})
    sid_a, sid_b = r1.json()["session_id"], r2.json()["session_id"]
    import time as _t
    for _ in range(120):
        if client.get(f"/api/sessions/{sid_b}").json().get("status") == "done":
            break
        _t.sleep(0.1)
    fb = client.post(f"/api/sessions/{sid_a}/feedback", json={"rating": 4})
    assert fb.status_code == 200 and fb.json()["ok"] is True
    assert client.get(f"/api/sessions/{sid_a}").json()["feedback"]["rating"] == 4
    assert client.get(f"/api/sessions/{sid_b}").json()["feedback"] is None



def test_build_consensus_all_failed():
    """全部评委失败 → 共识 None + 需人工介入提示。"""
    verdicts = [{"seat": "评委A", "model": "m", "ok": False, "error": "boom"}]
    c = triage.build_consensus(verdicts)
    assert c["category"] is None and "人工介入" in c["note"]


def test_consensus_dissent_and_failed_lists():
    """异议与失格评委列表正确生成。"""
    verdicts = [
        {"seat": "评委A", "model": "m1", "verdict": {"category": "billing", "severity": "P1", "action": "退款", "confidence": 0.9}},
        {"seat": "评委B", "model": "m2", "verdict": {"category": "complaint", "severity": "P2", "action": "安抚", "confidence": 0.7}},
        {"seat": "评委C", "model": "m3", "ok": False, "error": "TimeoutError"},
    ]
    c = triage.build_consensus(verdicts)
    assert c["category"] == "billing" and c["votes"] == "1/2"
    assert len(c["dissent"]) == 1 and c["dissent"][0]["seat"] == "评委B"
    assert c["failed"][0]["seat"] == "评委C"




def test_parse_verdict_edge_cases():
    """空串/纯围栏/嵌套 JSON 的解析边界。"""
    import pytest as _pytest
    with _pytest.raises(ValueError):
        triage.parse_verdict("")
    with _pytest.raises(ValueError):
        triage.parse_verdict("```json\n```")
    nested = '{"a":{"b":1}}'
    assert triage.parse_verdict("前缀 " + nested + " 后缀")["a"]["b"] == 1
