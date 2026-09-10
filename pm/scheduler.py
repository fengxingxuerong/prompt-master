"""
后台任务调度器：线程池异步执行优化闭环，管理任务生命周期。

设计要点：
- 复用 run.py 的执行逻辑，但去掉交互终端（API 无 stdin）
- 用 MemorySaver checkpointer（API 场景无需跨进程持久化）
- 演示模式 `PM_FAKE_BACKEND=progress|stall|dispute|unclear`：无 API Key 也能跑通全流程。
  注意这里**不再 monkeypatch 模块属性**，而是用 `pm.testing.scope()`（ContextVar）把假后端
  限定在当前任务内 —— 旧写法的补丁是进程级的：先结束的任务会把补丁摘掉，停止顺序不确定时
  mock 还会永久残留在模块上，此后整个进程（含真实 Key 的请求）都被静默喂假数据（C4）。
- 运行中进度实时可见：用 `app.stream(stream_mode="values")`，每个节点结束就刷新快照（H2）
- 结果落盘 `logs/report_<run_id>.md` + `logs/run_<run_id>.json`，进程重启后报告仍可取回（M2）
- 任务表有上限（`PM_MAX_TASKS`，默认 200），按插入序淘汰，避免长驻进程无界增长（M2）
- 赛马（race）：并发提交多个任务，全部跑完后生成横向对比报告
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import threading
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .graph import build_app
from .llm import usage_scope
from .state import initial_state

logger = logging.getLogger("pm.scheduler")


def _log_dir() -> Path:
    """产物目录（可测性：测试里指到 tmp，不往仓库 logs/ 写东西）。"""
    raw = os.getenv("PM_LOG_DIR", "").strip()
    return Path(raw) if raw else Path(__file__).resolve().parent.parent / "logs"


# 与 pm.graph._TERMINAL / route_after_evaluate 保持一致：这些状态都视为终态
TERMINAL_STATUS = ("passed", "max_iterations", "failed", "early_stopped")


@dataclass
class TaskRecord:
    run_id: str
    status: str = "pending"  # pending | running | finished | failed
    result: dict[str, Any] | None = None
    progress: dict[str, Any] | None = None  # 运行中的实时快照（H2）
    error: str | None = None
    report_path: Path | None = None
    future: Future | None = field(default=None, repr=False)


@dataclass
class RaceRecord:
    race_id: str
    name: str
    run_ids: list[str]
    spec_count: int


def _progress_of(state: dict[str, Any]) -> dict[str, Any]:
    """从图状态里裁出「给外部轮询用」的进度视图。

    旧版只裁了薄薄一层（status/iteration/aggregate/版本数/调用数），够轮询状态用，
    但富 UI 要做节点级流水线、提示词版本/diff、维度雷达图——这些数据本就存在于图状态，
    只是没有对外暴露。这里补齐：不动旧字段（向后兼容旧控制台），只追加富视图字段。
    """
    versions = state.get("prompt_versions", [])
    pv_list = versions if isinstance(versions, list) else []
    out = {
        "run_id": state.get("run_id"),
        "status": state.get("status", "running"),
        "iteration": state.get("iteration", 0),
        "aggregate": state.get("aggregate"),
        # 容忍两种输入：完整图状态（list）与已经裁过的进度快照（int）
        "prompt_versions": len(pv_list) if pv_list else int(versions or 0),
        "llm_calls": state.get("llm_calls", 0),
    }
    # ---- 富 UI 视图字段（数据本就存在于状态，旧版未对外暴露）----
    out["task"] = str(state.get("task", "") or "")
    out["target_model"] = str(state.get("target_model", "") or "")
    out["n_test_cases"] = int(state.get("n_test_cases", 0) or 0)
    out["test_cases"] = [str(c) for c in (state.get("test_cases", []) or [])]
    # 提示词全文版本（供版本切换 + diff）：只取展示所需的叶子字段，不挂整份 state
    out["prompt_version_list"] = [
        {
            "iteration": v.get("iteration"),
            "prompt": str(v.get("prompt", "") or ""),
            "note": str(v.get("note", "") or ""),
            "avg_score": v.get("avg_score"),
            "min_score": v.get("min_score"),
        }
        for v in pv_list
        if isinstance(v, dict)
    ]
    # 节点级 trace（供流水线可视化）
    out["trace"] = [dict(t) for t in (state.get("trace", []) or []) if isinstance(t, dict)]
    # 逐用例评估（供维度雷达图 + 评估表）：只取叶子，丢掉大段 evidence
    out["evaluations"] = [
        {
            "test_case_index": e.get("test_case_index"),
            "weighted_score": e.get("weighted_score"),
            "model_reported_score": e.get("model_reported_score"),
            "dimension_scores": e.get("dimension_scores"),
            "judge": e.get("judge"),
            "passed": e.get("passed"),
            "issues": list(e.get("issues", []) or []),
            "suggestions": list(e.get("suggestions", []) or []),
            "sample_scores": list(e.get("sample_scores", []) or []),
        }
        for e in (state.get("evaluations", []) or [])
        if isinstance(e, dict)
    ]
    out["baseline_aggregate"] = state.get("baseline_aggregate")
    out["pairwise"] = state.get("pairwise")
    out["early_stop_reason"] = state.get("early_stop_reason")
    # 按角色的 token/调用/耗时台账（运行中随节点推进实时刷新）
    out["llm_usage"] = dict(state.get("llm_usage", {}) or {})
    out["prompt_quality_issues"] = [
        dict(q) for q in (state.get("prompt_quality_issues", []) or []) if isinstance(q, dict)
    ]
    return out


def _save_artifacts(run_id: str, state: dict[str, Any]) -> tuple[Path | None, Path | None]:
    """把交付报告与完整状态落盘（CLI 与 API 共用同一份产物约定）。"""
    log_dir = _log_dir()
    try:
        log_dir.mkdir(exist_ok=True)
        report = state.get("final_report") or ""
        report_path = log_dir / f"report_{run_id}.md"
        report_path.write_text(report, encoding="utf-8")
        (log_dir / f"run_{run_id}.json").write_text(
            json.dumps(state, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )
        return report_path, None
    except OSError as e:  # 落盘失败不应让任务判失败
        logger.warning("任务 %s 产物落盘失败（不影响结果）：%s", run_id, e)
        return None, None


class TaskManager:
    """线程池驱动的后台任务调度器。

    线程安全，支持单任务优化与批量赛马；任务表有容量上限。
    """

    def __init__(self, max_workers: int = 4, max_records: int | None = None):
        self._executor = ThreadPoolExecutor(max_workers=max_workers)
        self._tasks: dict[str, TaskRecord] = {}
        self._races: dict[str, RaceRecord] = {}
        self._lock = threading.Lock()
        self._max_records = (
            max_records if max_records is not None else _int_env("PM_MAX_TASKS", 200)
        )

    # ------------------------------------------------------------------
    # 单任务
    # ------------------------------------------------------------------
    def submit(self, run_id: str, **kwargs: Any) -> str:
        """提交单个优化任务，异步执行。返回 run_id。"""
        with self._lock:
            if run_id in self._tasks:
                raise ValueError(f"run_id {run_id} 已存在")
            rec = TaskRecord(run_id=run_id, status="pending")
            self._tasks[run_id] = rec
            self._evict_locked()

        future = self._executor.submit(self._run_task, run_id, kwargs)
        with self._lock:
            rec.future = future
        return run_id

    def get_status(self, run_id: str) -> dict[str, Any] | None:
        """查询任务状态。运行中读实时快照，结束后读最终状态（H2）。"""
        with self._lock:
            rec = self._tasks.get(run_id)
        if rec is None:
            return None
        if rec.result:
            out = _progress_of(rec.result)
            # 任务本身崩了（未走到 report 节点）时，用调度器状态兜底
            if out.get("status") in (None, "", "running") and rec.status == "failed":
                out["status"] = rec.status
            out["error"] = rec.error
            return out
        if rec.progress:
            # 进度快照已经是裁好的视图，不能再过一道 _progress_of：
            # 那里的 len(prompt_versions) 拿到的是 int，第二次轮询就会 TypeError
            out = dict(rec.progress)
            out.setdefault("error", rec.error)
            return out
        return {"run_id": run_id, "status": rec.status, "error": rec.error}

    def get_report(self, run_id: str) -> str | None:
        """取交付报告：内存 → 落盘文件。都没有则 None（调用方按 404 处理）。"""
        with self._lock:
            rec = self._tasks.get(run_id)
        if rec is not None:
            report = (rec.result or {}).get("final_report") if rec.result else None
            if report:
                return report
            if rec.report_path and rec.report_path.exists():
                return rec.report_path.read_text(encoding="utf-8")
        # 进程重启 / 记录被淘汰后，仍可从 logs/ 找回
        fallback = _log_dir() / f"report_{run_id}.md"
        if fallback.exists():
            return fallback.read_text(encoding="utf-8")
        return None

    # ------------------------------------------------------------------
    # 批量赛马
    # ------------------------------------------------------------------
    def submit_race(self, race_name: str, specs: list[dict[str, Any]]) -> str:
        """提交多个优化任务并发执行，全部跑完后生成横向对比报告。返回 race_id。"""
        race_id = uuid.uuid4().hex[:8]
        run_ids: list[str] = []
        prepared: list[tuple[str, dict[str, Any]]] = []
        for raw in specs:
            spec = dict(raw)  # 不就地改调用方传入的对象（旧版 spec.pop 会污染入参）
            rid = str(spec.pop("run_id", None) or uuid.uuid4().hex[:12])
            prepared.append((rid, spec))
            run_ids.append(rid)
        # 先把名单登记齐，再提交：否则并发查询会看到 total 与 runs 数目不一致
        with self._lock:
            self._races[race_id] = RaceRecord(
                race_id=race_id, name=race_name, run_ids=run_ids, spec_count=len(run_ids)
            )
            self._evict_locked()
        for rid, spec in prepared:
            self.submit(rid, **spec)
        return race_id

    def get_race_status(self, race_id: str) -> dict[str, Any] | None:
        with self._lock:
            race = self._races.get(race_id)
        if race is None:
            return None
        statuses: dict[str, Any] = {}
        for rid in list(race.run_ids):
            s = self.get_status(rid)
            if s:
                statuses[rid] = s
        n_finished = sum(1 for s in statuses.values() if s.get("status") in TERMINAL_STATUS)
        return {
            "race_id": race_id,
            "name": race.name,
            "total": race.spec_count,
            "finished": n_finished,
            "runs": statuses,
        }

    def get_race_report(self, race_id: str) -> str | None:
        with self._lock:
            race = self._races.get(race_id)
        if race is None:
            return None
        ids = list(race.run_ids)
        reports = {rid: rpt for rid in ids if (rpt := self.get_report(rid))}
        statuses = self.get_race_status(race_id) or {}
        return _build_race_report(race, statuses.get("runs", {}), reports)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _evict_locked(self) -> None:
        """按插入序淘汰最旧记录（必须在持有 self._lock 时调用）。

        每个 result 都持有完整图状态（全部 prompt 版本 + trace），无上限保留等于内存泄漏（M2）。
        """
        limit = max(1, self._max_records)
        for table in (self._tasks, self._races):
            while len(table) > limit:
                table.pop(next(iter(table)), None)

    def _run_task(self, run_id: str, kwargs: dict[str, Any]) -> None:
        with self._lock:
            rec = self._tasks.get(run_id)
            if rec:
                rec.status = "running"

        # 演示模式：把假后端限定在本次任务的作用域内（ContextVar），不改动任何模块属性（C4）
        scenario = os.getenv("PM_FAKE_BACKEND", "")
        hook_cm: Any = contextlib.nullcontext()
        if scenario:
            from . import testing

            if scenario in testing.SCENARIOS:
                hook_cm = testing.scope(scenario)
            else:
                logger.warning(
                    "PM_FAKE_BACKEND=%r 不是可用场景（%s），本次按真实后端执行",
                    scenario,
                    "|".join(testing.SCENARIOS),
                )

        try:
            task = kwargs.get("task", "")
            if not task and "task_file" in kwargs:
                task = Path(kwargs["task_file"]).read_text(encoding="utf-8")
            init = initial_state(
                task=task,
                context=kwargs.get("context", ""),
                target_model=kwargs.get("target_model", "未指定"),
                n_test_cases=int(kwargs.get("n_test_cases", 3)),
                max_iterations=int(kwargs.get("max_iterations", 3)),
                auto_clarify=True,  # API 模式无交互终端
                seed_cases=kwargs.get("test_cases") or None,
                assertion_mode=str(kwargs.get("assertion_mode") or ""),
            )
            # 对外的 run_id 必须是唯一真相：否则报告内文里的 run_id 与 API 句柄对不上（M0）
            init["run_id"] = run_id
            # API 场景用 MemorySaver（无跨进程持久化需求），避免 SQLite 依赖
            app = build_app()
            config = {
                "configurable": {"thread_id": run_id},
                "recursion_limit": 100,
            }

            with hook_cm, usage_scope() as ledger:
                final: dict[str, Any] = {}
                # 逐节点刷新进度，运行中就能看到迭代/评分（H2）
                for chunk in app.stream(init, config, stream_mode="values"):
                    final = dict(chunk)
                    # token 用量随节点推进实时可见，不用等到任务结束
                    final["llm_usage"] = ledger.snapshot()
                    with self._lock:
                        if rec:
                            rec.progress = _progress_of(final)
                            # 报告一落地就对外可见：否则存在"/api/status 已说 passed、
                            # /api/report 还 404"的窗口，控制台会显示"报告未生成"
                            if chunk.get("final_report"):
                                rec.result = final
                final["llm_usage"] = ledger.snapshot()

            report_path, _ = _save_artifacts(run_id, final)
            with self._lock:
                if rec:
                    rec.result = final
                    rec.report_path = report_path
                    rec.status = "finished"
            logger.info("任务 %s 完成，状态=%s", run_id, final.get("status"))
        except Exception as e:
            logger.exception("任务 %s 失败", run_id)
            with self._lock:
                if rec:
                    rec.status = "failed"
                    rec.error = str(e)


def _int_env(key: str, default: int) -> int:
    raw = os.getenv(key, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("环境变量 %s=%r 不是整数，回退默认值 %s", key, raw, default)
        return default


def _build_race_report(
    race: RaceRecord,
    statuses: dict[str, Any],
    reports: dict[str, str],
) -> str:
    lines: list[str] = []
    lines.append(f"# 赛马报告：{race.name}")
    lines.append("")
    lines.append(f"- race_id：`{race.race_id}`")
    lines.append(f"- 总任务数：{race.spec_count}")
    lines.append("")

    lines.append("## 各任务评分总览")
    lines.append("")
    lines.append("| run_id | 状态 | 迭代 | 平均分 | 最低分 | 用例 | 通过 |")
    lines.append("|---|---|---|---|---|---|---|")
    for rid, s in statuses.items():
        agg = s.get("aggregate") or {}
        n_cases = agg.get("n_cases", "-")
        expected = agg.get("n_cases_expected") or 0
        if expected and expected != n_cases:
            n_cases = f"{n_cases}/{expected}"
        lines.append(
            f"| {rid} | {s.get('status', '-')} | {s.get('iteration', '-')} "
            f"| {agg.get('avg_score', '-')} | {agg.get('min_score', '-')} "
            f"| {n_cases} | {agg.get('passed', '-')} |"
        )
    incomplete = [
        rid
        for rid, s in statuses.items()
        if (s.get("aggregate") or {}).get("n_cases_expected")
        and (s.get("aggregate") or {}).get("cases_complete") is False
    ]
    if incomplete:
        lines.append("")
        lines.append(
            f"> ⚠️ {len(incomplete)} 个任务的实际用例数少于声明条数，其分数不足以支撑达标判定。"
        )
    lines.append("")

    lines.append("## 各任务最终提示词")
    lines.append("")
    for rid, rpt in reports.items():
        lines.append(f"### {rid}")
        lines.append("")
        lines.append(rpt)
        lines.append("")
        lines.append("---")
        lines.append("")
    return "\n".join(lines)
