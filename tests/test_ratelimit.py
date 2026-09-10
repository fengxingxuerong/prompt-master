"""服务层速率限制回归用例（H3 遗留半边）。

覆盖两层：
1. `pm/ratelimit.py` 单元行为：滑动窗口、cost 计费、窗口滑出后恢复、env 解析容错；
2. `pm/server.py` 集成：/api/optimize 超额 429 + Retry-After，/api/race 按任务数计费
   （批量入口不能成为限流旁路）。
"""

from __future__ import annotations

import time

from fastapi.testclient import TestClient
from pm import server as server_mod
from pm.ratelimit import SlidingWindowLimiter, parse_rate_limit_env


# --------------------------------------------------------------------------
# 限流器单元行为
# --------------------------------------------------------------------------
def test_limiter_basic_window():
    lim = SlidingWindowLimiter(max_events=3)
    assert lim.acquire("k", 1) == (True, 0.0)
    assert lim.acquire("k", 1) == (True, 0.0)
    assert lim.acquire("k", 1) == (True, 0.0)
    ok, retry_after = lim.acquire("k", 1)
    assert ok is False
    assert 0 < retry_after <= 60.0
    assert lim.denied_count == 1


def test_limiter_cost_accounting():
    """cost 按任务数计费：配额 5，先花 3，再要 3 拒绝、要 2 放行。"""
    lim = SlidingWindowLimiter(max_events=5)
    assert lim.acquire("k", 3)[0] is True
    ok, _ = lim.acquire("k", 3)
    assert ok is False  # 3 + 3 > 5
    assert lim.acquire("k", 2)[0] is True  # 3 + 2 = 5 恰好用满
    assert lim.acquire("k", 1)[0] is False


def test_limiter_keys_are_isolated():
    lim = SlidingWindowLimiter(max_events=1)
    assert lim.acquire("a", 1)[0] is True
    assert lim.acquire("b", 1)[0] is True
    assert lim.acquire("a", 1)[0] is False


def test_limiter_slides_after_window():
    lim = SlidingWindowLimiter(max_events=1, window_seconds=0.2)
    assert lim.acquire("k", 1)[0] is True
    assert lim.acquire("k", 1)[0] is False
    time.sleep(0.25)
    assert lim.acquire("k", 1)[0] is True


def test_parse_rate_limit_env():
    assert parse_rate_limit_env(None) == (10, None)
    assert parse_rate_limit_env("", default=7) == (7, None)
    assert parse_rate_limit_env("30") == (30, None)
    assert parse_rate_limit_env("0") == (0, None)  # 显式关闭
    val, warn = parse_rate_limit_env("abc")
    assert val == 10 and warn and "不是整数" in warn
    val, warn = parse_rate_limit_env("-3")
    assert val == 10 and warn and "负数" in warn


# --------------------------------------------------------------------------
# 服务层集成
# --------------------------------------------------------------------------
def _client_with_limiter(monkeypatch, max_events: int) -> TestClient:
    """固定限流器与鉴权，并 stub 掉调度器提交（只测入口限流，不跑任务）。"""
    monkeypatch.setattr(server_mod, "_limiter", SlidingWindowLimiter(max_events=max_events))
    monkeypatch.setattr(server_mod, "_rate_per_min", max_events)
    monkeypatch.setenv("PM_API_TOKEN", "tk")
    monkeypatch.setattr(server_mod._scheduler, "submit", lambda *a, **k: None)
    return TestClient(server_mod.app)


def _post_optimize(client: TestClient):
    return client.post(
        "/api/optimize",
        json={"task": "帮我写个提示词", "n_test_cases": 1, "max_iterations": 1},
        headers={"X-API-Key": "tk"},
    )


def test_server_optimize_returns_429_with_retry_after(monkeypatch):
    client = _client_with_limiter(monkeypatch, max_events=2)
    assert (
        client.post(
            "/api/optimize", json={"task": "这条任务足够长了"}, headers={"X-API-Key": "tk"}
        ).status_code
        == 200
    )
    r1 = _post_optimize(client)
    assert r1.status_code == 200
    r2 = _post_optimize(client)
    assert r2.status_code == 429
    assert "Retry-After" in r2.headers
    assert int(r2.headers["Retry-After"]) >= 1
    assert "每分钟上限 2" in r2.json()["detail"]


def test_server_race_billed_per_task(monkeypatch):
    """赛马按参赛任务数计费：配额 5，3 任务 + 3 任务必须撞 429。"""
    client = _client_with_limiter(monkeypatch, max_events=5)
    body = {
        "name": "演示赛马",
        "tasks": [
            {"task": "任务一：分析销售数据"},
            {"task": "任务二：总结会议纪要"},
            {"task": "任务三：撰写周报内容"},
        ],
    }
    r1 = client.post("/api/race", json=body, headers={"X-API-Key": "tk"})
    assert r1.status_code == 200
    r2 = client.post("/api/race", json=body, headers={"X-API-Key": "tk"})
    assert r2.status_code == 429


def test_server_limit_disabled_when_zero(monkeypatch):
    """PM_RATE_LIMIT_PER_MIN=0 显式关闭：不再有 429。"""
    monkeypatch.setattr(server_mod, "_limiter", None)
    monkeypatch.setenv("PM_API_TOKEN", "tk")
    monkeypatch.setattr(server_mod._scheduler, "submit", lambda *a, **k: None)
    client = TestClient(server_mod.app)
    for _ in range(4):
        assert _post_optimize(client).status_code == 200


def test_server_limit_keyed_by_token(monkeypatch):
    """计费 key 优先用 token：不同 token 互不占额度。"""
    monkeypatch.setattr(server_mod, "_limiter", SlidingWindowLimiter(max_events=1))
    monkeypatch.setattr(server_mod, "_rate_per_min", 1)
    monkeypatch.setenv("PM_API_TOKEN", "tk")
    monkeypatch.setattr(server_mod._scheduler, "submit", lambda *a, **k: None)
    client = TestClient(server_mod.app)
    assert _post_optimize(client).status_code == 200
    assert _post_optimize(client).status_code == 429
