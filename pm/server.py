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

import asyncio
import json
import logging
import os
import secrets
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any, cast

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from . import __version__ as PKG_VERSION
from .ratelimit import SlidingWindowLimiter, parse_rate_limit_env
from .scheduler import TaskManager
from .state import SEED_PROMPT_MAX_CHARS
from .store import store_from_env
from .web import WEB_CONSOLE_HTML

logger = logging.getLogger("pm.server")

# --------------------------------------------------------------------------
# 全局调度器单例
# --------------------------------------------------------------------------
# 记录存哪里由 PM_TASK_DB 决定（见 pm/store.py）：
# - 不设 → 进程内记录，此时只能单 worker（多 worker 会让 status/report 随机 404）；
# - 设了 → SQLite 共享记录，各 worker 读同一份，可放心 --workers > 1。
# 注意线程池仍是进程内的：任务由接到 POST 的那个 worker 执行，查询可以打给任意 worker。
_scheduler = TaskManager(store=store_from_env())

# --------------------------------------------------------------------------
# 评委校准排程（可选）：PM_CALIBRATE_HOURS=N 时，server 常驻期间每 N 小时在后台
# 回测一次锚点集（subprocess 调 run.py calibrate --json——与 CLI/MCP 单一口径），
# 漂移超阈直接打 WARNING。"评委可信吗"从一次性人工动作升级为持续监控。
# 默认 0=关闭：校准是真实计费调用，花钱的事必须显式开启。
# --------------------------------------------------------------------------
import threading  # noqa: E402
import time as _time  # noqa: E402

_CALIB_ROOT = Path(__file__).resolve().parent.parent


