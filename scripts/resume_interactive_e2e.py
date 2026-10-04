#!/usr/bin/env python3
"""交互式 E2E 的阶段 2 驱动：从 checkpoint 恢复澄清中断，喂入用户回答续跑到终态。

背景：run.py --interactive 的澄清交互走 stdin input()，长实验中无法无人值守地
把问题转给用户再收集回答。本脚本与「阶段 1（run.py --interactive --checkpoint
--thread-id X；进程阻塞在 input() 时安全终止）」配合：
  1. 阶段 1 的 invoke(init) 返回时，interrupt 状态已落盘 SqliteSaver；
  2. 本脚本加载同一 checkpoint + thread_id，用 Command(resume=answer) 续跑，
     后处理与 run_pipeline 完全同款（save_artifacts + 单 JSON stdout + 四态退出码）。

口径注记：阶段 1 进程的内存台账随进程终止不可恢复，最终报告的 llm_usage 只含
阶段 2 调用；跨阶段总调用数以 data/usage_daily.json 时间窗对账为准。

用法：
  python scripts/resume_interactive_e2e.py <checkpoint.db> <thread_id> \
      --answer-file <utf8.txt>
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

# 与 run.py 薄壳同款启动序列（顺序是行为的一部分）：
# sys.path 先行（早于任何 pm 导入），再 prepare_console()（load_dotenv + stdio）
sys.path.insert(0, str(ROOT))

from pm.bootstrap import prepare_console  # noqa: E402

prepare_console()

from langgraph.types import Command  # noqa: E402

from pm.cli.pipeline import _pending_interrupts  # noqa: E402  # 与 run_pipeline 同源，避免双实现漂移
from pm.cli.support import (  # noqa: E402
    _result_exit_code,
    emit_json_result,
    recursion_budget,
    save_artifacts,
)
from pm.graph import build_app  # noqa: E402
from pm.llm import usage_scope  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("checkpoint", help="阶段 1 落盘的 SQLite checkpoint 路径")
    ap.add_argument("thread_id", help="阶段 1 的 --thread-id")
    ap.add_argument("--answer-file", required=True, help="用户回答文本（UTF-8）")
    ap.add_argument("--max-iter", type=int, default=3, help="与阶段 1 一致的递归预算基数")
    args = ap.parse_args()

    answer = Path(args.answer_file).read_text(encoding="utf-8").strip()

    app = build_app(sqlite_path=args.checkpoint)
    config: dict[str, Any] = {
        "configurable": {"thread_id": args.thread_id},
        "recursion_limit": recursion_budget(args.max_iter),
    }

    with usage_scope() as ledger:
        app.invoke(Command(resume=answer or "按你的推断继续"), config)
        # 防呆：同 thread 若还有后续澄清中断，按系统推断继续（run.py 的
        # 「直接回车」语义），保证无人值守能跑完。
        for _ in range(4):
            if not _pending_interrupts(app, config):
                break
            app.invoke(Command(resume="按你的推断继续"), config)
        final = app.get_state(config).values

    # 台账快照写回 state（与 run_pipeline 收尾同款）
    final["llm_usage"] = ledger.snapshot()

    log_path, report_path = save_artifacts(final, None)
    emit_json_result(final, log_path, report_path)
    return _result_exit_code(final.get("status"))


if __name__ == "__main__":
    sys.exit(main())
