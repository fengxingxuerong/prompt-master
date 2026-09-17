"""Node 2：提示词优化（生成 + 代码侧质量门）。

（自原 pm/nodes.py 拆出；原文注释逐字保留。）
生成质量门 `_generate_prompt_with_gate` 同时被 revise 节点复用。
"""

from __future__ import annotations

import logging
from typing import Any

from .. import llm
from ..memory import find_similar_asset, render_memory_hint
from ..prompts import OPTIMIZER_SYSTEM, OPTIMIZER_USER, render
from ..quality import QualityReport, check_prompt_quality, retry_hint
from ..schemas import PromptVersion
from ..state import State
from .common import _apply, _strip_code_fence
from .profiles import _model_profile

logger = logging.getLogger("pm.nodes.optimize")


def _generate_prompt_with_gate(
    role: str, system: str, user_prompt: str
) -> tuple[str, dict[str, Any], QualityReport, int]:
    """生成提示词并过代码侧质量门（防元话语泄漏）。

    不合格时自动带 hint 重试一次；重试后仍不合格则按原样接受并记录警告
    （不炸流程，由 evaluate 注入让评估器重点核查）。

    返回 (prompt, meta, quality_report, llm_calls)
    """
    text, meta = llm.plain_call(role, system, user_prompt)
    calls = 1
    prompt = _strip_code_fence(text)
    report = check_prompt_quality(prompt)
    if report.ok:
        return prompt, meta, report, calls

    logger.warning("[%s] 生成物未过质量门（%s），带 hint 重试", role, report.describe())
    text2, meta2 = llm.plain_call(role, system, user_prompt + retry_hint(report))
    calls += 1
    prompt2 = _strip_code_fence(text2)
    report2 = check_prompt_quality(prompt2)
    if report2.ok:
        logger.info("[%s] 重试后通过质量门", role)
        return prompt2, meta2, report2, calls
    logger.warning("[%s] 重试后仍未过质量门（%s），按原样接受并记录警告", role, report2.describe())
    return prompt2, meta2, report2, calls


def optimize_node(state: State) -> dict[str, Any]:
    node = "optimize"
    task = state["task"]
    context = state.get("context", "")
    target_model = state.get("target_model", "未指定")

    # 记忆层·写侧：命中相似达标资产时注入参考块（PM_MEMORY_HINT=0 关闭）。
    # 注入的是"结构参考"不是答案：块内已标注按需取舍，且只借鉴 passed + Δ≥0 的运行。
    asset = find_similar_asset(task, exclude_run_id=state.get("run_id"))
    if asset:
        context = (context or "") + render_memory_hint(asset)

    user_prompt = render(
        OPTIMIZER_USER,
        target_model=target_model,
        model_profile=_model_profile(target_model),
        task_description=task,
        context=context or "（无）",
        revision_hint="",
    )

    try:
        prompt, meta, q_report, calls = _generate_prompt_with_gate(
            "optimizer", OPTIMIZER_SYSTEM, user_prompt
        )
    except Exception as e:
        logger.exception("optimize 失败")
        return _apply(
            state,
            node,
            {"status": "failed", "errors": [f"optimize: {e}"]},
            "optimize_failed",
            error=str(e),
        )

    versions = list(state.get("prompt_versions", []))
    versions.append(PromptVersion(iteration=0, prompt=prompt, note="初版").model_dump())

    patch: dict[str, Any] = {
        "prompt": prompt,
        "prompt_versions": versions,
        "llm_calls": state.get("llm_calls", 0) + calls,
    }
    if not q_report.ok:
        patch["prompt_quality_issues"] = [{"iteration": 0, "issues": q_report.describe()}]

    patch_extra: dict[str, Any] = {}
    if asset:
        patch_extra["memory_hint"] = {"run_id": asset["run_id"], "similarity": asset["similarity"]}

    return _apply(
        state,
        node,
        patch,
        "optimize_done",
        prompt_chars=len(prompt),
        quality_ok=q_report.ok,
        quality_issues=q_report.describe() or None,
        retried=calls > 1,
        model=meta.get("model"),
        latency_ms=meta.get("latency_ms"),
        channel=meta.get("channel"),
        attempts=meta.get("attempts"),
        **patch_extra,
    )
