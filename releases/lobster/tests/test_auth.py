"""LOBSTER_TOKEN 的四态回归 —— W7 豁免于 2026-11-20 收回后的口径。

历史：2026-09-21 用户指令「安全方面不要考虑」，W7（三服务无鉴权）转书面豁免；
2026-09-25 补了「机制已实测就位」的证据；2026-11-20 收回豁免，缺省从
「没配令牌就全开」改为「只有本机同源调用放行，跨机 / 跨站一律 401」。

四条态：
  一、未配令牌 + 本机同源      → 放行（本机能发请求的人本就能读盘，不算新暴露面）
  二、未配令牌 + 跨机 / 跨站   → 401（W7 真正的靶子：局域网写接口 + CSRF）
  三、配了令牌                 → 除 health/页面 GET 外均需 X-API-Key（原口径不变）
  四、显式 LOBSTER_ALLOW_OPEN=1 → 无条件放行（给人留的紧急出口，必须手写）

`client` 夹具来自本目录 conftest.py：已封 DB/ORDERS、delenv LOBSTER_TOKEN，
并**故意**设了 ALLOW_OPEN=1（业务用例不该被鉴权拖红）。本文件每条用例自己
delenv 它，否则等于把断言方向改掉。
"""

from __future__ import annotations

import app as lobster_app
from fastapi.testclient import TestClient

ORDER = {"name": "张三", "phone": "13800001111", "spec": "A", "qty": 1, "note": "鉴权用例"}


def _as(host: str) -> TestClient:
    """指定来源地址的客户端：模拟本机 / 局域网另一台机器。"""
    return TestClient(lobster_app.app, client=(host, 50000))


# ---------------------------------------------------------------------------
# 态一：未配令牌 + 本机同源 —— 放行，与历史本地体验一致
# ---------------------------------------------------------------------------
def test_no_token_local_caller_passes(monkeypatch):
    monkeypatch.delenv("LOBSTER_TOKEN", raising=False)
    monkeypatch.delenv("LOBSTER_ALLOW_OPEN", raising=False)
    c = _as("127.0.0.1")
    assert c.get("/api/health").status_code == 200
    assert c.get("/api/orders").status_code == 200
    assert c.post("/api/orders", json=ORDER).status_code == 200


def test_no_token_same_origin_page_passes(monkeypatch):
    """回环 + 同源 Origin（页面自己发的 fetch）也要放行。"""
    monkeypatch.delenv("LOBSTER_TOKEN", raising=False)
    monkeypatch.delenv("LOBSTER_ALLOW_OPEN", raising=False)
    c = _as("127.0.0.1")
    h = {"origin": "http://127.0.0.1:8791", "host": "127.0.0.1:8791"}
    assert c.post("/api/orders", json=ORDER, headers=h).status_code == 200


# ---------------------------------------------------------------------------
# 态二：未配令牌 + 跨机 / 跨站 —— 401（这是 W7 收回的实质）
# ---------------------------------------------------------------------------
def test_no_token_remote_caller_rejected(monkeypatch):
    monkeypatch.delenv("LOBSTER_TOKEN", raising=False)
    monkeypatch.delenv("LOBSTER_ALLOW_OPEN", raising=False)
    c = _as("192.168.1.9")
    assert c.post("/api/orders", json=ORDER).status_code == 401
    # 订单含手机号：数据读同样不放
    assert c.get("/api/orders").status_code == 401
    # 页面与健康检查永远可见，否则服务连排查入口都没有
    assert c.get("/api/health").status_code == 200
    assert c.get("/").status_code == 200


def test_no_token_cross_site_origin_rejected(monkeypatch):
    """回环来源 + 跨站 Origin：浏览器里随便一个网页不能对本机端口发 POST。"""
    monkeypatch.delenv("LOBSTER_TOKEN", raising=False)
    monkeypatch.delenv("LOBSTER_ALLOW_OPEN", raising=False)
    c = _as("127.0.0.1")
    h = {"origin": "http://evil.example", "host": "127.0.0.1:8791"}
    assert c.post("/api/orders", json=ORDER, headers=h).status_code == 401


