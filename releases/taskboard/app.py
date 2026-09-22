"""任务分配与追踪系统（真实任务分配机制 · demo→可复用）。

FastAPI + JSON 落盘台账（原子写 + 文件锁 + 全量 history 留痕 + undo 回滚）。
端口 8792。设计沿用龙虾站已验证模式，新增：撤销（undo）、负载看板、确认流。

端点：
  GET  /                     看板页（负载视图 + 截止日排序 + 逾期高亮）
  GET  /rules /handoff /exceptions  规则 / 交接 / 异常手册（Markdown）
  GET  /api/health           健康检查
  GET  /api/roster           花名册（角色定义）
  POST /api/tasks            创建任务（含指派记录）
  GET  /api/tasks            台账查询（?status=&assignee=）
  GET  /api/board            负载聚合（按负责人统计 + 逾期清单 + 截止日排序）
  PATCH /api/tasks/{id}      更新状态/改派（history 留痕）
  POST /api/tasks/{id}/undo  撤销最近一次变更（回滚至 before 快照）
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
TASKS = DATA / "tasks.json"
STATIC = ROOT / "static"
DOCS = ROOT / "docs"

_LOCK = threading.Lock()
app = FastAPI(title="Task Assignment Ledger", version="1.0.0")


@app.middleware("http")
async def require_token(request: Request, call_next):
    """鉴权：设了 TASKBOARD_TOKEN 后，除 /api/health、页面与文档 GET 外均需 X-API-Key。"""
    token = (os.getenv("TASKBOARD_TOKEN") or "").strip()
    if token:
        path = request.url.path
        open_paths = ("/api/health", "/", "/rules", "/exceptions", "/handoff")
        is_read = request.method in ("GET", "HEAD") and not path.startswith("/api/tasks") and not path.startswith("/api/board")
        if path not in open_paths and not is_read:
            from fastapi.responses import JSONResponse
            if (request.headers.get("X-API-Key") or "").strip() != token:
                return JSONResponse(status_code=401, content={"detail": "需要 X-API-Key 令牌"})
    return await call_next(request)

PRIORITIES = {"P1", "P2", "P3"}
STATUSES = {"pending", "in_progress", "review", "blocked", "done", "cancelled"}
STATUS_LABEL = {
    "pending": "待处理", "in_progress": "进行中", "review": "待复核",
    "blocked": "阻塞", "done": "已完成", "cancelled": "已取消",
}
# 花名册：执行角色（无虚构真人；"待定负责人"指派需用户确认）
ROSTER = [
    {"name": "用户", "role": "最终确认人", "note": "指派冲突与 P1 任务裁决；确认类任务的第一负责人"},
    {"name": "Agent-AutoClaw", "role": "执行代理", "note": "可立即执行的自动化/工程任务"},
    {"name": "待定负责人", "role": "占位角色", "note": "指派到此角色 = 需用户先确认真人"},
]
VALID_ASSIGNEES = {r["name"] for r in ROSTER}
PHONE_NONE = None  # 台账无个人隐私字段


def _load() -> dict:
    if not TASKS.exists():
        return {"version": 1, "seq": 0, "tasks": []}
    try:
        data = json.loads(TASKS.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=500, detail=f"台账文件损坏：{e}（data/backups/ 有备份，见异常手册）")
    return data


def _save(data: dict) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = TASKS.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, TASKS)


class TaskIn(BaseModel):
    title: str = Field(min_length=2, max_length=80)
    desc: str = Field(default="", max_length=500)
    assignee: str
    priority: str = "P2"
    due: str = ""  # YYYY-MM-DD；空=按优先级自动推算
    acceptance: str = Field(default="", max_length=300)
    source: str = Field(default="", max_length=300)  # 任务来源（文档/会议/口头），保证可溯
    created_by: str = "Agent-AutoClaw"


class TaskPatch(BaseModel):
    status: str | None = None
    assignee: str | None = None
    due: str | None = None
    priority: str | None = None
    title: str | None = Field(default=None, min_length=2, max_length=80)
    desc: str | None = Field(default=None, max_length=500)
    note: str = Field(default="", max_length=300)
    actor: str = "Agent-AutoClaw"


def _default_due(priority: str) -> str:
    days = {"P1": 3, "P2": 7, "P3": 21}.get(priority, 7)
    return (date.today() + timedelta(days=days)).isoformat()


def _snap(task: dict) -> dict:
    return {k: task.get(k) for k in ("title", "desc", "assignee", "priority", "due", "status", "acceptance")}


@app.middleware("http")
async def fix_latin1_body(request: Request, call_next):
    """D-L1 同款容错（继承龙虾站经验）：无 charset 客户端的中文 mojibake 无损还原。"""
    if request.method in ("POST", "PATCH") and "application/json" in (request.headers.get("content-type") or ""):
        body = await request.body()
        if body:
            try:
                obj = json.loads(body.decode("utf-8"))
            except Exception:  # noqa: BLE001
                obj = None
            if isinstance(obj, dict):
                changed = False
                for k, v in list(obj.items()):
                    if isinstance(v, str) and v and any(ch in v for ch in ("æ", "ç", "å", "ã", "è")):
                        try:
                            obj[k] = v.encode("latin-1").decode("utf-8")
                            changed = True
                        except (UnicodeEncodeError, UnicodeDecodeError):
                            pass
                if changed:
                    request._body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    return await call_next(request)


@app.get("/api/health")
def health():
    return {"status": "ok", "service": "task-ledger", "time": time.time()}


@app.get("/api/roster")
def roster():
    return {"roster": ROSTER}


@app.post("/api/tasks")
def create_task(t: TaskIn):
    if t.assignee not in VALID_ASSIGNEES:
        raise HTTPException(status_code=422, detail=f"无效负责人 {t.assignee!r}，花名册：{sorted(VALID_ASSIGNEES)}")
    if t.priority not in PRIORITIES:
        raise HTTPException(status_code=422, detail=f"无效优先级 {t.priority!r}，可选：{sorted(PRIORITIES)}")
    if t.assignee == "待定负责人":
        raise HTTPException(
            status_code=422,
            detail="「待定负责人」是占位角色：指派前请用户确认真人（见分配规则 §3）",
        )
    due = t.due.strip() or _default_due(t.priority)
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", due):
        raise HTTPException(status_code=422, detail="截止日期格式应为 YYYY-MM-DD")
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    with _LOCK:
        data = _load()
        data["seq"] = int(data.get("seq", 0)) + 1
        oid = f"TASK-{time.strftime('%Y%m%d')}-{data['seq']:03d}"
        rec = {
            "task_id": oid,
            "created_at": now,
            "created_by": t.created_by,
            "title": t.title,
            "desc": t.desc,
            "assignee": t.assignee,
            "priority": t.priority,
            "due": due,
            "status": "pending",
            "acceptance": t.acceptance,
            "source": t.source,
            "history": [{
                "ts": now, "action": "创建并指派", "actor": t.created_by,
                "detail": f"指派给 {t.assignee}（{t.priority}，截止 {due}）",
                "before": None,
            }],
        }
        data["tasks"].append(rec)
        _save(data)
    return {"ok": True, "task_id": oid, "assignee": t.assignee, "due": due, "status": "pending"}


@app.get("/api/tasks")
def list_tasks(status: str | None = None, assignee: str | None = None, limit: int = 500, offset: int = 0):
    """分页台账：limit/offset（交接文档 §5 的 500 条边界已按 TASK-006 前置处理）。"""
    with _LOCK:
        data = _load()
    rows = data["tasks"]
    if status:
        if status not in STATUSES:
            raise HTTPException(status_code=422, detail=f"无效状态 {status!r}")
        rows = [r for r in rows if r["status"] == status]
    if assignee:
        rows = [r for r in rows if r["assignee"] == assignee]
    total = len(rows)
    rows = list(reversed(rows))[max(0, offset): max(0, offset) + max(1, min(500, limit))]
    return {"count": total, "returned": len(rows), "offset": offset, "limit": limit,
            "tasks": rows, "status_labels": STATUS_LABEL}


@app.get("/api/tasks/{oid}")
def get_task(oid: str):
    """单任务查询（含完整 history），避免全量拉取。"""
    with _LOCK:
        data = _load()
    rec = next((r for r in data["tasks"] if r["task_id"] == oid), None)
    if rec is None:
        raise HTTPException(status_code=404, detail=f"任务不存在：{oid}")
    return rec


@app.get("/api/board")
def board():
    """负载聚合：按负责人统计 + 逾期清单 + 截止日排序。"""
    with _LOCK:
        data = _load()
    today = date.today().isoformat()
    by_assignee: dict[str, dict] = {}
    overdue = []
    for r in data["tasks"]:
        a = r["assignee"]
        agg = by_assignee.setdefault(a, {"total": 0, "open": 0, "done": 0, "overdue": 0,
                                         "by_priority": {"P1": 0, "P2": 0, "P3": 0}})
        agg["total"] += 1
        if r["status"] in ("done", "cancelled"):
            agg["done"] += 1
        else:
            agg["open"] += 1
            agg["by_priority"][r["priority"]] = agg["by_priority"].get(r["priority"], 0) + 1
            if r["due"] < today:
                overdue.append({"task_id": r["task_id"], "title": r["title"], "assignee": a,
                                "due": r["due"], "status": r["status"]})
                agg["overdue"] += 1
    open_tasks = sorted(
        (r for r in data["tasks"] if r["status"] not in ("done", "cancelled")),
        key=lambda r: (r["due"], {"P1": 0, "P2": 1, "P3": 2}.get(r["priority"], 3)),
    )
    return {
        "today": today,
        "by_assignee": by_assignee,
        "overdue": overdue,
        "due_sorted": [{"task_id": r["task_id"], "title": r["title"], "assignee": r["assignee"],
                        "priority": r["priority"], "due": r["due"], "status": r["status"]}
                       for r in open_tasks],
        "status_labels": STATUS_LABEL,
    }


@app.patch("/api/tasks/{oid}")
def patch_task(oid: str, p: TaskPatch):
    if p.status is not None and p.status not in STATUSES:
        raise HTTPException(status_code=422, detail=f"无效状态 {p.status!r}，可选：{sorted(STATUSES)}")
    if p.assignee is not None and p.assignee not in VALID_ASSIGNEES:
        raise HTTPException(status_code=422, detail=f"无效负责人 {p.assignee!r}，花名册：{sorted(VALID_ASSIGNEES)}")
    if p.priority is not None and p.priority not in PRIORITIES:
        raise HTTPException(status_code=422, detail=f"无效优先级 {p.priority!r}")
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    changes = []
    if p.status is not None:
        changes.append(f"状态→{STATUS_LABEL[p.status]}")
    if p.assignee is not None:
        changes.append(f"改派→{p.assignee}")
    if p.due is not None:
        changes.append(f"截止→{p.due}")
    if p.priority is not None:
        changes.append(f"优先级→{p.priority}")
    if p.title is not None:
        changes.append("标题更新")
    if p.desc is not None:
        changes.append("描述更新")
    if not changes:
        raise HTTPException(status_code=422, detail="未提供任何变更字段")
    with _LOCK:
        data = _load()
        rec = next((r for r in data["tasks"] if r["task_id"] == oid), None)
        if rec is None:
            raise HTTPException(status_code=404, detail=f"任务不存在：{oid}")
        rec["history"].append({
            "ts": now, "action": "；".join(changes), "actor": p.actor,
            "detail": p.note, "before": _snap(rec),
        })
        if p.status is not None:
            rec["status"] = p.status
        if p.assignee is not None:
            rec["assignee"] = p.assignee
        if p.due is not None:
            rec["due"] = p.due
        if p.priority is not None:
            rec["priority"] = p.priority
        if p.title is not None:
            rec["title"] = p.title
        if p.desc is not None:
            rec["desc"] = p.desc
        _save(data)
    return {"ok": True, "task_id": oid, "changes": "；".join(changes), "status": rec["status"], "assignee": rec["assignee"]}


@app.post("/api/tasks/{oid}/undo")
def undo_task(oid: str, actor: str = "Agent-AutoClaw"):
    """撤销最近一次变更：恢复到该次变更前快照，并追加撤销记录（台账保留撤销痕迹）。"""
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    with _LOCK:
        data = _load()
        rec = next((r for r in data["tasks"] if r["task_id"] == oid), None)
        if rec is None:
            raise HTTPException(status_code=404, detail=f"任务不存在：{oid}")
        target = None
        for h in reversed(rec["history"]):
            if h.get("before") is not None and h["action"] != "撤销":
                target = h
                break
        if target is None:
            raise HTTPException(status_code=422, detail="没有可撤销的变更（所有历史均无快照）")
        before = dict(target["before"])
        rec["history"].append({
            "ts": now, "action": "撤销", "actor": actor,
            "detail": f"回滚「{target['action']}」（{target['ts']}）；恢复 assignee={before.get('assignee')} status={before.get('status')}",
            "before": _snap(rec),
        })
        rec.update(before)
        _save(data)
    return {"ok": True, "task_id": oid, "restored_to": target["ts"],
            "status": rec["status"], "assignee": rec["assignee"]}


@app.get("/")
def board_page():
    return FileResponse(STATIC / "board.html", media_type="text/html")


@app.get("/rules")
def rules():
    return FileResponse(DOCS / "rules.md", media_type="text/markdown")


@app.get("/exceptions")
def exceptions():
    return FileResponse(DOCS / "exception-rollback.md", media_type="text/markdown")


@app.get("/handoff")
def handoff():
    return FileResponse(DOCS / "handoff.md", media_type="text/markdown")


if __name__ == "__main__":
    from bootstrap import ensure_utf8_stdio

    ensure_utf8_stdio()
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8792)
    args = ap.parse_args()
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="info")
