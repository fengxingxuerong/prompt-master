"""LOBSTER_TOKEN 的三态回归 —— 补 W7 豁免缺的那一半：证据。

豁免本身没动：token 未设时仍是"本地开放"，那是用户 2026-09-21 的书面决定。
但 `releases/suite/security-waiver.html` 里写着"机制已实测就位"，而 2026-09-25 盘点时
**lobster 与 taskboard 的鉴权一条用例都没有**（只有会审台有 `tests/test_auth.py`）——
一句话没有出处，就等于下一个接手的人只能选择信不信。这里把出处补上。

`client` 夹具来自本目录 conftest.py：已封 DB/ORDERS，并 delenv LOBSTER_TOKEN
（W16 教训：宿主环境里残留的 token 会静默改变断言方向）。
"""

from __future__ import annotations

ORDER = {"name": "张三", "phone": "13800001111", "spec": "A", "qty": 1, "note": "鉴权用例"}


def test_no_token_open_access(client):
    """态一（缺省）：不设 token，读写全开放 —— 与历史行为零差异。"""
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/orders").status_code == 200
    assert client.post("/api/orders", json=ORDER).status_code == 200


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


def test_empty_token_env_treated_as_unset(client, monkeypatch):
    """设了但是空白 = 未设：否则 `LOBSTER_TOKEN=""` 会把整套鉴权静默关掉。"""
    monkeypatch.setenv("LOBSTER_TOKEN", "   ")
    assert client.get("/api/orders").status_code == 200
