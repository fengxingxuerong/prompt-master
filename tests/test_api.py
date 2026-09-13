"""REST API 层端到端测试（补齐覆盖率最低的一层）。

背景：server.py 有 7 个路由（health / optimize / status / report / race×3），
此前只有 test_web_console 覆盖控制台页与 test_ratelimit 顺带碰一个接口，
接口层的正常路径与异常分支（404 / 422 / 401）都没有钉住 —— 这是覆盖率最低、
同时又是外部唯一入口的一层。

执行方式：用 TestClient 打真实 ASGI 应用（不 mock 路由与中间件），
任务执行走 `PM_FAKE_BACKEND=progress` 演示后端（零真实 API 调用）。
真实端点上的接口链路另行由手工 e2e 覆盖（见测试报告）。
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient
from pm.server import app

_DONE = {"passed", "max_iterations", "failed", "early_stopped", "needs_clarification"}


@pytest.fixture()
def client(monkeypatch) -> TestClient:
    # 演示后端在任务线程内按 env 生效（scheduler._run_task 读 PM_FAKE_BACKEND）
    monkeypatch.setenv("PM_FAKE_BACKEND", "progress")
    monkeypatch.delenv("PM_API_TOKEN", raising=False)
    monkeypatch.setenv("PM_EVAL_CACHE", "0")
    monkeypatch.setenv("PM_TARGET_CACHE", "0")
    return TestClient(app)


def _wait_status(client: TestClient, run_id: str, timeout: float = 30.0) -> dict:
    deadline = time.time() + timeout
    last: dict = {}
    while time.time() < deadline:
        r = client.get(f"/api/status/{run_id}")
        assert r.status_code == 200, r.text
        last = r.json()
        if last.get("status") in _DONE:
            return last
        time.sleep(0.2)
    raise AssertionError(f"任务 {run_id} 在 {timeout}s 内未结束，最后状态：{last}")


# --------------------------------------------------------------------------
# 正常路径
# --------------------------------------------------------------------------
def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "service": "prompt-master"}


def test_optimize_lifecycle_status_and_report(client):
    """提交 → 轮询状态 → 取报告：外部调用方的主链路。"""
    r = client.post(
        "/api/optimize",
        json={"task": "让 AI 分析销售数据并给出趋势结论", "n_test_cases": 2, "max_iterations": 1},
    )
    assert r.status_code == 200, r.text
    run_id = r.json()["run_id"]
    assert len(run_id) == 12
    assert run_id in r.json()["message"]

    st = _wait_status(client, run_id)
    assert st["run_id"] == run_id
    assert st["status"] in _DONE
    # 富视图字段：控制台/前端依赖这些键，缺一个页面就空一块
    assert st["task"].startswith("让 AI")
    assert st["n_test_cases"] == 2
    assert isinstance(st["test_cases"], list)

    rep = client.get(f"/api/report/{run_id}")
    assert rep.status_code == 200, rep.text
    body = rep.json()
    assert body["run_id"] == run_id
    assert "# PromptMaster 交付报告" in body["report"]
    assert "## 评分总览" in body["report"]


def test_report_available_immediately_when_terminal(client):
    """终态一到，报告必须立即可读：否则控制台会出现"状态已完成、报告未生成"。

    （2026-09-13 真实端点上观察过一次 404，后经查证疑为测试客户端走代理所致；
     这里用 TestClient 直连做回归，确认服务端不存在"终态早于报告"的窗口。）
    """
    r = client.post(
        "/api/optimize",
        json={"task": "让 AI 抽取销售数据中的城市与金额", "n_test_cases": 2, "max_iterations": 1},
    )
    run_id = r.json()["run_id"]
    while True:
        st = client.get(f"/api/status/{run_id}").json()
        if st.get("status") in _DONE:
            break
        time.sleep(0.02)
    rep = client.get(f"/api/report/{run_id}")
    assert rep.status_code == 200, "终态后首次取报告不应 404"
    assert rep.json()["report"].strip()


def test_optimize_with_seed_cases_uses_user_case_count(client):
    """给种子用例时用例数以用户为准（断言按序号对齐，声明数不一致会搅乱达标口径）。"""
    r = client.post(
        "/api/optimize",
        json={
            "task": "从销售文本抽取城市与金额",
            "test_cases": [
                {"input": "华东,120", "expected": "华东"},
                {"input": "华南,缺", "expected": "数据缺失"},
            ],
            "assertion_mode": "contains",
            "max_iterations": 1,
        },
    )
    assert r.status_code == 200, r.text
    st = _wait_status(client, r.json()["run_id"])
    assert st["n_test_cases"] == 2
    assert st["test_cases"] == ["华东,120", "华南,缺"]


def test_race_lifecycle(client):
    """赛马：多任务并发 → 状态聚合 → 横向对比报告。"""
    r = client.post(
        "/api/race",
        json={
            "name": "A/B 对照",
            "tasks": [
                {"task": "分析销售数据趋势", "n_test_cases": 2, "max_iterations": 1},
                {"task": "抽取城市与金额字段", "n_test_cases": 2, "max_iterations": 1},
            ],
        },
    )
    assert r.status_code == 200, r.text
    race_id = r.json()["race_id"]

    deadline = time.time() + 40
    body: dict = {}
    while time.time() < deadline:
        rs = client.get(f"/api/race/{race_id}")
        assert rs.status_code == 200
        body = rs.json()
        if body["finished"] >= body["total"]:
            break
        time.sleep(0.2)
    assert body.get("total") == 2
    assert body.get("finished") == 2
    assert len(body.get("runs", {})) == 2

    rep = client.get(f"/api/race/{race_id}/report")
    assert rep.status_code == 200
    assert rep.json()["report"].strip()


# --------------------------------------------------------------------------
# 异常分支
# --------------------------------------------------------------------------
def test_status_unknown_run_id_404(client):
    r = client.get("/api/status/deadbeefdead")
    assert r.status_code == 404
    assert "不存在" in r.json()["detail"]


def test_report_unknown_run_id_404(client):
    r = client.get("/api/report/deadbeefdead")
    assert r.status_code == 404
    assert "不存在" in r.json()["detail"]


def test_race_unknown_id_404(client):
    assert client.get("/api/race/zzzzzzzz").status_code == 404
    assert client.get("/api/race/zzzzzzzz/report").status_code == 404


def test_optimize_invalid_payload_422(client):
    """task 过短（min_length=4）与用例数越界（le=8）都要被拒在门口。"""
    assert client.post("/api/optimize", json={"task": "短"}).status_code == 422
    assert (
        client.post(
            "/api/optimize", json={"task": "合法长度的任务", "n_test_cases": 99}
        ).status_code
        == 422
    )


def test_race_requires_two_tasks(client):
    r = client.post("/api/race", json={"tasks": [{"task": "只有一个参赛任务"}]})
    assert r.status_code == 422


def test_token_required_when_configured(client, monkeypatch):
    """设了 PM_API_TOKEN 就强制校验：服务默认能启动就能烧钱（H3）。"""
    monkeypatch.setenv("PM_API_TOKEN", "secret-token")
    no_key = client.post("/api/optimize", json={"task": "让 AI 分析销售数据"})
    assert no_key.status_code == 401
    ok = client.post(
        "/api/optimize",
        json={"task": "让 AI 分析销售数据", "n_test_cases": 1, "max_iterations": 1},
        headers={"X-API-Key": "secret-token"},
    )
    assert ok.status_code == 200