def test_no_token_rejection_says_how_to_fix(monkeypatch):
    """错误文本必须自解释：接手的人不该靠读源码才知道有 ALLOW_OPEN。"""
    monkeypatch.delenv("LOBSTER_TOKEN", raising=False)
    monkeypatch.delenv("LOBSTER_ALLOW_OPEN", raising=False)
    detail = _as("192.168.1.9").post("/api/orders", json=ORDER).json()["detail"]
    assert "LOBSTER_TOKEN" in detail and "LOBSTER_ALLOW_OPEN" in detail


# ---------------------------------------------------------------------------
# 态四：显式放行开关 —— 写清 1/true/yes/on 才生效
# ---------------------------------------------------------------------------
def test_allow_open_env_restores_legacy_open(monkeypatch):
    monkeypatch.delenv("LOBSTER_TOKEN", raising=False)
    for val in ("1", "true", "YES", "on"):
        monkeypatch.setenv("LOBSTER_ALLOW_OPEN", val)
        assert _as("192.168.1.9").post("/api/orders", json=ORDER).status_code == 200, val


def test_allow_open_env_must_be_explicit(monkeypatch):
    """`0` / `false` / 空串一律不算放行 —— 否则「没写」和「写错」都能打开。"""
    monkeypatch.delenv("LOBSTER_TOKEN", raising=False)
    for val in ("0", "false", "", "  "):
        monkeypatch.setenv("LOBSTER_ALLOW_OPEN", val)
        assert _as("192.168.1.9").post("/api/orders", json=ORDER).status_code == 401, val


# ---------------------------------------------------------------------------
# 态三：配了令牌 —— 原口径不变
# ---------------------------------------------------------------------------
def test_token_set_health_and_pages_exempt(client, monkeypatch):
    monkeypatch.setenv("LOBSTER_TOKEN", "secret-1")
    assert client.get("/api/health").status_code == 200
    for page in ("/", "/ledger", "/poster"):
        assert client.get(page).status_code == 200, page


def test_token_set_writes_rejected_without_key(client, monkeypatch):
    monkeypatch.setenv("LOBSTER_TOKEN", "secret-1")
    assert client.post("/api/orders", json=ORDER).status_code == 401
    assert client.patch("/api/orders/TASK-1", json={"status": "confirmed"}).status_code == 401


def test_token_set_data_reads_rejected_without_key(client, monkeypatch):
    """W8 教训：订单里有客户手机号，所以**数据读**也必须凭据化，不能只挡写。"""
    monkeypatch.setenv("LOBSTER_TOKEN", "secret-1")
    for path in ("/api/orders", "/api/orders/1"):
        assert client.get(path).status_code == 401, path


def test_token_set_valid_key_passes(client, monkeypatch):
    monkeypatch.setenv("LOBSTER_TOKEN", "secret-1")
    h = {"X-API-Key": "secret-1"}
    assert client.get("/api/orders", headers=h).status_code == 200
    assert client.post("/api/orders", json=ORDER, headers=h).status_code == 200


def test_token_whitespace_tolerant(client, monkeypatch):
    """两端去空白：env 带空格与 header 带空格都应与干净值匹配（中间件两边都 .strip()）。"""
    monkeypatch.setenv("LOBSTER_TOKEN", "  secret-1  ")
    assert client.get("/api/orders", headers={"X-API-Key": "secret-1"}).status_code == 200
    assert client.get("/api/orders", headers={"X-API-Key": "  secret-1  "}).status_code == 200
    assert client.get("/api/orders").status_code == 401


def test_empty_token_env_treated_as_unset(monkeypatch):
    """设了但是空白 = 未设：既不是凭据（跨机仍 401），也不改变本机放行。"""
    monkeypatch.setenv("LOBSTER_TOKEN", "   ")
    monkeypatch.delenv("LOBSTER_ALLOW_OPEN", raising=False)
    assert _as("127.0.0.1").get("/api/orders").status_code == 200
    assert _as("192.168.1.9").get("/api/orders").status_code == 401
