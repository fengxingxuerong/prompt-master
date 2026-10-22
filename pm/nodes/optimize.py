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


EMPTY_UNKNOWN = "未观测到截断/额度证据，根因待查"


def empty_output_reason(label: str, meta: dict[str, Any], calls: int) -> str:
    """空产出的归因：**只有当场实测到"输出等于 max_tokens"才说额度**，否则照旧写未知。

    §三十·十九·六 的教训是"归档里写死一个没有证据的归因，比不写更坏"（那句"思考耗尽"
    后来被 token 分布推翻：空产出横跨 1493~16425 全分布）。所以这里不猜成因，只报数：
    2026-10-15 真端点一轮 `5253290b3fb5` 的修订器 3 发全部 `output_tokens=24000`
    = `PM_REVISER_MAX_TOKENS` 上限而正文为空 —— 这种就叫有证据。
    """
    out = int((meta or {}).get("output_tokens") or 0)
    cap = int((meta or {}).get("max_tokens") or 0)
    if cap <= 0 or out < cap:
        return f"{label}返回空内容，{EMPTY_UNKNOWN}"
    return (
        f"{label}返回空内容，且实测输出触达上限（output_tokens={out} = max_tokens={cap}，"
        f"本节点共 {calls} 发）⇒ 正文没有额度；要复核就把该角色"
        f"（{(meta or {}).get('role') or label}）的 max_tokens 上限调大再跑一轮"
    )


def _deliver_on_budget_exhausted(
    state: State, node: str, exc: Exception, seed: str
) -> dict[str, Any]:
    """优化器墙钟预算耗尽时的收口：有可交付版本就按未达标终态交出去，一个都没有才判 failed。

    分岔按数据走，不按希望走——`early_stopped` 的语义是"交付了东西但没达标"，
    手里没货时用它就是在假交付。抽成函数而不是就地写：`optimize_node` 的复杂度
    棘轮（CC 上限 10）不该为一条兜底分支让路。

    成本台账（2026-10-23 修）：预算烧完前已发出的真实请求（全部失败）两个分支都要
    入账——否则报告头「LLM 调用次数」系统性偏低（revise 的预算分支同日同修）。
    失败请求在 llm_usage.failed_calls 另有一份按角色的明细，与这个轮级总账不重复。
    """
    spent = int(getattr(exc, "details", {}).get("requests") or 0)
    fallback = str(state.get("prompt") or "").strip() or seed
    if not fallback:
        logger.exception("optimize 失败（且无任何可交付版本）")
        return _apply(
            state,
            node,
            {
                "status": "failed",
                "errors": [f"optimize: {exc}"],
                "llm_calls": state.get("llm_calls", 0) + spent,
            },
            "optimize_failed",
            error=str(exc),
            requests=spent,
        )
    versions = list(state.get("prompt_versions", []))
    if not any(str(v.get("prompt") or "").strip() == fallback for v in versions):
        versions.append(
            PromptVersion(iteration=0, prompt=fallback, note="原稿（优化未完成）").model_dump()
        )
    return _apply(
        state,
        node,
        {
            "status": "early_stopped",
            "early_stop_reason": "优化器墙钟预算耗尽（端点单发超时吃满剩余预算），交付已有版本",
            "errors": [f"optimize: {exc}"],
            "prompt": fallback,
            "prompt_versions": versions,
            "should_revise": False,
            "llm_calls": state.get("llm_calls", 0) + spent,
        },
        "optimize_budget_exhausted",
        error=str(exc),
        requests=spent,
    )


