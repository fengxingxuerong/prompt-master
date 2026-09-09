"""
FastAPI 服务层：将 PromptMaster 优化闭环包装为 REST API。

端点：
- POST /api/optimize    → 提交单个优化任务，异步执行
- GET  /api/status/{rid} → 查询任务状态（实时从 checkpoint 读取）
- GET  /api/report/{rid} → 获取交付报告（Markdown）
- POST /api/race        → 批量赛马：多任务并发执行
- GET  /api/race/{rid}  → 赛马状态
- GET  /api/race/{rid}/report → 赛马对比报告
- GET  /api/health      → 健康检查

启动：
    python run_server.py
    python run_server.py --port 8080 --workers 2
"""

from __future__ import annotations

import logging
import os
import uuid
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from .scheduler import TaskManager
from .web import WEB_CONSOLE_HTML

logger = logging.getLogger("pm.server")

# --------------------------------------------------------------------------
# 全局调度器单例（注意：它是**进内**的。多 worker 部署时任务只存在于接到 POST
# 的那个进程里，其他 worker 查不到 → 默认单 worker，详见 run_server.py）
# --------------------------------------------------------------------------
_scheduler = TaskManager()

# --------------------------------------------------------------------------
# FastAPI 应用
# --------------------------------------------------------------------------
app = FastAPI(
    title="PromptMaster API",
    description="提示词自动生成 / 测试 / 评估 / 迭代优化 — REST API",
    version="2.1.0",
)

# CORS：默认**不开**。这个服务会烧 API 余额，旧版 `allow_origins=["*"]` 等于允许任意网页
# 跳板提交任务（H3）。确实需要跳源调用时，用 PM_ALLOW_ORIGINS="https://a,https://b" 显式开启。
_origins = [o.strip() for o in os.getenv("PM_ALLOW_ORIGINS", "").split(",") if o.strip()]
if _origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_origins,
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )
else:
    logger.info("未配置 PM_ALLOW_ORIGINS，已关闭跳源访问")


def require_token(x_api_key: str | None = Header(default=None)) -> None:
    """可选的静态 token 鉴权：设了 `PM_API_TOKEN` 就强制校验。

    服务默认能启动就能烧钱，没有任何访问控制是不安全的（H3）。
    未设 `PM_API_TOKEN` 时保持旧行为（本地开发不碍事），但会记一条告警。
    """
    expected = os.getenv("PM_API_TOKEN", "").strip()
    if not expected:
        return
    if x_api_key != expected:
        raise HTTPException(status_code=401, detail="缺少或错误的 X-API-Key")


if not os.getenv("PM_API_TOKEN", "").strip():
    logger.warning(
        "未设置 PM_API_TOKEN：任何能访问本端口的请求都可以提交消耗 API 额度的任务。"
        "如果要放到共享网络，请设 token 并只监听 127.0.0.1"
    )


# --------------------------------------------------------------------------
# 请求 / 响应模型
# --------------------------------------------------------------------------
class CaseInput(BaseModel):
    """用户提供的测试用例（可选）：提供后跳过 mockgen，expected 参与事实断言。"""

    input: str = Field(min_length=1, max_length=8000, description="测试输入")
    expected: str = Field(default="", max_length=8000, description="期望输出（ground-truth，可空）")


class OptimizeRequest(BaseModel):
    # 长度上限是成本护栏：单任务最多 n_cases ×(1+评委数)×(max_iter+1) 次调用，
    # 而入参无上限时没人拦得住一次 200k 字的 task（H3）
    task: str = Field(min_length=4, max_length=8000, description="原始需求描述（4-8000 字）")
    context: str = Field(default="", max_length=8000, description="补充上下文")
    target_model: str = Field(
        default="未指定", max_length=200, description="提示词最终运行的目标模型"
    )
    n_test_cases: int = Field(default=3, ge=1, le=8, description="测试用例数量")
    max_iterations: int = Field(default=3, ge=1, le=10, description="最大修订轮次")
    test_cases: list[CaseInput] | None = Field(
        default=None,
        max_length=8,
        description="自定义测试集（可选）：提供后跳过用例生成，expected 参与事实断言",
    )
    assertion_mode: str = Field(
        default="",
        max_length=40,
        pattern=r"^(|exact|contains|regex|custom:[a-zA-Z][a-zA-Z0-9_]*)$",
        description="事实断言模式；空 = contains",
    )


