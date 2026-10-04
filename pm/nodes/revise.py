"""Node 6：定向修订（复用 optimize 的生成质量门）。

（自原 pm/nodes.py 拆出；原文注释逐字保留。）
"""

from __future__ import annotations

import logging
from typing import Any

from ..llm import CallBudgetExceeded
from ..prompts import REVISER_SYSTEM, REVISER_USER, render
from ..quality import MIN_PROMPT_LENGTH, SIZE_GROWTH_RATIO
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


def revise_node(state: State) -> dict[str, Any]:
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
        max_len=int(len(prev_prompt) * SIZE_GROWTH_RATIO) + 1,
    )

    try:
        new_prompt, meta, q_report, calls = _generate_prompt_with_gate(
            "reviser", REVISER_SYSTEM, user_prompt
        )
    except CallBudgetExceeded as e:
        # 墙钟预算用尽（如 429 风暴下重试叠层烧光 600s）与"空返回"同语义：
        # 都是修订器没能给出新版本。第六轮 E2E 实测（run 87d9c16e0e48）这条
        # 曾被下面的通用 except 炸成整轮 failed——评估闭环已完成的版本没被交付。
        # 转 early_stopped 保留历史最佳，预算细节进 errors 供报告披露。
        logger.warning("修订器墙钟预算用尽，保留上一版并终止迭代：%s", e)
        return _apply(
            state,
            node,
            {
                "status": "early_stopped",
                "early_stop_reason": "修订器墙钟预算用尽（端点限流/重试叠层），保留历史最佳版本",
                "errors": [f"revise: {e}"],
                "should_revise": False,
            },
            "revise_budget_exhausted",
            error=str(e),
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

    # 净增量核对：<size_budget> 里的"≤30%"原本只是给模型看的散文，超标无人验证，
    # 交付物就逐轮膨胀（而约束越多、目标模型漏执行越多）。这里做确定性核对，
    # 把超标写进版本注记让报告可见——不自动重写：为长度重写可能换来内容回归。
    prev_len, new_len = len(prev_prompt), len(new_prompt)
    # 百分比只在基准有意义时才报：真实跑里出过 v0 只有 12 字符（优化器空返回后残留的
    # 骨架）的情况，对这种基准算净增量得到 74100% 这种荒谬数字，反而掩盖真问题。
    # 门槛直接沿用质量门的"提示词过短"口径（MIN_PROMPT_LENGTH）：低于它就已经不是
    # 一份能拿来对比长度的交付物了。
    growth_pct = round((new_len / prev_len - 1) * 100, 1) if prev_len >= MIN_PROMPT_LENGTH else None
    max_len = int(prev_len * SIZE_GROWTH_RATIO)
    over_budget = new_len > max_len
    if over_budget:
        logger.warning("修订净增量超预算：%d → %d 字符（预算 %d 字符）", prev_len, new_len, max_len)

    versions = list(state.get("prompt_versions", []))
    note = f"第 {state.get('iteration', 0) + 1} 轮修订"
    if over_budget:
        note += f"（长度 {prev_len}→{new_len} 字符，超 {max_len} 预算" + (
            f"，净增量 {growth_pct:.0f}%" if growth_pct is not None else "）"
        )
    versions.append(
        PromptVersion(
            iteration=state.get("iteration", 0) + 1,
            prompt=new_prompt,
            note=note,
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
        new_prompt_chars=new_len,
        prev_prompt_chars=prev_len,
        growth_pct=growth_pct,
        size_budget_exceeded=over_budget,
        quality_ok=q_report.ok,
        quality_issues=q_report.describe() or None,
        retried=calls > 1,
        model=meta.get("model"),
        latency_ms=meta.get("latency_ms"),
        channel=meta.get("channel"),
        attempts=meta.get("attempts"),
    )
