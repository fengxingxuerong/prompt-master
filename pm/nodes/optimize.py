"""Node 2：提示词优化（生成 + 代码侧质量门）。

（自原 pm/nodes.py 拆出；原文注释逐字保留。）
生成质量门 `_generate_prompt_with_gate` 同时被 revise 节点复用。
"""

from __future__ import annotations

import logging
from typing import Any

from .. import llm
from ..memory import find_similar_asset, render_memory_hint
from ..prompts import (
    OPTIMIZER_SYSTEM,
    OPTIMIZER_USER,
    REFINER_SYSTEM,
    REFINER_USER,
    render,
)
from ..quality import QualityReport, check_prompt_quality, retry_hint
from ..schemas import PromptVersion
from ..state import SEED_PROMPT_MAX_CHARS, State
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


def _seed_findings(seed: str) -> list[str]:
    """对原稿做代码侧确定性体检，返回问题描述列表（零 LLM 调用）。"""
    return [i.detail for i in check_prompt_quality(seed).issues]


def optimize_node(state: State) -> dict[str, Any]:
    node = "optimize"
    task = state["task"]
    context = state.get("context", "")
    target_model = state.get("target_model", "未指定")
    seed = str(state.get("seed_prompt") or "").strip()

    if seed and len(seed) > SEED_PROMPT_MAX_CHARS:
        # 入口层（CLI/API）已有同口径护栏，这里兜住直接构造 state 的调用方
        logger.warning("原稿超长（%d > %d），终止流程", len(seed), SEED_PROMPT_MAX_CHARS)
        return _apply(
            state,
            node,
            {
                "status": "failed",
                "errors": [
                    f"optimize: 原稿 {len(seed)} 字符超过上限 {SEED_PROMPT_MAX_CHARS}，"
                    "请裁剪后再提交（原稿会进基线臂每条用例的每次调用）"
                ],
            },
            "optimize_seed_too_long",
            seed_chars=len(seed),
        )

    if seed:
        # 原稿改进分支：不进记忆层（相似资产的骨架会诱导整段重写，与"最小改动"对冲），
        # 但把代码侧体检命中项原样递给改进器——确定性规则比评委更值得采信。
        findings = _seed_findings(seed)
        user_prompt = render(
            REFINER_USER,
            target_model=target_model,
            model_profile=_model_profile(target_model),
            original_task=task,
            user_prompt=seed,
            quality_findings="\n".join(f"- {f}" for f in findings) or "（无命中）",
            context=context or "（无）",
            seed_len=len(seed),
            max_len=int(len(seed) * 1.3) + 200,
        )
        system, note = REFINER_SYSTEM, "改进自用户原稿"
        asset = None
    else:
        findings = []
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
        system, note = OPTIMIZER_SYSTEM, "初版"

    try:
        prompt, meta, q_report, calls = _generate_prompt_with_gate("optimizer", system, user_prompt)
    except Exception as e:
        logger.exception("optimize 失败")
        return _apply(
            state,
            node,
            {"status": "failed", "errors": [f"optimize: {e}"]},
            "optimize_failed",
            error=str(e),
        )

    # 2026-09-30 两轮 E2E 实测：optimizer 空产物连续发生（两轮都返回空提示词），
    # 空 prompt 继续跑完整评估循环 = 浪费 50+ 次调用。与 revise 对齐：空产物直接早停。
    if not prompt.strip():
        logger.warning("优化器返回空提示词，终止流程并如实入账")
        return _apply(
            state,
            node,
            {
                "status": "early_stopped",
                "early_stop_reason": "优化器返回空内容（端点截断/思考耗尽），无可优化提示词",
                "errors": ["optimize: 优化器返回空提示词，无法继续优化流程"],
                "llm_calls": state.get("llm_calls", 0) + calls,
                "prompt_quality_issues": [{"iteration": 0, "issues": q_report.describe()}],
            },
            "optimize_empty",
            calls=calls,
        )

    versions = list(state.get("prompt_versions", []))
    versions.append(PromptVersion(iteration=0, prompt=prompt, note=note).model_dump())

    patch: dict[str, Any] = {
        "prompt": prompt,
        "prompt_versions": versions,
        "llm_calls": state.get("llm_calls", 0) + calls,
    }
    if findings:
        # 原稿自身的问题单独入账：不进 prompt_quality_issues——那一列会被 judge 按
        # 当前轮次注入评委，让改进版为原稿的毛病挨扣分（v0 的分数必须只反映 v0）。
        patch["seed_quality_findings"] = findings
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
        seed_chars=len(seed) or None,
        seed_findings=len(findings) or None,
        **patch_extra,
    )
