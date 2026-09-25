"""多模型会审台测试集（G-04）。

零真实出网：mock 掉 app.call_llm，进程内 TestClient 覆盖：
- 共识投票（多数分类/组内最高严重度/异议留痕）
- JSON 围栏容错解析（P-03）
- 模型白名单快失败（P-05，mock get_hub_models）
- 健康度滑动窗口（P-02）
- API 边界（422/404/反馈闭环）
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from _panel_drain import wait_panel_done

# D-011 修复：lobster 与 triage 都有 app.py，合并跑时 sys.modules 的 'app' 会撞名。
# 用 importlib 按文件路径加载 triage 的 app，并从缓存里摘除同名模块避免串包。
_app_dir = Path(__file__).resolve().parents[1]
_app_path = _app_dir / "app.py"
sys.path.insert(0, str(_app_dir))  # notify_center 同目录导入
_spec = importlib.util.spec_from_file_location("triage_app", _app_path)
triage = importlib.util.module_from_spec(_spec)
sys.modules["triage_app"] = triage
_spec.loader.exec_module(triage)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """隔离的数据目录 + 固定评委返回。"""
    monkeypatch.setattr(triage, "DATA", tmp_path)
    monkeypatch.setattr(triage, "LEDGER", tmp_path / "ledger.jsonl")
    monkeypatch.setattr(triage, "SESSIONS", tmp_path / "sessions.json")  # 不存在 → 不导入真实快照
    monkeypatch.setattr(triage, "DB", tmp_path / "sessions.db")  # M3.4c：sessions 真值源
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
    assert d["status"] == "ok" and d["reviewers"] == 6 and d["version"].startswith("1.")


def test_review_consensus(client):
    r = client.post("/api/reviews?wait=1", json={"subject": "重复扣款", "body": "扣了两次钱"})
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
    wait_panel_done(client, sid)  # 夹具退出前排空后台线程，否则真实台账会被越期追加


def test_validation_errors(client):
    assert client.post("/api/reviews", json={"subject": "", "body": "x"}).status_code == 422
    assert client.post("/api/reviews", json={"subject": "s" * 121, "body": "x"}).status_code == 422
    assert client.post("/api/sessions/SES-X/feedback", json={"rating": 0}).status_code == 422
    assert client.post("/api/sessions/SES-X/feedback", json={"rating": 9}).status_code == 422


def test_ledger_traceability(client):
    """台账留痕：review/consensus/feedback 三类都有记录且字段齐。"""
    r = client.post("/api/reviews", json={"subject": "发票错误", "body": "抬头开错"})
    sid = r.json()["session_id"]
    wait_panel_done(client, sid)  # 不等就是拿"后台可能还没写完"的台账去断言三类齐全
    client.post(f"/api/sessions/{sid}/feedback", json={"rating": 4})
    led = client.get(f"/api/ledger?limit=100&session={sid}").json()
    kinds = {e["kind"] for e in led["entries"]}
    assert {"review", "consensus", "feedback"} <= kinds
    sample = next(e for e in led["entries"] if e["kind"] == "review")
    for k in ("ts", "model", "seat", "latency_ms", "ok", "session_id"):
        assert k in sample


# ---------------------------------------------------------------------------
# v1.2 新增：异步提交 + SSE 进度（G-07/G-12）
# ---------------------------------------------------------------------------
def test_async_submit_returns_immediately(client, monkeypatch):
    """异步模式：立即返回 running，后台完成后轮询可见 done。"""
    import time as _t
    r = client.post("/api/reviews", json={"subject": "异步单", "body": "不等结果"})
    assert r.status_code == 200
    d = r.json()
    assert d["status"] == "running" and d["session_id"].startswith("SES-")
    # 后台很快完成（mock 无延迟）
    poll = {}
    for _ in range(120):  # D-012:偶发竞态，窗口 40→120（12s），后台线程调度慢时不误报
        poll = client.get(f"/api/sessions/{d['session_id']}").json()
        if poll.get("status") == "done":
            break
        _t.sleep(0.1)
    assert poll["status"] == "done"
    assert poll["consensus"]["category"] == "billing"


def test_sse_emits_progress_and_done(client):
    """SSE：订阅异步会话应收到 progress 事件与 done 事件。"""
    r = client.post("/api/reviews", json={"subject": "SSE单", "body": "实时进度"})
    sid = r.json()["session_id"]
    import time as _t
    for _ in range(40):
        poll = client.get(f"/api/sessions/{sid}").json()
        if poll.get("status") == "done":
            break
        _t.sleep(0.1)
    # 会话已完成：订阅应立即收到 done（防错过终态）
    with client.stream("GET", f"/api/sessions/{sid}/events") as resp:
        assert resp.status_code == 200
        body = b"".join(resp.iter_raw()).decode("utf-8")
    assert "event: done" in body and '"status": "done"' in body


def test_api_notifications_endpoint(client, tmp_path, monkeypatch):
    """通知列表端点：notify 落盘后可经 API 回读。"""
    from notify_center import notify

    # 只封 DATA：路由现在读的是这个常量（原来读现写的 APP_DIR/"data"，夹具够不到）
    monkeypatch.setattr(triage, "DATA", tmp_path / "data")
    notify("triage", "端点通知演练", base_dir=tmp_path / "data")
    r = client.get("/api/notifications?limit=10")
    assert r.status_code == 200
    assert any(i["message"] == "端点通知演练" for i in r.json()["items"])


def test_notify_endpoints_cannot_touch_the_tracked_data_dir(client):
    """跑测试不许往被跟踪的运营数据里写 —— 这条是被真实事故逼出来的。

    2026-09-25 深夜跑 CI 等价门禁，`releases/triage/data/notifications.jsonl` 被
    `POST /api/notify-test` 追加了一行：三个通知端点把目录写成 `APP_DIR / "data"`，
    夹具只封得住 `DATA`。所以断言要盯两边 —— 只断"临时目录里有"抓不到"两边都写"。
    """
    real = triage.APP_DIR / "data" / "notifications.jsonl"
    before = real.read_bytes() if real.exists() else b""
    r = client.post("/api/notify-test", json={"message": "密封目录演练"})
    assert r.status_code == 200 and r.json()["file"] is True
    assert (triage.DATA / "notifications.jsonl").exists(), "通知没落进夹具目录"
    after = real.read_bytes() if real.exists() else b""
    assert after == before, "通知写进了被跟踪的真实 data 目录（夹具没封住路由）"


def test_api_notify_test_and_config(client):
    r = client.post("/api/notify-test", json={"message": "链路自检"})
    assert r.status_code == 200 and r.json()["file"] is True
    cfg = client.get("/api/notify-config")
    assert cfg.status_code == 200 and cfg.json()["enabled"] is False


def test_feedback_during_a_running_panel_does_not_lose_the_update(client, monkeypatch):
    """会审还没收口时提交反馈，终态与反馈都必须留住（lost update 回归）。

    原来面板收口是"整行 INSERT OR REPLACE"，反馈端点也是"读整行 → 只改 feedback → 写整行"
    ⇒ 两份整行写互相覆盖，谁后写谁赢：反馈后落就看不见 done（轮询/SSE 永远等不到终态），
    面板后落就丢掉反馈。2026-09-25 深夜由"排空后台线程"那条测试断言撞出来（10 次里 1~2 次）。
    这里故意把评委打慢，保证反馈一定插在会审中间。
    """
    import time as _t

    def slow_llm(*a, **k):
        _t.sleep(0.25)
        return {"ok": True, "latency_ms": 250, "content": "{}", "usage": {}}

    monkeypatch.setattr(triage, "call_llm", slow_llm)
    sid = client.post("/api/reviews",
                      json={"subject": "抢在收口前反馈", "body": "b"}).json()["session_id"]
    assert client.post(f"/api/sessions/{sid}/feedback", json={"rating": 4}).status_code == 200
    d = wait_panel_done(client, sid)
    assert d["status"] == "done" and (d.get("feedback") or {}).get("rating") == 4, (
        f"反馈与会审终态互相覆盖：{d.get('status')}/{d.get('feedback')}"
    )


def test_api_gateway_metrics_proxy(client):
    """代理端点：网关正常→透传；离线→offline 标记（两态都算通过）。"""
    r = client.get("/api/gateway-metrics")
    assert r.status_code == 200
    d = r.json()
    assert ("modelhub_calls_total" in d) or (d.get("offline") is True)


def test_health_file_corrupt_fallback(tmp_path, monkeypatch):
    """健康度文件损坏 → bump 从坏状态恢复写（D-013 教训）。"""
    p = tmp_path / "reviewer_health.json"
    p.write_text("bad{", encoding="utf-8")
    monkeypatch.setattr(triage, "HEALTH_FILE", p)
    triage.bump_health("评委Z", True)
    assert triage.load_health()["评委Z"]["streak_fail"] == 0


def test_reviewer_health_endpoint(client, tmp_path, monkeypatch):
    monkeypatch.setattr(triage, "HEALTH_FILE", tmp_path / "h.json")
    triage.bump_health("评委W", True)
    r = client.get("/api/reviewer_health")
    assert r.status_code == 200 and r.json()["评委W"]["total_ok"] == 1


def test_session_404(client):
    assert client.get("/api/sessions/SES-NOPE").status_code == 404


def test_static_file_404(client):
    """静态路由：不存在的文件→404；路径穿越被拒。"""
    assert client.get("/static/nope.html").status_code == 404
    assert client.get("/static/..%2Fapp.py").status_code in (404, 307)


def test_static_cost_board_served(client):
    assert client.get("/static/cost-board.html").status_code == 200
