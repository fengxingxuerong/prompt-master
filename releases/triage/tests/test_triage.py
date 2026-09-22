"""多模型会审台测试集（G-04）。

零真实出网：mock 掉 app.call_llm，进程内 TestClient 覆盖：
- 共识投票（多数分类/组内最高严重度/异议留痕）
- JSON 围栏容错解析（P-03）
- 模型白名单快失败（P-05，mock get_hub_models）
- 健康度滑动窗口（P-02）
- API 边界（422/404/反馈闭环）
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as triage  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """隔离的数据目录 + 固定评委返回。"""
    monkeypatch.setattr(triage, "DATA", tmp_path)
    monkeypatch.setattr(triage, "LEDGER", tmp_path / "ledger.jsonl")
    monkeypatch.setattr(triage, "SESSIONS", tmp_path / "sessions.json")
    monkeypatch.setattr(triage, "HEALTH_FILE", tmp_path / "health.json")
    # 清空模型白名单缓存，避免测试环境连真网关
    monkeypatch.setattr(triage, "_model_cache", {"ts": 0.0, "names": set()})

    def fake_call_llm(model, system, user, max_tokens=700, timeout=60, hub=None):
        verdict = json.dumps({
            "category": "billing", "severity": "P1",
            "action": "48 小时内退款", "confidence": 0.9,
        }, ensure_ascii=False)
        if model == "fence-model":
            verdict = '```json\n' + verdict + '\n``` 附带解释文字'
        if model == "garbage-model":
            verdict = "抱歉，我无法输出结构化结果"
        return {"ok": True, "latency_ms": 12, "content": verdict, "usage": {"total_tokens": 50}}

    monkeypatch.setattr(triage, "call_llm", fake_call_llm)
    # 白名单包含全部评委 + 特殊测试模型
    monkeypatch.setattr(triage, "get_hub_models",
                        lambda ttl=60: {rv["model"] for rv in triage.REVIEWERS}
                        | {"fence-model", "garbage-model"})
    return TestClient(triage.app)


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    d = r.json()
    assert d["status"] == "ok" and d["reviewers"] == 6 and d["version"] == "1.1.1"


def test_review_consensus(client):
    r = client.post("/api/reviews", json={"subject": "重复扣款", "body": "扣了两次钱"})
    assert r.status_code == 200
    d = r.json()
    assert d["consensus"]["category"] == "billing"
    assert d["consensus"]["severity"] == "P1"
    assert d["status"] == "done"
    assert len(d["verdicts"]) >= 3, "majority-return 生效时至少 3 票即汇总（其余为 late 票可能未返回）"
    assert all(v.get("verdict") for v in d["verdicts"])


def test_parse_verdict_fence_tolerance():
    """P-03：围栏 + 前后缀文本都能解析。"""
    good = '{"category":"billing","severity":"P1","action":"x","confidence":0.9}'
    assert triage.parse_verdict("```json\n" + good + "\n```")["category"] == "billing"
    assert triage.parse_verdict("前置说明 " + good + " 后置说明")["severity"] == "P1"
    with pytest.raises(ValueError):
        triage.parse_verdict("完全没有 JSON")


def test_unknown_model_fast_fail(monkeypatch):
    """P-05：不在白名单的模型 <1s 返回明确错误，不发起真实调用。"""
    monkeypatch.setattr(triage, "get_hub_models", lambda ttl=60: {"known-model"})
    t0 = __import__("time").time()
    r = triage.call_llm("no-such-model", "sys", "user")
    assert r["ok"] is False
    assert "unknown model" in r["error"]
    assert __import__("time").time() - t0 < 1.0


def test_reviewer_health_streak(monkeypatch, tmp_path):
    """P-02：连续 2 次失败 → unhealthy；成功 1 次即恢复。"""
    monkeypatch.setattr(triage, "HEALTH_FILE", tmp_path / "health.json")
    triage.bump_health("评委X", False)
    triage.bump_health("评委X", False)
    h = triage.load_health()
    assert h["评委X"]["unhealthy"] is True
    triage.bump_health("评委X", True)
    assert triage.load_health()["评委X"]["unhealthy"] is False


def test_feedback_loop_and_404(client):
    r = client.post("/api/reviews", json={"subject": "退货", "body": "想退货"})
    sid = r.json()["session_id"]
    fb = client.post(f"/api/sessions/{sid}/feedback", json={"rating": 5, "comment": "很准"})
    assert fb.status_code == 200 and fb.json()["feedback"]["rating"] == 5
    assert client.post("/api/sessions/SES-NOPE/feedback", json={"rating": 3}).status_code == 404


def test_validation_errors(client):
    assert client.post("/api/reviews", json={"subject": "", "body": "x"}).status_code == 422
    assert client.post("/api/reviews", json={"subject": "s" * 121, "body": "x"}).status_code == 422
    assert client.post("/api/sessions/SES-X/feedback", json={"rating": 0}).status_code == 422
    assert client.post("/api/sessions/SES-X/feedback", json={"rating": 9}).status_code == 422


def test_ledger_traceability(client):
    """台账留痕：review/consensus/feedback 三类都有记录且字段齐。"""
    r = client.post("/api/reviews", json={"subject": "发票错误", "body": "抬头开错"})
    sid = r.json()["session_id"]
    client.post(f"/api/sessions/{sid}/feedback", json={"rating": 4})
    led = client.get("/api/ledger?limit=100").json()
    kinds = {e["kind"] for e in led["entries"]}
    assert {"review", "consensus", "feedback"} <= kinds
    sample = next(e for e in led["entries"] if e["kind"] == "review")
    for k in ("ts", "model", "seat", "latency_ms", "ok", "session_id"):
        assert k in sample
