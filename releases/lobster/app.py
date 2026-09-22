"""澳洲龙虾发布包 · 订购与台账服务（demo 演示口径）。

FastAPI + JSON 落盘台账（data/orders.json，带文件锁与原子写）。
端点：
  GET  /                  落地页（含订购表单）
  GET  /ledger            台账管理页
  GET  /poster            宣传海报
  POST /api/orders        创建订单（校验+台账落盘）
  GET  /api/orders        台账查询（?status= 过滤）
  PATCH /api/orders/{id}  状态流转（含缺货/死虾赔付/退款三分支）
  GET  /api/health        健康检查
启动：python app.py --port 8791
"""

from __future__ import annotations

import argparse
import json
import os
import re
import threading
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
ORDERS = DATA / "orders.json"
STATIC = ROOT / "static"

_LOCK = threading.Lock()

app = FastAPI(title="Aussie Lobster Release", version="1.0.0-demo")

SPECS = {
    "A": {"label": "澳龙 600–800g（1 只装）", "ref_price_cny": 399, "unit": "只"},
    "B": {"label": "澳龙 800g–1.2kg（1 只装）", "ref_price_cny": 599, "unit": "只"},
    "C": {"label": "澳龙 1.2–1.5kg（1 只装）", "ref_price_cny": 799, "unit": "只"},
    "D": {"label": "澳龙 1.5kg+（礼盒装）", "ref_price_cny": 1099, "unit": "盒"},
}
STATUSES = {"pending", "confirmed", "shipped", "delivered", "out_of_stock", "refunded", "dead_compensation"}
STATUS_LABEL = {
    "pending": "待确认", "confirmed": "已确认", "shipped": "冷链在途", "delivered": "已签收",
    "out_of_stock": "缺货-已退款/改期", "refunded": "已退款", "dead_compensation": "死虾-已赔付",
}
PHONE_RE = re.compile(r"^1[3-9]\d{9}$")


def _load() -> dict:
    if not ORDERS.exists():
        return {"version": 1, "orders": []}
    try:
        data = json.loads(ORDERS.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=500, detail=f"台账文件损坏：{e}（可用 backups 恢复，见交接文档）") from e
    return data


def _save(data: dict) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = ORDERS.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, ORDERS)


class OrderIn(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    phone: str
    spec: str
    qty: int = Field(ge=1, le=20)
    deliver_date: str = ""
    note: str = Field(default="", max_length=200)


class StatusIn(BaseModel):
    status: str
    note: str = Field(default="", max_length=200)


@app.middleware("http")
async def require_token(request: Request, call_next):
    """鉴权：设了 LOBSTER_TOKEN 后，除 health/页面 GET 外均需 X-API-Key。"""
    token = (os.getenv("LOBSTER_TOKEN") or "").strip()
    if token:
        path = request.url.path
        open_paths = ("/api/health", "/", "/ledger", "/poster")
        is_read = request.method in ("GET", "HEAD") and path in open_paths
        if not is_read and (request.headers.get("X-API-Key") or "").strip() != token:
            return JSONResponse(status_code=401, content={"detail": "需要 X-API-Key 令牌"})
    return await call_next(request)


@app.get("/api/health")
def health():
    return {"status": "ok", "service": "lobster-release", "mode": "demo", "time": time.time()}


@app.post("/api/orders")
def create_order(o: OrderIn):
    if o.spec not in SPECS:
        raise HTTPException(status_code=422, detail=f"无效规格 {o.spec!r}，可选：{list(SPECS)}")
    if not PHONE_RE.match(o.phone):
        raise HTTPException(status_code=422, detail="手机号格式无效（11 位大陆手机号）")
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    oid = "LOB-" + time.strftime("%Y%m%d") + "-" + uuid.uuid4().hex[:4].upper()
    rec = {
        "order_id": oid,
        "created_at": now,
        "name": o.name,
        "phone": o.phone,
        "spec": o.spec,
        "spec_label": SPECS[o.spec]["label"],
        "ref_price_cny": SPECS[o.spec]["ref_price_cny"],
        "qty": o.qty,
        "amount_cny": SPECS[o.spec]["ref_price_cny"] * o.qty,
        "deliver_date": o.deliver_date,
        "note": o.note,
        "status": "pending",
        "history": [{"ts": now, "action": "创建订单", "status": "pending", "note": "demo 模拟数据"}],
    }
    with _LOCK:
        data = _load()
        data["orders"].append(rec)
        _save(data)
    return {"ok": True, "order_id": oid, "status": "pending", "amount_cny": rec["amount_cny"]}


@app.get("/api/orders")
def list_orders(status: str | None = None):
    with _LOCK:
        data = _load()
    rows = data["orders"]
    if status:
        if status not in STATUSES:
            raise HTTPException(status_code=422, detail=f"无效状态 {status!r}")
        rows = [r for r in rows if r["status"] == status]
    # PII 脱敏（W8）：接口返回手机号只留尾 4 位；落盘原文保留仅供客服核对（demo 边界）
    masked = []
    for r in list(reversed(rows)):
        r2 = dict(r)
        ph = str(r2.get("phone", ""))
        r2["phone"] = (ph[:3] + "****" + ph[-4:]) if len(ph) == 11 else ph
        masked.append(r2)
    return {"count": len(rows), "orders": masked, "status_labels": STATUS_LABEL}


@app.patch("/api/orders/{oid}")
def update_status(oid: str, request: Request, s: StatusIn | None = None):
    # D-L1 容错：部分客户端（如 PowerShell Invoke-RestMethod）不带 charset 时
    # 中文按 Latin-1 发送，导致 note 变 "?"。统一按字节读入并尝试 UTF-8 解码修复。
    import asyncio
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = None
    raw = loop.run_until_complete(request.body()) if loop else b""
    if raw and s is not None and "?" in (s.note or ""):
        try:
            fixed = json.loads(raw.decode("utf-8"))
            s = StatusIn(status=fixed.get("status", s.status), note=fixed.get("note", ""))
        except Exception:  # noqa: BLE001 - 解码失败则按原值（真乱码数据不掩盖）
            pass
    if s is None:
        raise HTTPException(status_code=422, detail="缺少请求体")
    if s.status not in STATUSES:
        raise HTTPException(status_code=422, detail=f"无效状态 {s.status!r}，可选：{sorted(STATUSES)}") from None
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    with _LOCK:
        data = _load()
        rec = next((r for r in data["orders"] if r["order_id"] == oid), None)
        if rec is None:
            raise HTTPException(status_code=404, detail=f"订单不存在：{oid}")
        rec["status"] = s.status
        rec["history"].append({"ts": now, "action": "状态流转", "status": s.status, "note": s.note})
        _save(data)
    return {"ok": True, "order_id": oid, "status": s.status, "label": STATUS_LABEL[s.status]}


@app.get("/")
def landing():
    return FileResponse(STATIC / "landing.html", media_type="text/html")


@app.get("/ledger")
def ledger():
    return FileResponse(STATIC / "ledger.html", media_type="text/html")


@app.get("/poster")
def poster():
    return FileResponse(STATIC / "poster.html", media_type="text/html")


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "prompt-master"))
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8791)
    args = ap.parse_args()
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="info")
