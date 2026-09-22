"""覆盖率补齐第二波：triage_app 未覆盖分支（Missing 集中区）。"""
import importlib.util
import sys
from pathlib import Path

_app_dir = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_app_dir))
_spec = importlib.util.spec_from_file_location("triage_app", _app_dir / "app.py")
triage = importlib.util.module_from_spec(_spec)
sys.modules["triage_app"] = triage
_spec.loader.exec_module(triage)


# ---------------------------------------------------------------------------
# triage_app：save_sessions/load_sessions 坏文件兜底、健康度、B904 路径
# ---------------------------------------------------------------------------
def test_load_sessions_corrupt_returns_empty(tmp_path, monkeypatch):
    """sessions.json 损坏 → 返回空 dict（D-013 教训：坏文件不崩）。"""
    p = tmp_path / "sessions.json"
    p.write_text("{corrupted", encoding="utf-8")
    monkeypatch.setattr(triage, "SESSIONS", p)
    assert triage.load_sessions() == {}


def test_save_sessions_roundtrip(tmp_path, monkeypatch):
    p = tmp_path / "sessions.json"
    monkeypatch.setattr(triage, "SESSIONS", p)
    triage.save_sessions({"SES-X": {"session_id": "SES-X"}})
    assert triage.load_sessions()["SES-X"]["session_id"] == "SES-X"



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
