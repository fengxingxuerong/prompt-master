"""Node 7：交付报告（纯代码，不额外消耗 LLM）。

（自原 pm/nodes.py 拆出；原文注释逐字保留。）
渲染逻辑在 `pm/report.py`（纯字符串组装，便于单测与改文案）；
本模块只负责把结果写回图状态。
"""

from __future__ import annotations

import logging
from typing import Any

from .. import llm
from ..report import render_report
from ..state import State
from .common import _apply

logger = logging.getLogger("pm.nodes.report")


def report_node(state: State) -> dict[str, Any]:
    """组装最终交付物。

    渲染逻辑在 `pm/report.py`（纯字符串组装，便于单测与改文案）；
    这里只负责把结果写回图状态。
    """
    # 用量台账在图内收口：报告渲染发生在本节点，早于外层（run_pipeline/scheduler）
    # 写回 state，所以必须在这里从当前上下文取快照，报告里的用量表才非空
    ledger = llm.current_ledger()
    report, best = render_report(state)
    patch: dict[str, Any] = {
        "final_report": report,
        "prompt": best.get("prompt", state.get("prompt", "")),
    }
    if ledger is not None:
        patch["llm_usage"] = ledger.snapshot()
    return _apply(
        state,
        "report",
        patch,
        "report_done",
        status=state.get("status", "running"),
        best_iteration=best.get("iteration"),
    )
