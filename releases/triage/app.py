"""多模型会审台（Triage Panel）v1.1 — 实战问题驱动优化版。

v1.0 实战暴露的问题 → v1.1 修复对照：
  P-01 会审耗时被最慢评委拖底      → majority-return：≥3 张有效票即汇总，迟到票标记 late 并留痕
  P-02 评委健康度无记忆            → 滑动窗口健康度：连续 2 次超时/失败的评委降为 late-ok（不阻塞汇总）
  P-03 JSON 围栏/前后缀解析失败    → parse_verdict 剥离 ```json 围栏与前后缀文本，正则提取首个 JSON 对象
  P-04 UI 无进度反馈               → （保持轮询简单，UI 文案已注明耗时区间；结构性优化留给 v1.2）

台账新增 kind=late_review（迟到票留痕，可追溯）。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
import threading
import time
import urllib.request
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse
from notify_center import list_notifications, load_config, notify
from pydantic import BaseModel, Field

APP_DIR = Path(__file__).resolve().parent
DATA = APP_DIR / "data"
DATA.mkdir(exist_ok=True)
LEDGER = DATA / "ledger.jsonl"
SESSIONS = DATA / "sessions.json"  # JSON 快照：仅作 SQLite 首次启动的种子数据（M3.4 后不再更新）
DB = DATA / "sessions.db"  # SQLite 真值源（WAL；*.db* 已 gitignore）
HEALTH_FILE = DATA / "reviewer_health.json"

HUB = "http://127.0.0.1:8687/v1/chat/completions"

REVIEWERS = [
    {"seat": "评委A", "model": "deepseek-v4-flash", "source": "sensenova"},
    {"seat": "评委B", "model": "glm-5.2", "source": "sensenova"},
    {"seat": "评委C", "model": "sensenova-6.8-flash-lite", "source": "sensenova"},
    {"seat": "评委D", "model": "amd-qwen3.8-27b", "source": "amd"},
    {"seat": "评委E", "model": "step-step-5-preview", "source": "stepfun"},
    {"seat": "评委F", "model": "nvidia-glm-5.3-flash", "source": "nvidia"},
]

REVIEW_SYSTEM = (
    "你是客服工单研判评委。对工单输出严格 JSON 对象（不要代码围栏、不要解释文字）："
    '{"category":"billing|technical|logistics|complaint|other",'
    '"severity":"P1|P2|P3","action":"一句话建议动作","confidence":0.0到1.0}'
    " 分类口径：billing=账单/退款/支付；technical=技术故障/API/系统；"
    "logistics=物流/配送/签收；complaint=情绪投诉/舆情风险；other=其他。"
    " severity 口径：P1=资金损失/生产故障/舆情正在发生；P2=功能受损但有替代路径；P3=咨询/轻微不便。"
    " action 必须是客服可直接执行的下一步（含时限或动作对象），不许写\"尽快处理\"这类空话。"
    " confidence 锚点：0.9+=诉求明确；0.7~0.8=主题明确缺关键细节；≤0.6=信息不足以分类。"
)

SEV_ORDER = {"P1": 3, "P2": 2, "P3": 1}
MAJORITY = 3          # ≥3 有效票即提前汇总
UNHEALTHY_STREAK = 2  # 连续失败 2 次即视为不健康（不阻塞汇总）

_lock = threading.Lock()
app = FastAPI(title="Triage Panel", version="1.3.1")
app.add_middleware(GZipMiddleware, minimum_size=1024)


@app.middleware("http")
async def require_token(request: Request, call_next):
    """鉴权（M3.1 安全收口 2026-09-23）：设了 TRIAGE_TOKEN 后，除 /api/health 与页面
    （/ 与 /static/*）GET 外，其余端点（含全部写操作与 /api 数据读）均需 X-API-Key。

    与 taskboard/lobster 同款环境变量门控模式：默认未设 = 本地全开放（便利优先），
    局域网/公网暴露前必须设置。比 taskboard 版更收紧的一处：/api/sessions* 等数据读
    也在保护范围内——工单正文含客户内容（W8 教训：明文 PII 不应无凭据可读）。
    """
    token = (os.getenv("TRIAGE_TOKEN") or "").strip()
    if token:
        path = request.url.path
        page_get = request.method in ("GET", "HEAD") and (
            path == "/" or path.startswith("/static/") or path == "/api/health")
        if not page_get:
            from fastapi.responses import JSONResponse
            if (request.headers.get("X-API-Key") or "").strip() != token:
                return JSONResponse(status_code=401,
                                    content={"detail": "需要 X-API-Key 令牌"})
    return await call_next(request)


# ---------- 基础设施 ----------
def append_ledger(rec: dict) -> None:
    rec["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    with _lock:
        with open(LEDGER, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _db_connect() -> sqlite3.Connection:
    """M3.4c 存储地基（2026-09-24）：sessions 从单机 JSON 迁 SQLite WAL。

    同 taskboard/lobster M3.4a/b 模式：连接即开即关，事务 + busy_timeout
    把并发写串行化；ledger.jsonl 是纯追加台账不受写放大影响，维持 JSONL。
    """
    conn = sqlite3.connect(DB, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _init_db(conn: sqlite3.Connection) -> None:
    """建表 + 一次性导入 JSON 快照（seeded 旗标保证只导一次；坏快照不阻塞建库，D-013）。"""
    conn.execute("CREATE TABLE IF NOT EXISTS sessions (session_id TEXT PRIMARY KEY, data TEXT NOT NULL)")
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    if conn.execute("SELECT 1 FROM meta WHERE key='seeded'").fetchone():
        return
    if SESSIONS.exists():
        try:
            data = json.loads(SESSIONS.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - 坏快照不复活也不阻塞，D-013 教训
            data = {}
        with conn:
            for sid, s in (data or {}).items():
                conn.execute("INSERT OR REPLACE INTO sessions (session_id, data) VALUES (?, ?)",
                             (sid, json.dumps(s, ensure_ascii=False)))
            conn.execute("INSERT OR REPLACE INTO meta VALUES ('seeded', ?)", (SESSIONS.name,))
    else:
        with conn:
            conn.execute("INSERT OR REPLACE INTO meta VALUES ('seeded', 'none')")


def load_sessions() -> dict:
    """返回 {session_id: session} 全量视图（形状与 JSON 时代一致，路由层零改动）。

    DB 层异常返回空 dict（D-013 教训：坏存储不崩，等价旧版坏 JSON 兜底）。
    """
    try:
        conn = _db_connect()
        try:
            _init_db(conn)
            return {r["session_id"]: json.loads(r["data"])
                    for r in conn.execute("SELECT session_id, data FROM sessions")}
        finally:
            conn.close()
    except sqlite3.DatabaseError:
        return {}


def save_sessions(all_sessions: dict) -> None:
    """全量替换（兼容保留）。常规写路径请用 save_session 单行 upsert。"""
    with _lock:
        conn = _db_connect()
        try:
            with conn:
                _init_db(conn)
                conn.execute("DELETE FROM sessions")
                conn.executemany(
                    "INSERT INTO sessions (session_id, data) VALUES (?, ?)",
                    [(sid, json.dumps(s, ensure_ascii=False)) for sid, s in all_sessions.items()],
                )
        finally:
            conn.close()


def save_session(session: dict) -> None:
    """M3.4c 写放大治理：单行 upsert——每次会写只动一行，不再全量重写所有会话。"""
    with _lock:
        conn = _db_connect()
        try:
            with conn:
                _init_db(conn)
                conn.execute("INSERT OR REPLACE INTO sessions (session_id, data) VALUES (?, ?)",
                             (session["session_id"], json.dumps(session, ensure_ascii=False)))
        finally:
            conn.close()


def load_health() -> dict:
    if not HEALTH_FILE.exists():
        return {}
    try:
        return json.loads(HEALTH_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def bump_health(seat: str, ok: bool) -> None:
    with _lock:
        h = load_health()
        rec = h.get(seat, {"streak_fail": 0, "total_ok": 0, "total_fail": 0})
        if ok:
            rec["streak_fail"] = 0
            rec["total_ok"] += 1
        else:
            rec["streak_fail"] += 1
            rec["total_fail"] += 1
        rec["unhealthy"] = rec["streak_fail"] >= UNHEALTHY_STREAK
        h[seat] = rec
        HEALTH_FILE.write_text(json.dumps(h, ensure_ascii=False, indent=1),
                               encoding="utf-8")


_model_cache: dict = {"ts": 0.0, "names": set()}


def get_hub_models(ttl: float = 60.0) -> set:
    """懒加载网关模型白名单（60s 缓存）。v1.1.1 P-05：坏模型名快速失败。"""
    now = time.time()
    if _model_cache["names"] and now - _model_cache["ts"] < ttl:
        return _model_cache["names"]
    try:
        with urllib.request.urlopen("http://127.0.0.1:8687/v1/models", timeout=5) as r:
            d = json.loads(r.read().decode("utf-8"))
        names = {m["id"] for m in d.get("data", [])}
        _model_cache["ts"] = now
        _model_cache["names"] = names
        return names
    except Exception:  # noqa: BLE001 - 网关不可达时放行（由调用本身兜底）
        return _model_cache["names"] or {"__hub_unreachable__"}


def call_llm(model: str, system: str, user: str, max_tokens: int = 700,
             timeout: int = 60, hub: str = HUB) -> dict:
    body = {"model": model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "max_tokens": max_tokens, "temperature": 0.2}
    req = urllib.request.Request(
        hub, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        method="POST", headers={"Content-Type": "application/json"})
    t0 = time.time()
    known = get_hub_models()
    if known and model not in known:
        return {"ok": False, "latency_ms": 0,
                "error": f"unknown model {model!r} (not in hub pool, fast-fail)"}
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read().decode("utf-8"))
        lat = round((time.time() - t0) * 1000)
        content = (d.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""
        return {"ok": True, "latency_ms": lat, "content": content, "usage": d.get("usage", {})}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "latency_ms": round((time.time() - t0) * 1000),
                "error": f"{type(e).__name__}: {str(e)[:150]}"}


def parse_verdict(text: str) -> dict:
    """v1.1 容错解析：剥 ```json 围栏、剥前后缀文本，正则提取首个平衡 JSON 对象。"""
    s = text.strip()
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    try:
        return json.loads(s)
    except Exception:  # noqa: BLE001
        pass
    m = re.search(r"\{", s)
    if not m:
        raise ValueError("no JSON object found")
    depth = 0
    start = m.start()
    for i in range(start, len(s)):
        if s[i] == "{":
            depth += 1
        elif s[i] == "}":
            depth -= 1
            if depth == 0:
                return json.loads(s[start:i + 1])
    raise ValueError("unbalanced JSON")


# ---------- 核心流程 ----------
_progress_queues: dict[str, list] = {}
_q_lock = threading.Lock()


def _push_event(sid: str, event: str, data: dict) -> None:
    """向该会话的 SSE 订阅者投递事件（无订阅者则静默丢弃）。"""
    with _q_lock:
        qs = _progress_queues.get(sid)
        if qs:
            for q in qs:
                q.append((event, json.dumps(data, ensure_ascii=False)))


def run_panel(session: dict) -> None:
    _run_panel_core(session, notify=True)


def _run_panel_core(session: dict, notify: bool = False) -> None:
    ticket = session["ticket"]
    user_msg = f"工单主题：{ticket['subject']}\n工单正文：{ticket['body']}"
    t0 = time.time()
    health = load_health()

    def review_one(rv: dict) -> dict:
        # v1.1：不健康评委用更长超时（80s），但不阻塞汇总（见 majority 逻辑）
        timeout = 80 if health.get(rv["seat"], {}).get("unhealthy") else 60
        r = call_llm(rv["model"], REVIEW_SYSTEM, user_msg, timeout=timeout)
        bump_health(rv["seat"], r["ok"])
        rec = {"seat": rv["seat"], "model": rv["model"], "source": rv["source"],
               "latency_ms": r["latency_ms"], "ok": r["ok"]}
        append_ledger({"kind": "review", "session_id": session["session_id"],
                       "model": rv["model"], "seat": rv["seat"],
                       "latency_ms": r["latency_ms"], "ok": r["ok"],
                       "error": r.get("error"), "in_chars": len(user_msg),
                       "out_chars": len(r.get("content", "") or ""),
                       "usage": r.get("usage", {})})
        if notify:
            _push_event(session["session_id"], "progress", {
                "type": "review", "seat": rv["seat"], "model": rv["model"],
                "ok": r["ok"], "has_verdict": bool(rec.get("verdict")),
                "latency_ms": r["latency_ms"],
                "elapsed_ms": round((time.time() - t0) * 1000)})
        if not r["ok"]:
            rec["error"] = r["error"]
            return rec
        try:
            v = parse_verdict(r["content"])
            rec["verdict"] = v
            rec["raw_head"] = r["content"][:120]
        except Exception as e:  # noqa: BLE001
            rec["parse_error"] = f"{type(e).__name__}: {str(e)[:80]}"
            rec["raw_head"] = r["content"][:160]
        return rec

    verdicts = []
    with ThreadPoolExecutor(max_workers=len(REVIEWERS)) as ex:
        futs = {ex.submit(review_one, rv): rv["seat"] for rv in REVIEWERS}
        for f in as_completed(futs):
            rec = f.result()
            verdicts.append(rec)
            oks = sum(1 for v in verdicts if v.get("verdict"))
            # v1.1 P-01：多数票已够 → 记录迟到票后不再等（futures 继续跑完写入 late_review）
            if oks >= MAJORITY:
                session["_majority_at"] = round((time.time() - t0) * 1000)
                break
    # 等剩余 futures 自然结束（不取消，结果留痕 late_review）
    late = []
    for f in futs:
        if not f.done():
            try:
                rec = f.result(timeout=90)
                if rec.get("verdict"):
                    rec["late"] = True
                    late.append(rec)
                    append_ledger({"kind": "late_review", "session_id": session["session_id"],
                                   "seat": rec["seat"], "model": rec["model"],
                                   "latency_ms": rec["latency_ms"]})
            except Exception:  # noqa: BLE001
                pass
    verdicts.extend(late)
    verdicts.sort(key=lambda x: x["seat"])
    session["verdicts"] = verdicts
    session["wall_ms"] = round((time.time() - t0) * 1000)
    session["majority_ms"] = session.get("_majority_at")
    session.pop("_majority_at", None)
    session["consensus"] = build_consensus(verdicts)
    session["status"] = "done"
    append_ledger({"kind": "consensus", "session_id": session["session_id"],
                   "wall_ms": session["wall_ms"], "majority_ms": session.get("majority_ms"),
                   "consensus": session["consensus"].get("category"),
                   "n_ok": sum(1 for v in verdicts if v.get("verdict")),
                   "n_total": len(verdicts)})
    save_session(session)
    if notify:
        _push_event(session["session_id"], "done", {
            "session_id": session["session_id"], "status": "done",
            "consensus": session["consensus"], "wall_ms": session["wall_ms"]})


def build_consensus(verdicts: list) -> dict:
    oks = [v for v in verdicts if v.get("verdict")]
    if not oks:
        return {"category": None, "note": "无有效研判（全部失败），需人工介入"}
    cats = Counter(v["verdict"].get("category", "other") for v in oks)
    top_cat, top_n = cats.most_common(1)[0]
    group = [v for v in oks if v["verdict"].get("category") == top_cat]
    sevs = [v["verdict"].get("severity", "P3") for v in group]
    top_sev = max(sevs, key=lambda s: SEV_ORDER.get(s, 1))
    confs = [float(v["verdict"].get("confidence", 0) or 0) for v in group]
    actions = [v["verdict"].get("action", "") for v in group]
    return {
        "category": top_cat,
        "severity": top_sev,
        "action": Counter(a for a in actions if a).most_common(1)[0][0] if any(actions) else "",
        "votes": f"{top_n}/{len(oks)}",
        "agreement": round(top_n / len(oks), 2),
        "avg_confidence": round(sum(confs) / len(confs), 2) if confs else 0,
        "dissent": [{"seat": v["seat"], "category": v["verdict"].get("category")}
                    for v in oks if v["verdict"].get("category") != top_cat],
        "failed": [{"seat": v["seat"], "model": v["model"],
                    "reason": v.get("error") or v.get("parse_error")}
                   for v in verdicts if not v.get("verdict")],
    }


# ---------- API ----------
class ReviewIn(BaseModel):
    subject: str = Field(min_length=1, max_length=120)
    body: str = Field(min_length=1, max_length=20000)
    customer: str = Field(default="测试客户", max_length=60)


@app.post("/api/reviews")
def create_review(req: ReviewIn, wait: int = 0):
    """wait=1 保留旧行为（同步等完成）；默认异步：立即返回 session_id，后台会审。

    G-07：异步模式解决多用户排队——提交即得 session_id，前端走 SSE/轮询取进度。
    """
    sid = f"SES-{time.strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"
    session = {"session_id": sid, "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
               "ticket": req.model_dump(), "status": "running",
               "verdicts": [], "consensus": None, "feedback": None}
    if wait:
        run_panel(session)
        return session
    save_session(session)  # 先落 running 状态，轮询/SSE 立即可见
    threading.Thread(target=_run_panel_core, args=(session, True),
                     daemon=True, name=f"panel-{sid}").start()
    return {"session_id": sid, "status": "running",
            "events": f"/api/sessions/{sid}/events",
            "poll": f"/api/sessions/{sid}"}


@app.get("/api/sessions")
def list_sessions():
    all_s = load_sessions()
    items = sorted(all_s.values(), key=lambda s: s["ts"], reverse=True)
    return {"count": len(items),
            "sessions": [{k: s.get(k) for k in ("session_id", "ts", "status")}
                         | {"subject": s["ticket"]["subject"]} for s in items]}


@app.get("/api/sessions/{sid}")
def get_session(sid: str):
    s = load_sessions().get(sid)
    if not s:
        raise HTTPException(404, f"会话不存在：{sid}")
    return s


class FeedbackIn(BaseModel):
    rating: int = Field(ge=1, le=5)
    comment: str = Field(default="", max_length=300)


@app.post("/api/sessions/{sid}/feedback")
def give_feedback(sid: str, fb: FeedbackIn):
    """M3.4c：单行读改写——反馈只动目标会话一行，不再全量 load+save（写放大清零）。"""
    conn = _db_connect()
    try:
        _init_db(conn)
        row = conn.execute("SELECT data FROM sessions WHERE session_id=?", (sid,)).fetchone()
        if not row:
            raise HTTPException(404, f"会话不存在：{sid}")
        s = json.loads(row["data"])
        s["feedback"] = {"rating": fb.rating, "comment": fb.comment,
                         "ts": time.strftime("%Y-%m-%dT%H:%M:%S")}
        with conn:
            conn.execute("UPDATE sessions SET data=? WHERE session_id=?",
                         (json.dumps(s, ensure_ascii=False), sid))
    finally:
        conn.close()
    append_ledger({"kind": "feedback", "session_id": sid,
                   "rating": fb.rating, "comment": fb.comment[:100]})
    return {"ok": True, "feedback": s["feedback"]}


@app.get("/api/sessions/{sid}/events")
async def session_events(sid: str):
    """SSE 实时进度：progress 事件逐票推送，done 事件收尾。

    G-12：前端实时看到每位评委的研判结果，替代整段盲等。
    订阅时若会话已完成，直接推 done（避免错过终态）。
    """
    from fastapi.responses import StreamingResponse

    async def gen():
        q: list = []
        with _q_lock:
            _progress_queues.setdefault(sid, []).append(q)
        try:
            all_s = load_sessions()
            sess = all_s.get(sid)
            if sess and sess.get("status") == "done":
                yield f"event: done\ndata: {json.dumps({'session_id': sid, 'status': 'done', 'consensus': sess.get('consensus'), 'wall_ms': sess.get('wall_ms')}, ensure_ascii=False)}\n\n"
                return
            idle = 0
            while idle < 600:  # 最长 ~5 分钟无事件则断开
                if q:
                    event, data = q.pop(0)
                    idle = 0
                    yield f"event: {event}\ndata: {data}\n\n"
                    if event == "done":
                        return
                else:
                    idle += 1
                    yield ": keep-alive\n\n"
                    await asyncio.sleep(0.5)
        finally:
            with _q_lock:
                qs = _progress_queues.get(sid)
                if qs and q in qs:
                    qs.remove(q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@app.get("/api/notifications")
def api_notifications(limit: int = 50):
    return {"count": 0, "items": list_notifications(APP_DIR / "data", limit=limit)}


@app.get("/api/notify-config")
def api_notify_config():
    cfg = load_config(APP_DIR / "data")
    return {"enabled": cfg.get("enabled", False),
            "webhook_url": ("已配置" if cfg.get("webhook_url") else "未配置")}


class NotifyTestIn(BaseModel):
    message: str = Field(default="通知链路测试", max_length=200)


@app.post("/api/notify-test")
def api_notify_test(req: NotifyTestIn):
    """M2.1 验证入口：发一条测试通知（落盘+webhook 按配置）。"""
    r = notify("triage", req.message, level="test", base_dir=APP_DIR / "data")
    return r


@app.get("/api/gateway-metrics")
def api_gateway_metrics():
    """M2.3：代理网关 /v1/metrics（浏览器直连 8687 会跨域）。失败返回 offline。"""
    try:
        with urllib.request.urlopen("http://127.0.0.1:8687/v1/metrics", timeout=3) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        return {"offline": True, "error": f"{type(e).__name__}: {str(e)[:80]}"}


@app.get("/api/usage-board")
def usage_board():
    """G-14 成本看板数据端点：只读 usage_daily.json，按日+按模型聚合。"""
    usage_file = APP_DIR.parents[1] / "data" / "usage_daily.json"
    days = []
    if usage_file.exists():
        try:
            raw = json.loads(usage_file.read_text(encoding="utf-8"))
            for date in sorted(raw.keys()):
                d = raw[date]
                days.append({"date": date, "calls": d.get("calls", 0),
                             "success": d.get("success", 0),
                             "latency_ms_sum": d.get("latency_ms_sum", 0),
                             "failovers_sum": d.get("failovers_sum", 0),
                             "by_model": d.get("by_model", {})})
        except Exception:  # noqa: BLE001 - 损坏时返回空集（usage_store 自身会告警）
            pass
    total = {"calls": 0, "success": 0, "latency_ms_sum": 0, "failovers_sum": 0,
             "by_model": {}}
    for d in days:
        for k in ("calls", "success", "latency_ms_sum", "failovers_sum"):
            total[k] += d.get(k, 0)
        for m, v in (d.get("by_model") or {}).items():
            t = total["by_model"].setdefault(m, {"calls": 0, "success": 0})
            t["calls"] += v.get("calls", 0)
            t["success"] += v.get("success", 0)
    return {"days": days, "total": total}


@app.get("/api/reviewer_health")
def reviewer_health():
    return load_health()


@app.get("/api/ledger")
def query_ledger(limit: int = 50, session: str = ""):
    """perf 优化（v1.3.1）：无 session 过滤时只解析尾部 limit 行，避免全量 loads 随台账增长线性变慢。

    count 语义保持"总条目数"（行数计数，无需解析 JSON）；session 过滤仍需全量（现有调用方都是小 limit）。
    """
    if not LEDGER.exists():
        return {"count": 0, "entries": []}
    lines = LEDGER.read_text(encoding="utf-8").strip().splitlines()
    live = [ln for ln in lines if ln.strip()]
    if session:
        entries = []
        for ln in live:
            try:
                e = json.loads(ln)
            except Exception:  # noqa: BLE001 - 坏行跳过
                continue
            if e.get("session_id") == session:
                entries.append(e)
        return {"count": len(entries), "entries": entries[-limit:]}
    tail = live[-limit:]
    entries = []
    for ln in tail:
        try:
            entries.append(json.loads(ln))
        except Exception:  # noqa: BLE001
            pass
    return {"count": len(live), "entries": entries}


@app.get("/api/health")
def health():
    led_n = 0
    if LEDGER.exists():
        led_n = len(LEDGER.read_text(encoding="utf-8").strip().splitlines())
    return {"status": "ok", "service": "triage-panel", "version": "1.3.1",
            "reviewers": len(REVIEWERS), "ledger_entries": led_n,
            "sessions": len(load_sessions())}


@app.get("/")
def index():
    return FileResponse(APP_DIR / "static" / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/static/{name}")
def static_file_cached(name: str):
    f = APP_DIR / "static" / name
    if not f.exists() or ".." in name:
        from fastapi import HTTPException
        raise HTTPException(404, "not found")
    return FileResponse(f, headers={"Cache-Control": "public, max-age=300"})


@app.get("/static/{name}")
def static_file(name: str):
    f = APP_DIR / "static" / name
    if not f.exists() or ".." in name:
        from fastapi import HTTPException
        raise HTTPException(404, "not found")
    return FileResponse(f)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8793)
