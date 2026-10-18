"""同步流水线执行：构建图、演示模式作用域、interrupt 澄清循环。（自 run.py 拆出，逻辑逐字保留）"""

from __future__ import annotations

import argparse
import contextlib
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from pm.graph import build_app
from pm.llm import usage_scope

from .support import recursion_budget


def _rescue_artifact(state: dict[str, Any]) -> Path | None:
    """把中断时**已经跑出来**的状态落到运营目录（尽力而为，落盘失败不许掩盖原始异常）。

    为什么必须做：2026-10-14 的 R4 单变量对照那一轮，`.json` 产物是 0 字节、
    `logs/` 下也没有那一轮的 `run_*.json`（同日完成/早停的四轮都有落盘）——
    进程一断，几十次真端点调用连同它要裁定的判据一起没了。而 langgraph 默认带
    MemorySaver，每个节点之后的状态本来就在内存里，只是没人去取。

    两个刻意的形状：
    1. **明写 `artifact_interrupted=true`**——半份产物长得像完整报告比没有产物更危险；
    2. **空状态不落盘**——"文件在、信息不在"正是那次事故的坏形状（`live_run_*.json` 0 字节）。
    """
    if not state:
        return None
    from pm import paths
    from pm.modelhub.filelock import _atomic_write

    payload = dict(state)
    payload["artifact_interrupted"] = True
    payload["interrupted_at"] = datetime.now().isoformat(timespec="seconds")
    try:
        target = paths.log_dir() / f"run_{state.get('run_id') or 'unknown'}.json"
        _atomic_write(target, payload)
    except OSError:
        return None
    return target


# --------------------------------------------------------------------------
# 交互模式：处理 interrupt
# --------------------------------------------------------------------------
def _pending_interrupts(app: Any, config: Any) -> list[dict[str, Any]]:
    snap = app.get_state(config)
    found: list[dict[str, Any]] = []
    for task in getattr(snap, "tasks", []) or []:
        for it in getattr(task, "interrupts", []) or []:
            found.append(it.value)
    return found


def _demo_scope() -> Any:
    """演示模式作用域（与 server/scheduler 同一机制）。

    `PM_FAKE_BACKEND` 有效时把假后端限定在本次运行的作用域内（ContextVar，
    不改动任何模块属性，C4）。CLI 之前不支持该变量——无 Key 演示只能走 server；
    对齐后 CLI 也能跑。值不可用只提醒一句，照旧按真实后端执行。
    """
    from pm import testing

    scenario = testing.active_scenario()
    if scenario:
        return testing.scope(scenario)
    raw = (os.getenv("PM_FAKE_BACKEND") or "").strip()
    if raw:
        print(
            f"⚠️ PM_FAKE_BACKEND={raw!r} 不是可用场景"
            f"（{'|'.join(testing.SCENARIOS)}），本次按真实后端执行"
        )
    return contextlib.nullcontext()


def _print_clarification(payload: Any) -> None:
    print("\n" + "=" * 60)
    print("需要澄清（需求不够清晰）")
    print("=" * 60)
    if isinstance(payload, dict):
        print(f"任务理解：{payload.get('task_summary', '')}\n")
        for i, q in enumerate(payload.get("questions", []), 1):
            print(f"  {i}. {q}")
    print("\n请输入回答（直接回车表示按系统推断继续）：")


def _answer_clarifications(app: Any, config: Any) -> None:
    """交互模式：不断响应澄清中断，直到流程走完（最多 5 轮）。"""
    from langgraph.types import Command

    rounds = 0
    while rounds < 5:
        interrupts = _pending_interrupts(app, config)
        if not interrupts:
            return
        _print_clarification(interrupts[0])
        try:
            answer = input("> ").strip()
        except EOFError:
            answer = ""
        app.invoke(Command(resume=answer or "按你的推断继续"), config)
        rounds += 1


def _rescue_on_interrupt(app: Any, config: Any, ledger: Any) -> None:
    """中断之后把**已经跑出来**的状态落盘（尽力而为，失败不添乱）。

    中断（Ctrl+C / 终端关闭走的是 BaseException）也要留证：这一轮烧掉的调用
    不能随进程一起消失（R4 事故）。抢救本身失败就老老实实让原始异常抛出去——
    把 KeyboardInterrupt 变成别的错，会让调用方以为任务本身坏了。
    """
    try:
        partial = dict(app.get_state(config).values or {})
    except Exception:  # noqa: BLE001  抢救路径：checkpointer 自己也坏了就只留异常，不添乱
        partial = {}
    if not partial:
        return
    if ledger is not None:
        try:
            partial["llm_usage"] = ledger.snapshot()
        except Exception:  # noqa: BLE001  台账快照取不到也不能挡住"把状态存下来"这件正事
            pass
    saved = _rescue_artifact(partial)
    if saved:
        print(
            f"⚠️ 运行被中断，已把跑到的进度存到 {saved}"
            f"（带 artifact_interrupted=true——这是中断快照，不是完整报告）",
            file=sys.stderr,
        )


def run_pipeline(args: argparse.Namespace, init: dict[str, Any]) -> dict[str, Any]:
    app = build_app(sqlite_path=args.checkpoint)
    config = {
        "configurable": {"thread_id": args.thread_id or init["run_id"]},
        "recursion_limit": recursion_budget(args.max_iter),
    }

    hook_cm = _demo_scope()

    ledger: Any = None
    final: dict[str, Any] = {}
    try:
        with hook_cm, usage_scope() as _ledger:
            ledger = _ledger
            app.invoke(init, config)
            if args.interactive:
                _answer_clarifications(app, config)
            final = app.get_state(config).values
    except BaseException:
        # 中断（Ctrl+C / 终端关闭走的是 BaseException）也要留证：这一轮烧掉的调用
        # 不能随进程一起消失（R4 事故）。抢救本身失败就老老实实让原始异常抛出——
        # 把 KeyboardInterrupt 变成别的错，会让调用方以为任务本身坏了。
        _rescue_on_interrupt(app, config, ledger)
        raise

    # 台账快照写回 state：报告与 /api/status 直接展示（usage_scope 外已无记账）
    final["llm_usage"] = ledger.snapshot()
    # final 已按 dict[str, Any] 声明：langgraph 的 get_state().values 是 Any，
    # 逐字保留原语义的同时让 mypy 看得见类型（原先的 cast 现在冗余）
    return final