def _generate_prompt_with_gate(
    role: str, system: str, user_prompt: str
) -> tuple[str, dict[str, Any], QualityReport, int]:
    """生成提示词并过代码侧质量门（防元话语泄漏）。

    不合格时带 hint 重试；重试后仍不合格则按原样接受并记录警告
    （不炸流程，由 evaluate 注入让评估器重点核查）。

    返回 (prompt, meta, quality_report, llm_calls)
    """

    # §三十·二十一：**空产出**要额外的重试机会。
    #
    # 为什么单列：实测空产出率 **19.7%**（66 轮里 13 轮，§三十·二十），
    # 而 `optimize` 节点拿到空串就早停——那 13 轮整轮作废、3 万多 token 打水漂。
    # 判据实测：`check_prompt_quality("")` → ok=False / "提示词为空"，
    # 所以空串**确实会触发下面那次带 hint 重试**（实测 calls=2）。
    # 问题在于**只重试一次**：第二次还空就放弃。
    #
    # 为什么空产出要再多给机会，而质量问题不给：
    #   · 质量问题（过长/含元话语）重试一次通常就好了，且反复重试容易把
    #     提示词越改越偏——「按原样接受并记录警告」是刻意的选择。
    #   · 空产出**不含任何信息**，重试没有「越改越偏」的风险；
    #     而 19.7% 的发生率意味着「再试一次」的期望收益很高。
    #
    # 成本上限：最多额外 2 次（约 2×100s）。19.7% 里「连空两次」的占比未测，
    # 所以这里取的是保守值，不是算出来的最优解。
    _max_empty_retries = 2
    text, meta = llm.plain_call(role, system, user_prompt)
    calls = 1
    prompt = _strip_code_fence(text)
    report = check_prompt_quality(prompt)

    empty_retries = 0
    while not report.ok and empty_retries < _max_empty_retries and _is_empty_prompt(prompt):
        empty_retries += 1
        logger.warning(
            "[%s] 第 %d 次空产出（%s），追加一次重试机会",
            role,
            empty_retries,
            report.describe(),
        )
        # meta 必须一起换掉：重试成功时要报的是**这次**调用的元信息，
        # 留着第一次那个空产出的 meta 会让"实际花了多久/哪个模型"对不上
        # （第一版这里漏了赋值，mypy 的 no-any-return 顺带把它揪了出来）。
        text, meta = llm.plain_call(role, system, user_prompt + retry_hint(report))
        calls += 1
        prompt = _strip_code_fence(text)
        report = check_prompt_quality(prompt)

    if report.ok:
        return prompt, meta, report, calls

    # 空产出已经把额外机会用完了 ⇒ 别再掉进下面的质量门重试。
    # 不加这句会多发一次（第 4 次）纯重复请求：empty_retries 用满时
    # 那一支的 `report` 仍然是"提示词为空"，于是下面的带 hint 重试照跑。
    # 这是被 tests/test_empty_delivery_retry.py::test_empty_retries_are_bounded
    # 逮到的：它期望 3 次、实测 4 次。
    if empty_retries >= _max_empty_retries:
        logger.warning(
            "[%s] 空产出重试已用满（%d 次），停止：%s", role, empty_retries, report.describe()
        )
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


def _is_empty_prompt(prompt: str) -> bool:
    """是否「空产出」——与「质量门不过」是不同的判据。

    分开的理由（§三十·二十一）：质量问题重试可能越改越偏，所以只给一次；
    空产出没有任何内容，重试没有副作用，所以可以多给机会。
    判据必须用 `strip()` 之后的空值——实测纯空白串同样判「提示词为空」，
    而 `_strip_code_fence` 剥完可能留下换行。
    """
    return not prompt.strip()


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
    except llm.CallBudgetExceeded as e:
        # 真实案例 A 实测（2026-10-15，`4077d3fa3711`）：优化器一发 300s 超时后，
        # **同一个异常在 revise 里是 early_stopped 保留历史最佳，在这里却把整轮判成 failed**
        # ——而原稿改进模式下手里明明还有用户的原稿。1 次调用、302 秒白等，
        # 换到的是一份连"已有版本"都没归档的失败。
        logger.warning("优化器墙钟预算耗尽，尝试交付已有版本：%s", e)
        return _deliver_on_budget_exhausted(state, node, e, seed)
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
                "early_stop_reason": empty_output_reason("优化器", meta, calls),
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