def _scheduled_calibrate_once(judge: str = "evaluator") -> dict[str, Any] | None:
    """后台跑一次锚点校准；返回解析后的 JSON（含 drift），失败返回 None。"""
    proc = subprocess.run(
        [sys.executable, str(_CALIB_ROOT / "run.py"), "calibrate", "--judge", judge, "--json"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=900,
        stdin=subprocess.DEVNULL,
        cwd=str(_CALIB_ROOT),
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    try:
        return cast("dict[str, Any] | None", json.loads(proc.stdout))
    except json.JSONDecodeError:
        logger.warning("定期校准输出异常（exit=%s）：%s", proc.returncode, proc.stderr[-200:])
        return None


def _calibrate_patrol_loop(interval_hours: float, judge: str) -> None:
    while True:
        try:
            result = _scheduled_calibrate_once(judge)
            drift = (result or {}).get("drift")
            analysis = (result or {}).get("analysis") or {}
            bias_now = analysis.get("bias")
            mae_now = analysis.get("mae")
            if result is None:
                pass  # _scheduled_calibrate_once 内部已告警
            elif drift and drift.get("drifted"):
                logger.warning(
                    "评委漂移告警（%s）：bias %s → %s（Δ%+.2f），mae %s → %s（Δ%+.2f）——"
                    "评估结论的可信度正在变化，建议人工复核锚点样本",
                    judge,
                    drift["prev_bias"],
                    bias_now,
                    drift["delta_bias"],
                    drift["prev_mae"],
                    mae_now,
                    drift["delta_mae"],
                )
            elif drift:
                logger.info(
                    "定期校准稳定（%s）：bias Δ%+.2f / mae Δ%+.2f",
                    judge,
                    drift["delta_bias"],
                    drift["delta_mae"],
                )
            else:
                logger.info("定期校准完成（%s）：首次建账，下次起可对比漂移", judge)
        except Exception as e:  # noqa: BLE001 - 排程失败绝不能影响服务
            logger.warning("定期校准失败（不影响服务）：%s", e)
        _time.sleep(interval_hours * 3600)


def _maybe_start_calibration_patrol() -> None:
    raw = (os.getenv("PM_CALIBRATE_HOURS") or "").strip()
    if not raw:
        return
    try:
        hours = max(0.1, float(raw))
    except ValueError:
        logger.warning("PM_CALIBRATE_HOURS=%r 不是数字，校准排程忽略", raw)
        return
    judge = (os.getenv("PM_CALIBRATE_JUDGE") or "evaluator").strip() or "evaluator"
    threading.Thread(
        target=_calibrate_patrol_loop,
        args=(hours, judge),
        daemon=True,
        name="judge-calibration-patrol",
    ).start()
    logger.info("评委校准排程已启动：每 %.1f 小时回测锚点集（%s）", hours, judge)


_maybe_start_calibration_patrol()

# --------------------------------------------------------------------------
# FastAPI 应用
# --------------------------------------------------------------------------
app = FastAPI(
    title="PromptMaster API",
    description="提示词自动生成 / 测试 / 评估 / 迭代优化 — REST API",
    # 版本只有一个事实源（pm.__version__）；这里再写一个字面量就会和 pyproject 各说各话
    version=PKG_VERSION,
)


# --------------------------------------------------------------------------
# 停机排空（M3.5 stderr 噪音治理，2026-09-24）
# --------------------------------------------------------------------------
# 此前进程退出时任务线程池没有排空钩子：排队任务被解释器强杀、在跑任务的异常
# 无人取回（未检索的 Future 异常 → 退出期 "Exception ignored" 类 stderr 噪音），
# 记录也可能停在 pending。现在 uvicorn 收到停机信号后先排空线程池：
# 排队任务取消（记录标 failed），在跑任务等它自然收尾（正常落库/落盘）。
#
# PM_DRAIN_TIMEOUT：排空等待上限（秒，浮点）。不设 = 无限等（与旧版 atexit join
# 语义一致）；设了（如 30）则到点后对卡死任务转入不等待退出。
# 注册走 app.router.on_shutdown 而非 @app.on_event——后者已弃用，其
# DeprecationWarning 本身就是新噪音源（治理噪音不能再制造噪音）。
def _drain_task_pool() -> None:
    raw = os.getenv("PM_DRAIN_TIMEOUT", "").strip()
    drain_timeout: float | None
    try:
        drain_timeout = float(raw) if raw else None
    except ValueError:
        logger.warning("PM_DRAIN_TIMEOUT=%r 不是数字，按无限排空处理", raw)
        drain_timeout = None
    try:
        cancelled = _scheduler.shutdown(wait=True, timeout=drain_timeout)
        if cancelled:
            logger.info("停机排空完成，取消排队任务：%s", ", ".join(cancelled))
    except Exception:
        logger.exception("停机排空线程池失败（不阻塞退出）")


app.router.on_shutdown.append(_drain_task_pool)


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


# --------------------------------------------------------------------------
# 安全响应头
# --------------------------------------------------------------------------
# 控制台是零外部依赖的单页（CSS/JS/canvas 全内联，无任何 CDN 请求），所以能上
# `default-src 'self'`：即便有人往报告里塞了 <img src=外部地址>，外联也会被浏览器掐掉。
#
# `script-src` / `style-src` 用**逐响应 nonce** 而不是 'unsafe-inline'：
# 'unsafe-inline' 一旦打开，注入进来的内联脚本照样能跑，等于把 CSP 降级成"只防外联"。
# 代价是控制台自己也不能用内联事件属性（onclick=...）与 style 属性 —— 那部分已改成
# data-* + 事件委托与工具类（见 pm/web.py），所以这里可以不给 'unsafe-inline'。
CSP_NONCE_PLACEHOLDER = "__CSP_NONCE__"


def render_csp(nonce: str) -> str:
    return (
        "default-src 'self'; "
        f"script-src 'self' 'nonce-{nonce}'; "
        f"style-src 'self' 'nonce-{nonce}'; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "font-src 'self' data:; "
        "object-src 'none'; "
        "base-uri 'none'; "
        "form-action 'self'; "
        "frame-ancestors 'none'"
    )


# Swagger UI / ReDoc 从 CDN 拉静态资源，套 strict CSP 会白屏 —— 这几个路径跳过 CSP，
# 其余安全头照给（它们是 HTML 页面，值得防嵌套与嗅探）。
_CSP_EXEMPT_PATHS = ("/docs", "/redoc", "/openapi.json")


@app.middleware("http")
async def _security_headers(request: Request, call_next: Any) -> Response:
    """给所有响应补基础安全头；控制台页额外上带 nonce 的 CSP。

    nonce 必须在**处理请求之前**生成并放进 request.state，路由渲染 HTML 时才能用同一个值；
    响应头在 call_next 之后补。两者是同一个 nonce，否则控制台自己的脚本会被自家 CSP 拦下。
    """
    nonce = secrets.token_urlsafe(16)
    request.state.csp_nonce = nonce
    resp = await call_next(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    resp.headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
    if not request.url.path.startswith(_CSP_EXEMPT_PATHS):
        resp.headers.setdefault("Content-Security-Policy", render_csp(nonce))
    return resp


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
# 提交端点限流（H3 遗留半边：鉴权挡不住合法身份的高频滥用）
# --------------------------------------------------------------------------
_rate_per_min, _rate_warning = parse_rate_limit_env(os.getenv("PM_RATE_LIMIT_PER_MIN"))
if _rate_warning:
    logger.warning(_rate_warning)
if _rate_per_min == 0:
    logger.warning("PM_RATE_LIMIT_PER_MIN=0：提交端点限流已显式关闭（仅建议本地单人开发使用）")
_limiter = SlidingWindowLimiter(max_events=max(1, _rate_per_min)) if _rate_per_min > 0 else None


async def enforce_rate_limit(request: Request, cost: int = 1) -> None:
    """提交端点的限流检查（在 handler 内显式调用，不是 Depends）。

    为什么不用 Depends：依赖在 handler 之前执行，而 `/api/race` 的 cost
    依赖已解析的请求体（参赛任务数），此时还拿不到 —— 旧写法在 handler 里
    设置 request.state.rate_cost 对依赖来说永远晚一步，赛马会被按 1 次计费。
    在 handler 内调用则两个端点都能拿到真实成本。

    计费口径：key = API token（区分身份）否则客户端 IP；
    `/api/race` 的 cost = 参赛任务数（成本跟任务数走，不是跟请求数走）。
    超额返回 429 + Retry-After，调用方拿到明确等待时间而不是盲目重试。
    """
    if _limiter is None:
        return
    expected = os.getenv("PM_API_TOKEN", "").strip()
    key = expected or (request.client.host if request.client else "unknown")
    cost = max(1, int(cost or 1))
    ok, retry_after = _limiter.acquire(key, cost)
    if not ok:
        logger.warning("限流触发：key=%s cost=%d，%.1fs 后可重试", key, cost, retry_after)
        raise HTTPException(
            status_code=429,
            detail=f"提交过于频繁（每分钟上限 {_rate_per_min} 次提交），请 {max(1, int(retry_after) + 1)} 秒后重试",
            headers={"Retry-After": str(max(1, int(retry_after) + 1))},
        )


# --------------------------------------------------------------------------
# 请求 / 响应模型
# --------------------------------------------------------------------------
class CaseInput(BaseModel):
    """用户提供的测试用例（可选）：提供后跳过 mockgen，expected 参与事实断言。"""

    input: str = Field(min_length=1, max_length=8000, description="测试输入")
    expected: str = Field(default="", max_length=8000, description="期望输出（ground-truth，可空）")
    # 逐条覆盖全局 assertion_mode：一份用例集里混着写字面片段与需求规则才是常态
    assert_mode: str = Field(
        default="",
        max_length=40,
        pattern=r"^(|exact|contains|regex|rule|custom:[a-zA-Z][a-zA-Z0-9_]*)$",
        description="本条的断言模式；空 = 用请求级的 assertion_mode",
    )
    # 注入存活专项（确定性校验，不经评委）的可选标记：与 CLI cases-file 同口径透传
    scenario: str = Field(default="", max_length=40, description='可选场景标记，如 "injection"')
    hijack_marker: str = Field(
        default="",
        max_length=200,
        description=(
            "注入指令点名的短语（确定性劫持检测依据）；"
            "仅对随请求提供的种子用例生效，自动生成的注入用例由系统派生校验码"
        ),
    )


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
        pattern=r"^(|exact|contains|regex|rule|custom:[a-zA-Z][a-zA-Z0-9_]*)$",
        description="事实断言模式；空 = contains，rule = 把 expected 当需求规则交评委核验",
    )
    seed_prompt: str = Field(
        default="",
        max_length=SEED_PROMPT_MAX_CHARS,
        description="待改进的用户原稿提示词。非空时 optimize 节点走改进分支（保留原稿术语、"
        "只做最小改动），且基线臂换成这份原稿——返回的 Δ 读作「比你自己的版本好多少」",
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
    # ---- 富 UI 视图字段（数据本就存在于图状态，旧版未对外暴露）----
    task: str = ""
    target_model: str = ""
    n_test_cases: int = 0
    test_cases: list[str] = []
    prompt_version_list: list[dict[str, Any]] = []
    trace: list[dict[str, Any]] = []
    evaluations: list[dict[str, Any]] = []
    baseline_aggregate: dict[str, Any] | None = None
    pairwise: dict[str, Any] | None = None
    early_stop_reason: str | None = None
    prompt_quality_issues: list[dict[str, Any]] = []
    # 按角色的 token 用量与耗时（{role: {calls, input_tokens, output_tokens, latency_ms}}）
    llm_usage: dict[str, dict[str, int]] = {}


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
async def web_console(request: Request) -> str:
    """Web 控制台（单 HTML 页面）。

    nonce 占位符在渲染时替换成中间件为本请求生成的值；替换后 HTML 与响应头里的
    nonce 必须一致 —— 不一致的表现是控制台完全没反应（脚本被 CSP 拦掉），
    tests/test_web_console.py 有断言比对两者。
    """
    nonce = getattr(request.state, "csp_nonce", "")
    return WEB_CONSOLE_HTML.replace(CSP_NONCE_PLACEHOLDER, nonce)


@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {"status": "ok", "service": "prompt-master"}


@app.get("/api/history")
async def history_endpoint() -> JSONResponse:
    """运行历史聚合 + 同任务 Δ 显著性（Web 历史面板的数据源）。

    subprocess 调 `run.py history --json`：与 CLI/MCP 单一事实来源。
    stdin 必须显式接 DEVNULL——继承 server 的 stdio 会被 run.py 的
    ensure_utf8_stdio 触碰后永久阻塞（MCP 侧踩过同一坑）。
    """
    proc = await asyncio.to_thread(
        subprocess.run,
        [
            sys.executable,
            str(Path(__file__).resolve().parent.parent / "run.py"),
            "history",
            "--last",
            "50",
            "--json",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        stdin=subprocess.DEVNULL,
        cwd=str(Path(__file__).resolve().parent.parent),
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    try:
        return JSONResponse(json.loads(proc.stdout))
    except json.JSONDecodeError as e:
        raise HTTPException(
            status_code=500, detail=f"history 输出异常：{proc.stderr[-300:]}"
        ) from e


@app.post(
    "/api/optimize",
    response_model=OptimizeResponse,
    dependencies=[Depends(require_token)],
)
async def optimize(req: OptimizeRequest, request: Request) -> OptimizeResponse:
    await enforce_rate_limit(request, cost=1)
    run_id = uuid.uuid4().hex[:12]
    seeds = [c.model_dump() for c in req.test_cases] if req.test_cases else None
    # 用例数以用户提供为准：断言按序号与 expected 对齐，声明数不一致会搅乱达标口径
    n_cases = len(seeds) if seeds else req.n_test_cases
    _scheduler.submit(
        run_id,
        task=req.task,
        context=req.context,
        target_model=req.target_model,
        n_test_cases=n_cases,
        max_iterations=req.max_iterations,
        test_cases=seeds,
        assertion_mode=req.assertion_mode,
        seed_prompt=req.seed_prompt.strip(),
    )
    return OptimizeResponse(
        run_id=run_id, message=f"任务已提交，查询状态：GET /api/status/{run_id}"
    )


@app.get("/api/status/{run_id}", response_model=StatusResponse)
async def status(run_id: str) -> StatusResponse:
    s = _scheduler.get_status(run_id)
    if s is None:
        raise HTTPException(status_code=404, detail=f"run_id {run_id} 不存在")
    return StatusResponse(**s)


@app.get("/api/report/{run_id}", response_model=ReportResponse)
async def report(run_id: str) -> ReportResponse:
    rpt = _scheduler.get_report(run_id)
    if rpt is None:
        raise HTTPException(status_code=404, detail=f"run_id {run_id} 不存在或报告未生成")
    return ReportResponse(run_id=run_id, report=rpt)


@app.post("/api/race", response_model=RaceResponse, dependencies=[Depends(require_token)])
async def race(req: RaceRequest, request: Request) -> RaceResponse:
    # 赛马的成本 = 参赛任务数：一次 10 任务提交按 10 次计费，不能让批量入口变成限流旁路
    await enforce_rate_limit(request, cost=len(req.tasks))
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
async def race_status(race_id: str) -> RaceStatusResponse:
    s = _scheduler.get_race_status(race_id)
    if s is None:
        raise HTTPException(status_code=404, detail=f"race_id {race_id} 不存在")
    return RaceStatusResponse(**s)


@app.get("/api/race/{race_id}/report", response_model=RaceReportResponse)
async def race_report(race_id: str) -> RaceReportResponse:
    rpt = _scheduler.get_race_report(race_id)
    if rpt is None:
        raise HTTPException(status_code=404, detail=f"race_id {race_id} 不存在或报告未生成")
    return RaceReportResponse(race_id=race_id, report=rpt)
