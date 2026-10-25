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
import contextlib
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
ORDERS = DATA / "orders.json"  # JSON 快照：仅作 SQLite 首次启动的种子数据（M3.4 后不再更新）
DB = DATA / "orders.db"  # SQLite 真值源（WAL；*.db* 已 gitignore）
STATIC = ROOT / "static"

_LOCK = threading.Lock()

app = FastAPI(title="Aussie Lobster Release", version="1.1.0-demo")

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


def mask_phone(ph: str) -> str:
    """手机号脱敏（M3.2 存储脱敏 2026-09-23）：保留前 3 后 4，中间 ****。

    幂等：已是掩码形态或非 11 位原样返回——存储层（create_order）与
    接口层（list_orders）共用，对旧明文数据与新掩码数据双重安全。
    """
    ph = str(ph or "")
    if "*" in ph or len(ph) != 11:
        return ph
    return ph[:3] + "****" + ph[-4:]


def _db_connect() -> sqlite3.Connection:
    """M3.4b 存储地基（2026-09-24）：SQLite WAL 替代单机 JSON——跨进程并发写安全。

    同 taskboard M3.4a 模式：连接即开即关，事务 + busy_timeout 把并发写串行化。
    """
    conn = sqlite3.connect(DB, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _init_db(conn: sqlite3.Connection) -> None:
    """建表 + 一次性导入 JSON 快照（seeded 旗标保证只导一次，之后 JSON 只读不动）。"""
    conn.execute("CREATE TABLE IF NOT EXISTS orders (order_id TEXT PRIMARY KEY, data TEXT NOT NULL)")
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    if conn.execute("SELECT 1 FROM meta WHERE key='seeded'").fetchone():
        return
    if ORDERS.exists():
        try:
            data = json.loads(ORDERS.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise HTTPException(
                status_code=500,
                detail=f"JSON 快照损坏且尚无数据库：{e}（可用 backups 恢复，见交接文档）",
            ) from e
        with conn:
            for o in data.get("orders", []):
                conn.execute("INSERT OR REPLACE INTO orders (order_id, data) VALUES (?, ?)",
                             (o["order_id"], json.dumps(o, ensure_ascii=False)))
            conn.execute("INSERT OR REPLACE INTO meta VALUES ('seeded', ?)", (ORDERS.name,))
    else:
        with conn:
            conn.execute("INSERT OR REPLACE INTO meta VALUES ('seeded', 'none')")


def _load() -> dict:
    """返回与 JSON 时代完全同构的文档（{version, orders}），路由层零改动。"""
    conn = _db_connect()
    try:
        _init_db(conn)
        return _load_in(conn)
    finally:
        conn.close()


def _load_in(conn: sqlite3.Connection) -> dict:
    """在**给定连接**上读全量文档（供事务内使用，不能再自己开连接）。"""
    orders = [json.loads(r["data"]) for r in conn.execute("SELECT data FROM orders ORDER BY rowid")]
    return {"version": 1, "orders": orders}


@contextlib.contextmanager
def _txn():
    """`BEGIN IMMEDIATE` 的读改写事务：先拿写锁，再读。

    为什么必须这样（2026-10-31 修，实测事故）：旧版写路径是
    `with _LOCK: data=_load(); 改; _save(data)` —— `_LOCK` 只是**进程内**锁，而
    `_save` 是 `DELETE FROM orders` + 全量 re-INSERT。两个实例（≈ 两个进程）各自
    load→append→save 时，后写的那一份把前一份整份覆盖：实测写前 2 条、并行新增
    LOB-P1/P2 后只剩 3 条，**LOB-P1 消失**，而两个请求都回 200。

    `BEGIN IMMEDIATE` 而不是默认的 deferred：deferred 只在**写**时取锁，
    "读整表 → 改一条 → 写回"中间那次读不受保护，另一个写者恰好在那时提交，
    我这笔写就会把它盖掉（lost update）。triage 早就是这么做的，这里有现成范式。
    """
    conn = _db_connect()
    try:
        _init_db(conn)
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()


def _upsert(conn: sqlite3.Connection, rec: dict) -> None:
    """只写这一条（不 DELETE 全表）：全量替换在多写者下就是丢数据。"""
    conn.execute(
        "INSERT OR REPLACE INTO orders (order_id, data) VALUES (?, ?)",
        (rec["order_id"], json.dumps(rec, ensure_ascii=False)),
    )


def _get_one(conn: sqlite3.Connection, order_id: str) -> dict | None:
    row = conn.execute("SELECT data FROM orders WHERE order_id=?", (order_id,)).fetchone()
    return json.loads(row["data"]) if row else None


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
    try:
        n = len(_load()["orders"])
        storage = "sqlite(wal)"
    except Exception as e:  # noqa: BLE001 - 健康端点永不 500，存储异常如实暴露
        n, storage = -1, f"error: {type(e).__name__}"
    return {"status": "ok", "service": "lobster-release", "mode": "demo",
            "time": time.time(), "storage": storage, "orders": n}


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
        # M3.2 存储脱敏：入岸即掩码，落盘不再保留 11 位明文（W8 存储侧收口）
        "phone": mask_phone(o.phone),
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
    with _txn() as conn:
        # 只 INSERT 这一条：全量替换在多写者下会把别人的新增覆盖掉（见 `_txn` 的说明）
        _upsert(conn, rec)
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
    # PII 脱敏（W8）：M3.2 起存储层即掩码（create_order 入岸掩码 + mask_storage_phones.py
    # 存量迁移）；此处对历史/异常数据兜底再掩一次（mask_phone 幂等）。
    masked = []
    for r in list(reversed(rows)):
        r2 = dict(r)
        r2["phone"] = mask_phone(r2.get("phone", ""))
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
    with _txn() as conn:
        # 事务内读那一条再写回：`BEGIN IMMEDIATE` 保证这段里有写锁，
        # 别的写者不会在我"读完改完还没写"的窗口里插进来（lost update）
        rec = _get_one(conn, oid)
        if rec is None:
            raise HTTPException(status_code=404, detail=f"订单不存在：{oid}")
        rec.setdefault("history", []).append(
            {"ts": now, "action": "状态流转", "status": s.status, "note": s.note}
        )
        rec["status"] = s.status
        _upsert(conn, rec)
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
