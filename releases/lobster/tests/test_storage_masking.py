"""M3.2 存储脱敏 + M3.4b SQLite 存储测试（2026-09-24）。

与 test_lobster.py 相同的密封 client 夹具（DB/快照均 tmp 隔离）。
覆盖：入岸即掩码（落库零明文）、掩码函数幂等、GET 输出口径不变、
掩码串伪装输入 422 拒绝、JSON 快照导入后的订单同样保持掩码。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8")

import app as lobster_app  # noqa: E402  需先注入 app 目录到 sys.path（conftest 同款）


def test_mask_phone_idempotent():
    """掩码函数幂等：明文→掩码；掩码→原样；非 11 位原样。"""
    assert lobster_app.mask_phone("13800138001") == "138****8001"
    assert lobster_app.mask_phone("138****8001") == "138****8001"
    assert lobster_app.mask_phone("123") == "123"
    assert lobster_app.mask_phone("") == ""
    assert lobster_app.mask_phone(None) == ""  # or 兜底空串，不抛异常


def test_storage_masks_phone_at_intake(client):
    """入岸即脱敏：POST 成功后，DB 里不允许出现 11 位明文手机号。"""
    r = client.post("/api/orders", json={"name": "m32", "phone": "13800138001", "spec": "A", "qty": 1})
    assert r.status_code == 200
    rows = [json.loads(row["data"]) for row in
            lobster_app._db_connect().execute("SELECT data FROM orders")]
    assert len(rows) == 1
    assert rows[0]["phone"] == "138****8001"
    # GET 输出口径不变（掩码串对接口层再掩一次应稳定）
    lst = client.get("/api/orders").json()
    assert lst["count"] == 1
    assert lst["orders"][0]["phone"] == "138****8001"


def test_masked_input_rejected_at_intake(client):
    """掩码串不是合法手机号：PHONE_RE 校验在前，伪装输入 422 拒绝。"""
    r = client.post("/api/orders", json={"name": "x", "phone": "138****8001", "spec": "A", "qty": 1})
    assert r.status_code == 422
    assert lobster_app._load()["orders"] == [], "被拒订单不应落库"


def test_json_import_preserves_masking(client, tmp_path):
    """JSON 快照导入：快照里的掩码手机号原样进 DB（不二次改写、不复活明文）。"""
    snap = {"version": 1, "orders": [
        {"order_id": "LOB-20260901-AAAA", "created_at": "2026-09-01 10:00:00",
         "name": "旧单", "phone": "138****7777", "spec": "A", "spec_label": "x",
         "ref_price_cny": 399, "qty": 1, "amount_cny": 399, "deliver_date": "",
         "note": "", "status": "delivered", "history": []}]}
    (tmp_path / "orders.json").write_text(json.dumps(snap, ensure_ascii=False), encoding="utf-8")
    lst = client.get("/api/orders").json()
    assert lst["count"] == 1 and lst["orders"][0]["phone"] == "138****7777"
    # 新订单继续入岸掩码
    client.post("/api/orders", json={"name": "新单", "phone": "13900139000", "spec": "B", "qty": 1})
    rows = [json.loads(row["data"]) for row in
            lobster_app._db_connect().execute("SELECT data FROM orders")]
    assert rows[1]["phone"] == "139****9000"


# ---------------------------------------------------------------------------
# W8 收口（2026-11-20）：自由文本不再是脱敏范围外
# ---------------------------------------------------------------------------
def test_mask_free_text_masks_embedded_phones():
    """note 里夹带的号码也要掩；已掩码/非 11 位/长数字串里的片段不动。"""
    f = lobster_app.mask_free_text
    assert f("打我 13800138001") == "打我 138****8001"
    assert f("13800138001/13900139000") == "138****8001/139****9000"
    assert f("已掩码 138****8001") == "已掩码 138****8001"
    assert f("订单号 2026090113800138001") == "订单号 2026090113800138001"  # 前后贴数字，不算号码
    assert f("") == ""
    assert f(None) == ""


def test_storage_masks_note_at_intake(client):
    """入岸即脱敏的第二个字段：note 落库不允许残留 11 位明文。"""
    r = client.post("/api/orders", json={
        "name": "w8", "phone": "13800138001", "spec": "A", "qty": 1,
        "note": "客户说他另一个号 13711112222 也行"})
    assert r.status_code == 200
    rows = [json.loads(row["data"]) for row in
            lobster_app._db_connect().execute("SELECT data FROM orders")]
    assert "13711112222" not in json.dumps(rows, ensure_ascii=False)
    assert rows[0]["note"] == "客户说他另一个号 137****2222 也行"
    # 接口层同样不返回明文
    assert "13711112222" not in json.dumps(client.get("/api/orders").json(), ensure_ascii=False)


def test_status_note_is_masked_too(client):
    """状态流转的备注是第三个落盘点，别漏。"""
    oid = client.post("/api/orders", json={
        "name": "w8b", "phone": "13800138001", "spec": "A", "qty": 1}).json()["order_id"]
    client.patch(f"/api/orders/{oid}", json={"status": "confirmed", "note": "联系 13633334444"})
    rows = [json.loads(row["data"]) for row in
            lobster_app._db_connect().execute("SELECT data FROM orders")]
    assert "13633334444" not in json.dumps(rows, ensure_ascii=False)
    assert rows[0]["history"][-1]["note"] == "联系 136****4444"
