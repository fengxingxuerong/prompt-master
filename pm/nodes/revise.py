"""Node 6：定向修订（复用 optimize 的生成质量门）。

（自原 pm/nodes.py 拆出；原文注释逐字保留。）
"""

from __future__ import annotations

import logging
from typing import Any

from ..prompts import REVISER_SYSTEM, REVISER_USER, render
from ..schemas import PromptVersion
from ..state import State
from .common import _apply
from .optimize import _generate_prompt_with_gate

logger = logging.getLogger("pm.nodes.revise")


def _attempted_text(state: State) -> str:
    """列出此前各轮"针对什么反馈改出了什么分数"，切断在两种解释之间来回震荡。"""
    history = [h for h in (state.get("revision_history") or []) if isinstance(h, dict)]
    cur = int(state.get("iteration", 0) or 0)
    lines = []
    for h in history:
        if int(h.get("iteration", 0) or 0) >= cur:
            continue
        lines.append(
            f"- 第 {h.get('iteration')} 轮针对：{h.get('feedback') or '（无记录）'}\n"
            f"  改出结果：avg {h.get('avg_score')} / min {h.get('min_score')}"
            + (f"（下界 {h.get('ci_lower')}）" if h.get("ci_lower") is not None else "")
        )
    return "\n".join(lines) if lines else "（无历史记录，本轮是首次修订）"


def revise_node(state: State) -> dict:
    node = "revise"
    prev_prompt = state["prompt"]
    # 字符预算注入：抽象的"净增量≤30%"在真实端点上执行不稳（实测 572/578 字符
    # 连续超限），把比例翻译成具体数字模型才可执行——与 Optimizer 自己的
    # "模糊形容词替换为可测量要求"原则一致
    user_prompt = render(
        REVISER_USER,
        original_task=state["task"],
        previous_prompt=prev_prompt,
        evaluation_feedback=state.get("revision_feedback", "（无）"),
        attempted=_attempted_text(state),
        n=len(state.get("test_cases", [])),
        prev_len=len(prev_prompt),
        max_len=int(len(prev_prompt) * 1.3) + 1,
    )

    try:
        new_prompt, meta, q_report, calls = _generate_prompt_with_gate(
            "reviser", REVISER_SYSTEM, user_prompt
        )
    except Exception as e:
        logger.exception("revise 失败")
        return _apply(
            state,
            node,
            {"status": "failed", "errors": [f"revise: {e}"]},
            "revise_failed",
            error=str(e),
        )

    if not new_prompt.strip():
        # 修订空返回不能追加一条与上一版相同的版本：那会让报告把同一文本当成“修订后版本”
        # 并给出不同分数，读者会误读成“修订带来了提升”（C2）。直接按未达标终态交付历史最佳。
        logger.warning("修订返回空内容，保留上一版并终止迭代")
        return _apply(
            state,
            node,
            {
                "status": "early_stopped",
                "early_stop_reason": "修订器返回空内容（端点截断/思考耗尽），保留历史最佳版本",
                "errors": ["revise: 修订返回空内容，已保留上一版"],
                "should_revise": False,
                "llm_calls": state.get("llm_calls", 0) + calls,
            },
            "revise_empty",
            calls=calls,
        )

    versions = list(state.get("prompt_versions", []))
    versions.append(
        PromptVersion(
            iteration=state.get("iteration", 0) + 1,
            prompt=new_prompt,
            note=f"第 {state.get('iteration', 0) + 1} 轮修订",
        ).model_dump()
    )

    patch: dict[str, Any] = {
        "prompt": new_prompt,
        "prompt_versions": versions,
        "iteration": state.get("iteration", 0) + 1,
        "evaluations": [],
        "test_runs": [],
        "llm_calls": state.get("llm_calls", 0) + calls,
    }
    # 累积质量警告（供 evaluate 注入 + 报告展示）
    if not q_report.ok:
        issues = list(state.get("prompt_quality_issues", []))
        issues.append({"iteration": state.get("iteration", 0) + 1, "issues": q_report.describe()})
        patch["prompt_quality_issues"] = issues

    return _apply(
        state,
        node,
        patch,
        "revise_done",
        new_prompt_chars=len(new_prompt),
        quality_ok=q_report.ok,
        quality_issues=q_report.describe() or None,
        retried=calls > 1,
        model=meta.get("model"),
        latency_ms=meta.get("latency_ms"),
        channel=meta.get("channel"),
        attempts=meta.get("attempts"),
    )
