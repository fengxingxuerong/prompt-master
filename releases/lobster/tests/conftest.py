import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app as lobster_app
import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """M3.4b 起全密封：DB 与 JSON 快照均指向 tmp，测试不再污染真实台账。"""
    monkeypatch.setattr(lobster_app, "DB", tmp_path / "orders.db")
    monkeypatch.setattr(lobster_app, "ORDERS", tmp_path / "orders.json")
    monkeypatch.delenv("LOBSTER_TOKEN", raising=False)
    return TestClient(lobster_app.app)
