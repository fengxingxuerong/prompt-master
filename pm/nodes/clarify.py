"""Node 1 / 1b：需求澄清与向用户提问。

（自原 pm/nodes.py 拆出；原文注释逐字保留。）
"""

from __future__ import annotations

import logging
from typing import Any

from langgraph.types import interrupt

from .. import llm
from ..prompts import CLARIFIER_SYSTEM, CLARIFIER_USER, render
from ..schemas import ClarificationResult
from ..state import State
from .common import _apply

logger = logging.getLogger("pm.nodes.clarify")


def clarify_node(state: State) -> dict[str, Any]:
    node = "clarify"
    task = state["task"]
    context = state.get("context", "")
    answers = state.get("clarification_answers", "")

    # 若上一轮已收集到用户回答，把它并入上下文再分析。
    # 用标记做幂等保护：本节点会被重跑多次，重复追加会导致上下文不断膨胀。
    ANSWER_MARK = "<用户补充回答>"
    if answers and ANSWER_MARK not in context:
        context = (context + f"\n\n{ANSWER_MARK}\n" + answers + f"\n</{ANSWER_MARK[1:]}>").strip()

    user_prompt = render(CLARIFIER_USER, task=task, context=context or "（无）")

    try:
        result, meta = llm.structured_call(
            "clarifier", ClarificationResult, CLARIFIER_SYSTEM, user_prompt
        )
    except Exception as e:
        logger.exception("clarify 失败")
        return _apply(
            state,
            node,
            {
                "status": "failed",
                "errors": [f"clarify: {e}"],
                "clarification": {"is_clear": True, "task_summary": task},
            },
            "clarify_failed",
            error=str(e),
        )

    logger.info(
        "clarify: is_clear=%s questions=%d", result.is_clear, len(result.clarifying_questions)
    )

    patch: dict[str, Any] = {
        "clarification": result.model_dump(),
        "clarify_round": state.get("clarify_round", 0) + 1,
        "needs_reanalysis": False,
        "llm_calls": state.get("llm_calls", 0) + 1,
    }
    # 把推断上下文注入 context，供后续 Optimizer 使用（原文档缺失这一步）
    INFERRED_MARK = "<推断上下文"
    if not result.is_clear and INFERRED_MARK not in context:
        inferred = result.inferred_context
        context = (
            context + "\n\n<推断上下文（用户需求不够清晰，以下为系统推断，已向用户明示）>\n"
            f"- 目标受众：{inferred.target_audience}\n"
            f"- 领域：{inferred.domain}\n"
            f"- 输出格式：{inferred.output_format}\n"
            f"- 语气：{inferred.tone}\n"
            f"- 其他约束：{', '.join(inferred.constraints) or '无'}\n"
            "</推断上下文>"
        ).strip()
        patch["unresolved_questions"] = result.clarifying_questions

    if result.is_clear and state.get("unresolved_questions"):
        # 用户已回答且本轮判清晰：遗留问题必须清空，否则报告仍会把已回答的问题列为遗留（A7）
        patch["unresolved_questions"] = []

    # 无论本次是否判定清晰，都要把（可能含用户回答与推断的）上下文写回，
    # 否则用户回答只会停在 state 里，永远传不到 Optimizer。
    patch["context"] = context

    return _apply(
        state,
        node,
        patch,
        "clarify_done",
        is_clear=result.is_clear,
        questions=result.clarifying_questions,
        # meta 结构零容错的下标取值会把上游缺字段放大成节点崩溃（L6），统一 .get 容错
        model=meta.get("model"),
        latency_ms=meta.get("latency_ms"),
        channel=meta.get("channel"),
    )


def ask_user_node(state: State) -> dict[str, Any]:
    """独立的提问节点。

    为什么必须独立成节点（这是个容易踩的坑）：
    `interrupt()` 靠抛异常实现，节点内 interrupt 之后的代码**不会执行**；
    且 resume 时 LangGraph 会**从节点函数第一行重新执行**。
    若把 interrupt 放在 clarify_node 的分析之后，重跑时会带着旧 state 再分析一次，
    而"保存用户答案"的那次 return 从未被执行 —— 答案直接丢失。

    独立成节点后职责单一：进节点即 interrupt，resume 重跑时
    interrupt 直接返回答案并落盘，再由路由回 clarify 带答案重新分析。
    """
    node = "ask_user"
    clarification = state.get("clarification") or {}
    questions = clarification.get("clarifying_questions") or state.get("unresolved_questions", [])

    payload = {
        "type": "clarification_needed",
        "task_summary": clarification.get("task_summary", state.get("task", "")),
        "questions": questions,
        "inferred_context": clarification.get("inferred_context", {}),
    }

    answer = interrupt(payload)  # 抛异常暂停；resume 后重跑本节点并在此返回答案

    prior = state.get("clarification_answers", "")
    merged = (prior + "\n" + str(answer)).strip() if prior else str(answer)

    return _apply(
        state,
        node,
        {
            # 用显式布尔标志而非把 clarification 置 None：
            # 后者依赖 LangGraph 对 None 值的覆盖语义，实测不可靠。
            "needs_reanalysis": True,
            "clarification_answers": merged,
            "clarify_round": state.get("clarify_round", 0) + 1,
            # 提问轮数独立计数（M8）：一轮交互 = clarify + ask_user + clarify，
            # clarify_round 会把这三步都算进去，用它当提问上限会缩水一轮
            "clarify_questions_asked": state.get("clarify_questions_asked", 0) + 1,
        },
        "user_answered",
        questions=questions,
    )
