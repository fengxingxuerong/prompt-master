"""M3.2 存储脱敏测试（2026-09-23）。

与 test_lobster.py 不同：这里用 tmp_path 隔离 ORDERS，不读写真实台账。
覆盖：入岸即掩码（落盘零明文）、掩码函数幂等、GET 输出口径不变。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8")

import app as lobster_app  # noqa: E402  需先注入 app 目录到 sys.path（conftest 同款）
from fastapi.testclient import TestClient  # noqa: E402


def _client_with(tmp_orders, monkeypatch):
    monkeypatch.setattr(lobster_app, "ORDERS", tmp_orders)
    return TestClient(lobster_app.app)


def test_mask_phone_idempotent():
    """掩码函数幂等：明文→掩码；掩码→原样；非 11 位原样。"""
    assert lobster_app.mask_phone("13800138001") == "138****8001"
    assert lobster_app.mask_phone("138****8001") == "138****8001"
    assert lobster_app.mask_phone("123") == "123"
    assert lobster_app.mask_phone("") == ""
    assert lobster_app.mask_phone(None) == ""  # or 兜底空串，不抛异常


def test_storage_masks_phone_at_intake(tmp_path, monkeypatch):
    """入岸即脱敏：POST 成功后，落盘文件不允许出现 11 位明文手机号。"""
    orders_file = tmp_path / "orders.json"
    c = _client_with(orders_file, monkeypatch)
    r = c.post("/api/orders", json={"name": "m32", "phone": "13800138001", "spec": "A", "qty": 1})
    assert r.status_code == 200
    raw = orders_file.read_text(encoding="utf-8")
    assert "13800138001" not in raw, "落盘出现明文手机号！"
    assert "138****8001" in raw
    # GET 输出口径不变（掩码串对接口层再掩一次应稳定）
    lst = c.get("/api/orders").json()
    assert lst["count"] == 1
    assert lst["orders"][0]["phone"] == "138****8001"


def test_masked_input_rejected_at_intake(tmp_path, monkeypatch):
    """掩码串不是合法手机号：PHONE_RE 校验在前，伪装输入 422 拒绝。"""
    orders_file = tmp_path / "orders.json"
    c = _client_with(orders_file, monkeypatch)
    r = c.post("/api/orders", json={"name": "x", "phone": "138****8001", "spec": "A", "qty": 1})
    assert r.status_code == 422
    assert not orders_file.exists(), "被拒订单不应落盘"
