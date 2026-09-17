"""同步流水线执行：构建图、演示模式作用域、interrupt 澄清循环。（自 run.py 拆出，逻辑逐字保留）"""

from __future__ import annotations

import contextlib
import os
from typing import Any

from pm.graph import build_app
from pm.llm import usage_scope

from .support import recursion_budget


# --------------------------------------------------------------------------
# 交互模式：处理 interrupt
# --------------------------------------------------------------------------
def _pending_interrupts(app, config):
    snap = app.get_state(config)
    found = []
    for task in getattr(snap, "tasks", []) or []:
        for it in getattr(task, "interrupts", []) or []:
            found.append(it.value)
    return found


def run_pipeline(args, init: dict) -> dict:
    from langgraph.types import Command

    app = build_app(sqlite_path=args.checkpoint)
    config = {
        "configurable": {"thread_id": args.thread_id or init["run_id"]},
        "recursion_limit": recursion_budget(args.max_iter),
    }

    # 演示模式（与 server/scheduler 同一机制）：PM_FAKE_BACKEND 有效时，
    # 把假后端限定在本次运行的作用域内（ContextVar，不改动任何模块属性，C4）。
    # CLI 之前不支持该变量——无 Key 演示只能走 server；对齐后 CLI 也能跑。
    scenario = os.getenv("PM_FAKE_BACKEND", "").strip()
    hook_cm: Any = contextlib.nullcontext()
    if scenario:
        from pm import testing

        if scenario in testing.SCENARIOS:
            hook_cm = testing.scope(scenario)
        else:
            print(
                f"⚠️ PM_FAKE_BACKEND={scenario!r} 不是可用场景"
                f"（{'|'.join(testing.SCENARIOS)}），本次按真实后端执行"
            )

    with hook_cm, usage_scope() as ledger:
        app.invoke(init, config)

        if not args.interactive:
            final = app.get_state(config).values
        else:
            # 交互模式：不断响应澄清中断，直到流程走完
            rounds = 0
            while rounds < 5:
                interrupts = _pending_interrupts(app, config)
                if not interrupts:
                    break
                payload = interrupts[0]
                print("\n" + "=" * 60)
                print("需要澄清（需求不够清晰）")
                print("=" * 60)
                if isinstance(payload, dict):
                    print(f"任务理解：{payload.get('task_summary', '')}\n")
                    for i, q in enumerate(payload.get("questions", []), 1):
                        print(f"  {i}. {q}")
                print("\n请输入回答（直接回车表示按系统推断继续）：")
                try:
                    answer = input("> ").strip()
                except EOFError:
                    answer = ""
                app.invoke(Command(resume=answer or "按你的推断继续"), config)
                rounds += 1
            final = app.get_state(config).values

    # 台账快照写回 state：报告与 /api/status 直接展示（usage_scope 外已无记账）
    final["llm_usage"] = ledger.snapshot()
    return final
