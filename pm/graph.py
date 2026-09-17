"""
LangGraph 图装配。

针对原文档的修复：
1. 原骨架 `lambda state: ... else END` 在条件边里返回字符串 "END"，
   实际应返回 END 常量；且引用了未声明的 state["clarification"]。
2. 原骨架缺 mock 节点、缺失败兜底、缺最终报告节点。
3. 这里补上 interrupt（人在回路澄清）所需的 checkpointer —— 没有它，
   `interrupt()` 会直接抛异常。

拓扑：
    START → clarify ──(需澄清)──→ ask_user → clarify  (最多 2 轮)
                └──(清晰)──→ optimize → mock → test → evaluate
                                                  ↑        │
                                                  │   (未达标且未超轮次)
                                                  │        ↓
                                                  └──── revise
                                                          │(达标/超轮次/失败)
                                                          ↓
                                                       report → END
"""

from __future__ import annotations

import logging
from typing import Any, cast

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from . import nodes
from .state import State

logger = logging.getLogger("pm.graph")


# --------------------------------------------------------------------------
# 路由函数
# --------------------------------------------------------------------------
def route_after_clarify(state: State) -> str:
    if state.get("status") == "failed":
        return "report"
    # 刚收到用户回答 → 带答案重新分析
    if state.get("needs_reanalysis"):
        return "clarify"
    if state.get("clarification") is None:
        return "clarify"

    clarification = state["clarification"] or {}
    if not clarification.get("is_clear") and not state.get("auto_clarify", True):
        # M8：上限按「已向用户提问的轮数」计，而不是 clarify 分析次数。
        # 旧写法 `clarify_round < 2` 里 clarify_round 在每次 clarify 都 +1
        # （含带答案重分析），实际最多只问 1 轮，与文档承诺的 2 轮不符。
        if state.get("clarify_questions_asked", 0) < 2:
            return "ask_user"
        logger.warning("澄清提问已达 2 轮上限，改用推断上下文继续")
    return "optimize"


_TERMINAL = ("passed", "max_iterations", "failed", "early_stopped")


def route_after_generate(state: State) -> str:
    """optimize / revise 硬失败或已终态时直通 report（M3）。

    这两个节点后面原先是无条件边：生成失败后图仍会拿着空/旧 prompt 跑完
    mock→test→evaluate，白烧 N×(1+评委数) 次真实计费调用，且失败信号会被后续
    节点的 status 覆写。失败就该立即交付。
    """
    return "report" if state.get("status") in _TERMINAL else "next"


def route_after_evaluate(state: State) -> str:
    status = state.get("status")
    if status in _TERMINAL:
        return "report"
    if state.get("should_revise") and state.get("iteration", 0) < state.get("max_iterations", 3):
        return "revise"
    return "report"


# --------------------------------------------------------------------------
# 构建
# --------------------------------------------------------------------------
def build_graph(checkpointer: Any = None) -> Any:
    g = StateGraph(State)

    g.add_node("clarify", nodes.clarify_node)
    g.add_node("ask_user", nodes.ask_user_node)
    g.add_node("optimize", nodes.optimize_node)
    g.add_node("mock", nodes.mock_node)
    g.add_node("baseline", nodes.baseline_node)
    g.add_node("test", nodes.test_node)
    g.add_node("evaluate", nodes.evaluate_node)
    g.add_node("revise", nodes.revise_node)
    g.add_node("compare", nodes.compare_node)
    g.add_node("report", nodes.report_node)

    g.add_edge(START, "clarify")
    g.add_conditional_edges(
        "clarify",
        route_after_clarify,
        {
            "clarify": "clarify",
            "ask_user": "ask_user",
            "optimize": "optimize",
            # 所有去交付的路径都先过成对盲评（无法比较时它会直接透传）
            "report": "compare",
        },
    )
    # ask_user 必须显式连回 clarify，否则该节点无出边会被隐式接到 END，
    # 导致用户回答后流程直接终止（这一坑极易被忽略）
    g.add_edge("ask_user", "clarify")
    g.add_conditional_edges("optimize", route_after_generate, {"next": "mock", "report": "compare"})
    # 基线在测试集锁定之后、主路测试之前跑：两边共用同一批用例与采样口径，Δ 才可比
    g.add_edge("mock", "baseline")
    g.add_edge("baseline", "test")
    g.add_edge("test", "evaluate")
    g.add_conditional_edges(
        "evaluate",
        route_after_evaluate,
        {"revise": "revise", "report": "compare"},
    )
    g.add_conditional_edges("revise", route_after_generate, {"next": "test", "report": "compare"})
    g.add_edge("compare", "report")
    g.add_edge("report", END)

    return g.compile(checkpointer=checkpointer)


def build_app(sqlite_path: str | None = None) -> Any:
    """编译可执行应用。

    sqlite_path 为空时用 MemorySaver（进程内，够用于单轮运行与 interrupt）；
    传入路径则用 SqliteSaver，支持跨进程恢复与运行历史回溯。
    """
    if sqlite_path:
        import sqlite3

        from langgraph.checkpoint.sqlite import SqliteSaver

        conn = sqlite3.connect(sqlite_path, check_same_thread=False)
        checkpointer = SqliteSaver(conn)
    else:
        checkpointer = MemorySaver()
    return build_graph(checkpointer=checkpointer)


def mermaid() -> str:
    """导出图的可视化描述，便于文档与调试。"""
    try:
        return cast("str", build_graph(checkpointer=MemorySaver()).get_graph().draw_mermaid())
    except Exception as e:  # noqa: BLE001
        logger.warning("生成 mermaid 失败：%s", e)
        return ""
