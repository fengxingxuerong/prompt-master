import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app as lobster_app
import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """M3.4b 起全密封：DB 与 JSON 快照均指向 tmp，测试不再污染真实台账。

    W7 收口（2026-11-20）后，未配令牌时只有「本机同源」调用放行，而 TestClient 的来源
    是 `testclient`（非回环）⇒ 业务用例会成片 401。这里显式打开放行开关，让业务用例
    继续测业务；鉴权语义由 test_auth.py 自己 delenv 后断言（否则等于把断言方向改掉）。
    """
    monkeypatch.setattr(lobster_app, "DB", tmp_path / "orders.db")
    monkeypatch.setattr(lobster_app, "ORDERS", tmp_path / "orders.json")
    monkeypatch.delenv("LOBSTER_TOKEN", raising=False)
    monkeypatch.setenv("LOBSTER_ALLOW_OPEN", "1")
    return TestClient(lobster_app.app)