class OptimizeResponse(BaseModel):
    run_id: str
    message: str


class StatusResponse(BaseModel):
    run_id: str
    status: str
    iteration: int = 0
    aggregate: dict[str, Any] | None = None
    prompt_versions: int = 0
    llm_calls: int = 0
    error: str | None = None  # 任务异常终止时给出来因，否则调用方只能看到 status=failed


class ReportResponse(BaseModel):
    run_id: str
    report: str


class RaceRequest(BaseModel):
    name: str = Field(default="赛马", max_length=100, description="赛马名称")
    tasks: list[OptimizeRequest] = Field(description="参赛任务列表", min_length=2, max_length=10)


class RaceResponse(BaseModel):
    race_id: str
    message: str


class RaceStatusResponse(BaseModel):
    race_id: str
    name: str
    total: int
    finished: int
    runs: dict[str, Any]


class RaceReportResponse(BaseModel):
    race_id: str
    report: str


# --------------------------------------------------------------------------
# 路由
# --------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def web_console():
    """Web 控制台（单 HTML 页面）。"""
    return WEB_CONSOLE_HTML


@app.get("/api/health")
async def health():
    return {"status": "ok", "service": "prompt-master"}


@app.post("/api/optimize", response_model=OptimizeResponse, dependencies=[Depends(require_token)])
async def optimize(req: OptimizeRequest):
    run_id = uuid.uuid4().hex[:12]
    seeds = [c.model_dump() for c in req.test_cases] if req.test_cases else None
    if seeds:
        # 用例数以用户提供为准：断言按序号与 expected 对齐，声明数不一致会搅乱达标口径
        n_cases = len(seeds)
    else:
        n_cases = req.n_test_cases
    _scheduler.submit(
        run_id,
        task=req.task,
        context=req.context,
        target_model=req.target_model,
        n_test_cases=n_cases,
        max_iterations=req.max_iterations,
        test_cases=seeds,
        assertion_mode=req.assertion_mode,
    )
    return OptimizeResponse(
        run_id=run_id, message=f"任务已提交，查询状态：GET /api/status/{run_id}"
    )


@app.get("/api/status/{run_id}", response_model=StatusResponse)
async def status(run_id: str):
    s = _scheduler.get_status(run_id)
    if s is None:
        raise HTTPException(status_code=404, detail=f"run_id {run_id} 不存在")
    return StatusResponse(**s)


@app.get("/api/report/{run_id}", response_model=ReportResponse)
async def report(run_id: str):
    rpt = _scheduler.get_report(run_id)
    if rpt is None:
        raise HTTPException(status_code=404, detail=f"run_id {run_id} 不存在或报告未生成")
    return ReportResponse(run_id=run_id, report=rpt)


@app.post("/api/race", response_model=RaceResponse, dependencies=[Depends(require_token)])
async def race(req: RaceRequest):
    specs = [
        {
            "task": t.task,
            "context": t.context,
            "target_model": t.target_model,
            "n_test_cases": len(t.test_cases) if t.test_cases else t.n_test_cases,
            "max_iterations": t.max_iterations,
            "test_cases": [c.model_dump() for c in t.test_cases] if t.test_cases else None,
            "assertion_mode": t.assertion_mode,
        }
        for t in req.tasks
    ]
    race_id = _scheduler.submit_race(req.name, specs)
    return RaceResponse(race_id=race_id, message=f"赛马已启动，状态：GET /api/race/{race_id}")


@app.get("/api/race/{race_id}", response_model=RaceStatusResponse)
async def race_status(race_id: str):
    s = _scheduler.get_race_status(race_id)
    if s is None:
        raise HTTPException(status_code=404, detail=f"race_id {race_id} 不存在")
    return RaceStatusResponse(**s)


@app.get("/api/race/{race_id}/report", response_model=RaceReportResponse)
async def race_report(race_id: str):
    rpt = _scheduler.get_race_report(race_id)
    if rpt is None:
        raise HTTPException(status_code=404, detail=f"race_id {race_id} 不存在或报告未生成")
    return RaceReportResponse(race_id=race_id, report=rpt)
