import sys
sys.stdout.reconfigure(encoding="utf-8")

def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"

def test_landing_page(client):
    r = client.get("/")
    assert r.status_code == 200 and "AUSSIE LOBSTER" in r.text

def test_create_order_ok(client):
    r = client.post("/api/orders", json={"name": "pytest", "phone": "13800138001", "spec": "A", "qty": 1})
    assert r.status_code == 200
    d = r.json()
    assert d["order_id"].startswith("LOB-") and d["amount_cny"] == 399

def test_create_order_invalid_phone(client):
    r = client.post("/api/orders", json={"name": "x", "phone": "123", "spec": "A", "qty": 1})
    assert r.status_code == 422

def test_create_order_invalid_spec(client):
    r = client.post("/api/orders", json={"name": "x", "phone": "13800138002", "spec": "Z", "qty": 1})
    assert r.status_code == 422

def test_list_orders_masks_phone(client):
    r = client.get("/api/orders")
    d = r.json()
    assert r.status_code == 200 and d["count"] > 0
    assert "****" in d["orders"][0]["phone"]  # PII 脱敏在位

def test_status_transition_and_404(client):
    r = client.post("/api/orders", json={"name": "trans", "phone": "13800138003", "spec": "B", "qty": 1})
    oid = r.json()["order_id"]
    p = client.patch(f"/api/orders/{oid}", json={"status": "confirmed", "note": "pytest"})
    assert p.status_code == 200
    nf = client.patch("/api/orders/LOB-NOPE", json={"status": "confirmed"})
    assert nf.status_code == 404
