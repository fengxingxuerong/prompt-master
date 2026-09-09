"""
6 个图节点。

流程：clarify → optimize → mock → test → evaluate → (revise → test → evaluate)* → END

关键设计决策（与原文档的差异）：
1. **测试用例只在首轮生成一次**。原文档每轮都重新生成 mock input，
   会导致迭代前后评估基准漂移，分数对比失去意义 —— 这在优化闭环里是致命的。
   这里首轮锁定测试集，后续轮次复用，保证 A/B 可比。
2. **目标模型调用失败不炸图**。单条用例失败记为 error 并在评估时判低分，
   全部失败才置 status=failed，保证部分失败也能产出可用结果。
3. **判定以代码计算的加权分为准**，模型自报分仅记录用于偏差监控。
4. **双评委交叉验证（P0）**：评估默认由两个评委模型独立打分（PM_JUDGES=2），
   分差在阈值内取各维度均值，分差过大触发第三评委仲裁（PM_ARBITER_*），
   避免单一 LLM 自评的系统性放水。
5. **目标模型调用并发化（P0）**：多用例用线程池并发调用 + 信号量限速
   （PM_TARGET_MAX_CONCURRENCY），把迭代延迟从 用例数 × 轮次 降下来。
6. **本地结果缓存（P1）**：目标输出与评估结果按内容哈希落盘，
   断点续跑（SQLite checkpoint 恢复）时命中即跳过，不重复消耗 API 预算。
"""

from __future__ import annotations

import json
import logging
import os
import random
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from langgraph.types import interrupt
from pydantic import ValidationError

from .assertions import check_assertion
from .cache import eval_cache, key_for_eval, key_for_target, target_cache
from .llm import build_config, call_fingerprint, plain_call, structured_call
from .prompts import (
    CLARIFIER_SYSTEM,
    CLARIFIER_USER,
    COMPARATOR_SYSTEM,
    COMPARATOR_USER,
    EVALUATOR_SYSTEM,
    EVALUATOR_USER,
    MOCKGEN_SYSTEM,
    MOCKGEN_USER,
    OPTIMIZER_SYSTEM,
    OPTIMIZER_USER,
    REVISER_SYSTEM,
    REVISER_USER,
    render,
)
from .quality import QualityReport, check_prompt_quality, evaluator_warning_block, retry_hint
from .schemas import (
    PASS_THRESHOLD,
    AggregateScore,
    ClarificationResult,
    DimensionScores,
    EvaluationResult,
    MockInputSet,
    PreferenceResult,
    PromptVersion,
    TestRun,
    early_stop_reason,
)
from .state import State, merge_state, trace_event

logger = logging.getLogger("pm.nodes")


def _apply(state: State, node: str, patch: dict[str, Any], event: str, **payload) -> dict:
    """把节点产出与日志事件合并成一份返回值。"""
    log = trace_event(state, node, event, **payload)
    return merge_state(patch, log)


# ==========================================================================
# Node 1: 需求澄清
# ==========================================================================
def clarify_node(state: State) -> dict:
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
        result, meta = structured_call(
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


# ==========================================================================
# Node 1b: 向用户提问（独立节点 —— 修复 interrupt 的重跑语义问题）
# ==========================================================================
def ask_user_node(state: State) -> dict:
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
        },
        "user_answered",
        questions=questions,
    )


# ==========================================================================
# Node 2: 提示词优化（核心）
# ==========================================================================
def _generate_prompt_with_gate(
    role: str, system: str, user_prompt: str
) -> tuple[str, dict[str, Any], QualityReport, int]:
    """生成提示词并过代码侧质量门（防元话语泄漏）。

    不合格时自动带 hint 重试一次；重试后仍不合格则按原样接受并记录警告
    （不炸流程，由 evaluate 注入让评估器重点核查）。

    返回 (prompt, meta, quality_report, llm_calls)
    """
    text, meta = plain_call(role, system, user_prompt)
    calls = 1
    prompt = _strip_code_fence(text)
    report = check_prompt_quality(prompt)
    if report.ok:
        return prompt, meta, report, calls

    logger.warning("[%s] 生成物未过质量门（%s），带 hint 重试", role, report.describe())
    text2, meta2 = plain_call(role, system, user_prompt + retry_hint(report))
    calls += 1
    prompt2 = _strip_code_fence(text2)
    report2 = check_prompt_quality(prompt2)
    if report2.ok:
        logger.info("[%s] 重试后通过质量门", role)
        return prompt2, meta2, report2, calls
    logger.warning("[%s] 重试后仍未过质量门（%s），按原样接受并记录警告", role, report2.describe())
    return prompt2, meta2, report2, calls


def optimize_node(state: State) -> dict:
    node = "optimize"
    task = state["task"]
    context = state.get("context", "")
    target_model = state.get("target_model", "未指定")

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
    )


# ==========================================================================
# Node 3: 模拟输入生成（仅首轮）
# ==========================================================================
def mock_node(state: State) -> dict:
    node = "mock"
    n = int(state.get("n_test_cases", 3))
    existing = [c for c in state.get("test_cases", []) or [] if c and c.strip()]

    # 用户提供了测试集（ground-truth 场景）：直接采用，不再让 mockgen 生成 ——
    # 既省一次 LLM 调用，也保证断言用的 expected 与输入逐条对齐。
    seeds = [
        c
        for c in state.get("seed_cases", []) or []
        if isinstance(c, dict) and (c.get("input") or "").strip()
    ]
    if seeds and len(existing) < n:
        inputs = [c["input"].strip() for c in seeds][:8]
        return _apply(
            state,
            node,
            {"test_cases": inputs},
            "mock_from_seed",
            n_cases=len(inputs),
            n_expected=n,
            source="user_seed_cases",
        )

    # 首轮锁定测试集，后续轮次复用，保证迭代间可比；
    # 但**条数不足时不锁定**：一次欠采样的基准不能当成永久基准（C1）。
    if len(existing) >= n:
        return _apply(
            state,
            node,
            {},
            "mock_skipped",
            reason="复用首轮锁定的测试集以保证可比性",
            n_cases=len(existing),
            n_expected=n,
        )

    user_prompt = render(MOCKGEN_USER, prompt=state["prompt"], n=n)
    cases: list[str] = []
    rationale: list[str] = []
    meta: dict[str, Any] = {}
    calls = 0
    err: str | None = None

    for attempt in (1, 2):
        try:
            result, meta = structured_call("mockgen", MockInputSet, MOCKGEN_SYSTEM, user_prompt)
        except Exception as e:  # 生成失败走降级基准，不炸图
            err = str(e)
            logger.exception("mock 生成失败（第 %d 次），回退为原始需求作为单条用例", attempt)
            break
        calls += 1
        # 只截不补是 C1 的根源：这里改成“不够就再要一次”，而不是默默拿 1 条去当基准
        got = [c for c in result.test_cases if c and c.strip()][:n]
        if len(got) > len(cases):
            cases, rationale = got, list(result.rationale or [])
        if len(cases) >= n:
            break
        logger.warning("mock 只给了 %d/%d 条用例，重新生成", len(cases), n)
        user_prompt += (
            f"\n\n<count_warning>上次只输出了 {len(cases)} 条。请严格输出 {n} 条彼此不重复的模拟输入，"
            "且 rationale 与 test_cases 逐条对齐。</count_warning>"
        )

    degraded = False
    if not cases:
        # 降级基准：没有可用用例时退回原始需求本身，但标记为 degraded，不允许据此判达标。
        cases = [state["task"]]
        rationale = ["（降级）mock 未产出用例，回退为原始需求"]
        degraded = True
        err = err or "mockgen 未返回可用用例"

    patch: dict[str, Any] = {
        "test_cases": cases,
        "llm_calls": state.get("llm_calls", 0) + max(1, calls),
    }
    if degraded:
        patch["errors"] = [f"mock: {err}"]
    elif len(cases) < n:
        # 条数不够不能静默：评分口径受限要写进错误列表与报告（C1）
        patch["errors"] = [f"mock: 只得到 {len(cases)}/{n} 条用例，样本量不足以支撑达标判定"]

    return _apply(
        state,
        node,
        patch,
        "mock_fallback" if degraded else "mock_done",
        n_cases=len(cases),
        n_expected=n,
        degraded=degraded,
        rationale=rationale[: len(cases)],
        model=meta.get("model"),
        latency_ms=meta.get("latency_ms"),
        channel=meta.get("channel"),
        attempts=meta.get("attempts"),
        error=err,
    )


# ==========================================================================
# Node 4: 测试执行（代码节点，真实调用目标模型，并发 + 输出缓存）
# ==========================================================================
# 并发硬上限：环境变量写 9999 这类值时直接放行会瞬间打爆目标端点（L8）
TARGET_CONCURRENCY_CAP = 32


def _target_concurrency() -> int:
    """目标模型并发上限（L8：9999 不封顶会把端点瞬间打爆，加硬上限并告警）。"""
    raw = os.getenv("PM_TARGET_MAX_CONCURRENCY", "4")
    try:
        val = int(raw)
    except ValueError:
        logger.warning("PM_TARGET_MAX_CONCURRENCY=%r 不是整数，回退默认 4", raw)
        return 4
    if val > TARGET_CONCURRENCY_CAP:
        logger.warning(
            "PM_TARGET_MAX_CONCURRENCY=%d 超过上限 %d，已封顶（受目标 API 速率限制约束，调高无益）",
            val,
            TARGET_CONCURRENCY_CAP,
        )
        val = TARGET_CONCURRENCY_CAP
    return max(1, val)


def _attach_assertion(run: TestRun, expected: str, mode: str) -> TestRun:
    """把确定性断言结果写进 TestRun；无标注（且非自定义断言）或调用失败的用例跳过。

    custom:* 断言不需要 expected（判定全在注册函数里），必须放行。
    """
    if run.error:
        return run
    if not expected and not (mode and mode.startswith("custom:")):
        return run
    result = check_assertion(expected, run.output, mode or "contains")
    if result is not None:
        run.assertion = result.model_dump()
    return run


def _samples_per_case() -> int:
    """每条用例的重复采样次数（`PM_SAMPLES_PER_CASE`，默认 2）。

    为什么需要：旧流程每条用例每轮只采样 1 次就去比 8.0 阈值，而 target 温度 0.7
    本身的抖动就大于判定余量 —— “分数提升”与“噪声”分不开。多采样后用中位数定分、
    用极差估计噪声，并将判定改看均分的保守下界。
    """
    raw = os.getenv("PM_SAMPLES_PER_CASE", "2").strip()
    try:
        n = int(raw)
    except ValueError:
        logger.warning("PM_SAMPLES_PER_CASE=%r 不是整数，回退为 2", raw)
        return 2
    return max(1, min(5, n))


def _run_one_target(
    idx: int,
    case: str,
    prompt: str,
    target_model: str,
    expected: str = "",
    assert_mode: str = "",
    sample: int = 0,
) -> TestRun:
    """执行单条测试用例；命中目标输出缓存时直接复用（断点续跑省 API）。

    expected 非空时对输出执行确定性断言（ground-truth），结果记入 TestRun。
    断言对缓存命中的输出同样执行：便宜、确定，且断言不通过说明缓存里的旧输出已不满足要求。

    `sample` 是重复采样序号：必须进缓存键，否则 k 次采样会全部命中同一条旧输出，
    “重复采样”退化成“同一条结果看 k 遍”。
    """
    cache = target_cache()
    # 指纹里带上实际生效的模型/端点/采样参数：否则换了 PM_TARGET_MODEL 或温度仍命中旧缓存（H1）
    ck = key_for_target(prompt, case, target_model, f"{call_fingerprint('target')}|s{sample}")
    if cache is not None:
        cached = cache.get(ck)
        if isinstance(cached, dict) and isinstance(cached.get("output"), str):
            try:
                return _attach_assertion(
                    TestRun(
                        test_case_index=idx,
                        sample_index=sample,
                        test_input=case,
                        prompt=prompt,
                        output=cached["output"],
                        target_model=cached.get("target_model", target_model),
                        latency_ms=cached.get("latency_ms"),
                        cache_hit=True,
                    ),
                    expected,
                    assert_mode,
                )
            except ValidationError:
                pass  # 缓存内容异常则重新调用

    t0 = time.time()
    try:
        output, meta = plain_call("target", prompt, case)
        run = TestRun(
            test_case_index=idx,
            sample_index=sample,
            test_input=case,
            prompt=prompt,
            output=output,
            target_model=meta.get("model", target_model),
            latency_ms=meta.get("latency_ms"),
        )
    except Exception as e:  # noqa: BLE001 - 目标模型调用失败按用例记录，不炸图
        logger.warning("用例 #%d 第 %d 次采样调用目标模型失败：%s", idx, sample, e)
        run = TestRun(
            test_case_index=idx,
            sample_index=sample,
            test_input=case,
            prompt=prompt,
            output="",
            target_model=target_model,
            error=str(e),
            latency_ms=int((time.time() - t0) * 1000),
        )

    if cache is not None and not run.error and run.output.strip():
        # 空输出（截断/思考耗尽但未抛错）不能入盘：否则这次空结果会被冻结到后续所有续跑
        cache.put(
            ck,
            {
                "output": run.output,
                "target_model": run.target_model,
                "latency_ms": run.latency_ms,
            },
        )
    return _attach_assertion(run, expected, assert_mode)


def _run_matrix(
    cases: list[str],
    prompt: str,
    target_model: str,
    expected_fn,
    mode: str,
    k: int,
    concurrency: int,
) -> tuple[list[TestRun], int]:
    """对 (用例 × 采样) 矩阵跑 target 调用，返回按用例/采样序号排序的结果与真实调用数。

    抽成函数是为了让 test 与 baseline 共用同一套并发/缓存/断言逻辑，
    否则基线只是另一份手写实现，很容易跟主路跑偏（口径不一致的对比没有意义）。
    """
    work = [(i, c, s) for i, c in enumerate(cases) for s in range(k)]
    if concurrency <= 1 or len(work) <= 1:
        runs = [
            _run_one_target(
                i, c, prompt, target_model, expected=expected_fn(i), assert_mode=mode, sample=s
            )
            for i, c, s in work
        ]
    else:
        with ThreadPoolExecutor(max_workers=concurrency) as ex:
            runs = list(
                ex.map(
                    lambda job: _run_one_target(
                        job[0],
                        job[1],
                        prompt,
                        target_model,
                        expected=expected_fn(job[0]),
                        assert_mode=mode,
                        sample=job[2],
                    ),
                    work,
                )
            )
    # 并发返回顺序不保证，按（用例, 采样）排序保持输出稳定
    runs.sort(key=lambda r: (r.test_case_index, r.sample_index))
    n_cached = sum(1 for r in runs if r.cache_hit)
    return runs, max(0, len(runs) - n_cached)


def test_node(state: State) -> dict:
    node = "test"
    prompt = state["prompt"]
    cases = state.get("test_cases", [])
    target_model = state.get("target_model", "未指定")
    max_concurrency = _target_concurrency()
    # ground-truth：seed_cases 里的 expected 与 test_cases 按序号一一对应
    mode = (state.get("assertion_mode") or "contains").strip()
    expected_list = [
        (c.get("expected") or "") if isinstance(c, dict) else ""
        for c in state.get("seed_cases", []) or []
    ]

    def _expected(i: int) -> str:
        return expected_list[i] if i < len(expected_list) else ""

    k = _samples_per_case()
    runs, n_real_calls = _run_matrix(
        list(cases), prompt, target_model, _expected, mode, k, max_concurrency
    )

    n_failed = sum(1 for r in runs if r.error)
    patch: dict[str, Any] = {
        "test_runs": [r.model_dump() for r in runs],
        # 命中缓存的样本没有消耗 LLM 调用
        "llm_calls": state.get("llm_calls", 0) + n_real_calls,
    }
    errors = [r.error for r in runs if r.error]
    if errors:
        patch["errors"] = errors
    if n_failed == len(runs) and runs:
        patch["status"] = "failed"
        logger.error("全部测试用例调用失败")

    return _apply(
        state,
        node,
        patch,
        "test_done",
        n_cases=len(cases),
        n_samples=k,
        n_runs=len(runs),
        n_failed=n_failed,
        n_cached=sum(1 for r in runs if r.cache_hit),
        concurrency=max_concurrency,
        total_latency_ms=sum(r.latency_ms or 0 for r in runs),
    )


# ==========================================================================
# Node 5: 评估（双评委交叉验证 + 仲裁 + 结果缓存）
# ==========================================================================
def _active_judges() -> list[str]:
    """按 PM_JUDGES 决定评委列表：1 = 单评委（向后兼容），2 = 双评委（默认）。"""
    try:
        n = max(1, min(2, int(os.getenv("PM_JUDGES", "2"))))
    except ValueError:
        n = 2
    return ["evaluator", "evaluator_b"][:n]


def _judge_disagreement_threshold() -> float:
    try:
        return max(0.1, float(os.getenv("PM_JUDGE_DISAGREEMENT", "2.0")))
    except ValueError:
        return 2.0


def _judge_spec(judges: list[str]) -> str:
    """评委组合标识，用于评估缓存键（评委模型变了缓存自然失效）。"""
    parts = []
    for j in judges:
        try:
            parts.append(f"{j}:{build_config(j).model}")
        except Exception:  # noqa: BLE001 - 配置读取失败不影响缓存键构造
            parts.append(j)
    return "+".join(parts)


def _call_evaluator(
    judge: str, user_prompt: str, idx: int
) -> tuple[EvaluationResult, dict[str, Any]]:
    ev, meta = structured_call(judge, EvaluationResult, EVALUATOR_SYSTEM, user_prompt)
    ev.test_case_index = idx
    ev.finalize()
    drift = round(ev.model_reported_score - ev.weighted_score, 2)
    logger.info(
        "case#%d [%s] 加权分=%.2f 自报分=%.2f 偏差=%+.2f channel=%s",
        idx,
        judge,
        ev.weighted_score,
        ev.model_reported_score,
        drift,
        meta["channel"],
    )
    return ev, meta


def _merge_judge_results(
    results: list[tuple[str, EvaluationResult]], idx: int, user_prompt: str
) -> EvaluationResult:
    """合并多评委结果；分差过大时触发第三评委仲裁。

    规则：
    - 单评委：直接用。
    - 双评委分差 ≤ 阈值：各维度取均值，issues/suggestions 并集，
      should_revise 取保守（任一评委说要修就修）。
    - 双评委分差 > 阈值：调用仲裁评委独立复核；仲裁失败则回退取较低分
      （安全侧：宁可低估也不放水）。
    """
    if len(results) == 1:
        return results[0][1]

    name_a, ev_a = results[0]
    name_b, ev_b = results[1]
    diff = abs(ev_a.weighted_score - ev_b.weighted_score)
    judge_scores = {name_a: ev_a.weighted_score, name_b: ev_b.weighted_score}
    for ev in (ev_a, ev_b):
        ev.judge_scores = dict(judge_scores)
        ev.judge_disagreement = round(diff, 2)

    if diff <= _judge_disagreement_threshold():
        dims = DimensionScores(
            task_completion=(
                ev_a.dimension_scores.task_completion + ev_b.dimension_scores.task_completion
            )
            / 2,
            format_adherence=(
                ev_a.dimension_scores.format_adherence + ev_b.dimension_scores.format_adherence
            )
            / 2,
            constraint_compliance=(
                ev_a.dimension_scores.constraint_compliance
                + ev_b.dimension_scores.constraint_compliance
            )
            / 2,
            robustness=(ev_a.dimension_scores.robustness + ev_b.dimension_scores.robustness) / 2,
            quality=(ev_a.dimension_scores.quality + ev_b.dimension_scores.quality) / 2,
        )
        merged = EvaluationResult(
            dimension_scores=dims,
            model_reported_score=round(
                (ev_a.model_reported_score + ev_b.model_reported_score) / 2, 2
            ),
            issues=_dedupe(ev_a.issues + ev_b.issues),
            suggestions=_dedupe(ev_a.suggestions + ev_b.suggestions),
            should_revise=ev_a.should_revise or ev_b.should_revise,
            test_case_index=idx,
            judge="merged",
            judge_scores=judge_scores,
            judge_disagreement=round(diff, 2),
        ).finalize()
        # 保守：任一评委要求修订，或均值未达标，都进入修订
        merged.should_revise = merged.should_revise or not merged.passed
        logger.info(
            "case#%d 双评委分差 %.2f ≤ 阈值 → merged=%.2f", idx, diff, merged.weighted_score
        )
        return merged

    # 分差过大 → 仲裁
    dispute_note = (
        f"\n\n<JUDGE_DISPUTE>\n前两位评委的加权分分歧较大，需要你独立复核：\n"
        f"- {name_a} 给出 {ev_a.weighted_score}\n"
        f"- {name_b} 给出 {ev_b.weighted_score}\n"
        "请忽略前两位评委的分数，基于原始需求、提示词、测试输入与输出，"
        "给出你自己的独立评分。\n</JUDGE_DISPUTE>"
    )
    try:
        arb, _ = _call_evaluator("arbiter", user_prompt + dispute_note, idx)
        arb.judge = "arbiter"
        arb.judge_scores = judge_scores
        arb.judge_disagreement = round(diff, 2)
        logger.warning(
            "case#%d 双评委分差 %.2f > 阈值 → 仲裁得分 %.2f", idx, diff, arb.weighted_score
        )
        return arb
    except Exception as e:  # noqa: BLE001
        logger.warning("仲裁调用失败，回退取较低分（安全侧）：%s", e)
        fallback = ev_a if ev_a.weighted_score <= ev_b.weighted_score else ev_b
        fallback.judge = "conservative"
        return fallback


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        if it and it not in seen:
            seen.add(it)
            out.append(it)
    return out


def _evaluate_one(
    run: dict[str, Any],
    *,
    task: str,
    context: str,
    prompt: str,
    judges: list[str],
    judge_spec: str,
    q_warns: list[str],
    errors: list[str],
) -> tuple[dict[str, Any], int, bool]:
    """评一份输出（一个采样），返回 (评估 dict, 额外 LLM 调用数, 是否命中缓存)。

    抽成函数是为了让 baseline 与主路走**完全相同的评分口径**：
    没有基线就只能报“最终 8.2 分”而报不出“比不优化好多少”，而口径不一致的对比没有意义。
    """
    idx = int(run["test_case_index"])
    # 调用失败的用例：不浪费一次评估，直接判低分
    if run.get("error"):
        ev = EvaluationResult(
            dimension_scores=_min_dims(),
            model_reported_score=1.0,
            issues=[f"目标模型调用失败：{run['error']}"],
            suggestions=["检查目标模型 API 配置（PM_TARGET_*）与网络连通性"],
            should_revise=False,  # 基础设施问题，改提示词无意义
            test_case_index=idx,
            judge="system",
        ).finalize()
        return ev.model_dump(), 0, False

    user_prompt = render(
        EVALUATOR_USER,
        original_task=task,
        context=context or "（无）",
        prompt=prompt,
        test_input=run["test_input"],
        test_output=run["output"],
    )
    if q_warns:
        user_prompt += evaluator_warning_block(q_warns)

    # 1) 命中缓存直接复用（断点续跑 / 相同输出重复评估）
    cache = eval_cache()
    # 键里带上原始需求 / 上下文 / 质量警告：不同 task 不能串用同一份评估结果（H1）
    ck = key_for_eval(
        prompt,
        run["test_input"],
        run["output"],
        judge_spec,
        extra=f"{task}\n{context}\n{'|'.join(q_warns)}",
    )
    if cache is not None:
        cached = cache.get(ck)
        if isinstance(cached, dict):
            try:
                ev = EvaluationResult.model_validate(cached)
                ev.cache_hit = True
                return ev.model_dump(), 0, True
            except ValidationError:
                pass  # 缓存内容异常则重新评估

    # 2) 各评委独立评估
    results: list[tuple[str, EvaluationResult]] = []
    n_calls = 0
    for judge in judges:
        try:
            ev, _ = _call_evaluator(judge, user_prompt, idx)
            results.append((judge, ev))
            n_calls += 1
        except Exception as e:  # noqa: BLE001
            logger.warning("用例 #%d 评委 %s 评估失败：%s", idx, judge, e)
            errors.append(f"evaluate#{idx}[{judge}]: {e}")

    if not results:
        ev = EvaluationResult(
            dimension_scores=_min_dims(),
            model_reported_score=1.0,
            issues=[f"全部评委评估失败：{errors[-1] if errors else 'unknown'}"],
            suggestions=["检查评估模型配置（PM_EVALUATOR_* / PM_EVALUATOR_B_*）"],
            should_revise=False,
            test_case_index=idx,
            judge="system",
        ).finalize()
        return ev.model_dump(), n_calls, False

    # 3) 合并 / 仲裁
    final_ev = _merge_judge_results(results, idx, user_prompt)
    if final_ev.judge == "arbiter":
        n_calls += 1  # 仲裁也是一次 LLM 调用
    # 仅当**全部**评委都成功时才落盘：否则单评委退化结果会被写进双评委缓存键，
    # 断点续跑命中后该用例永久跳过交叉验证 —— P0 防线会被静默关闭（A1）。
    if cache is not None and len(results) == len(judges):
        cache.put(ck, final_ev.model_dump())
    final_ev.sample_index = int(run.get("sample_index", 0) or 0)
    return final_ev.model_dump(), n_calls, False


def _evaluate_runs(
    runs: list[dict[str, Any]],
    *,
    task: str,
    context: str,
    prompt: str,
    judges: list[str],
    judge_spec: str,
    q_warns: list[str],
) -> tuple[list[dict[str, Any]], list[str], int, int]:
    """逐样本评分后把同一用例的多次采样压成一条（中位数定分、极差当噪声）。"""
    raw: list[dict[str, Any]] = []
    errors: list[str] = []
    n_cache_hit = 0
    n_llm_calls = 0
    for run in runs:
        ev, calls, hit = _evaluate_one(
            run,
            task=task,
            context=context,
            prompt=prompt,
            judges=judges,
            judge_spec=judge_spec,
            q_warns=q_warns,
            errors=errors,
        )
        n_llm_calls += calls
        n_cache_hit += 1 if hit else 0
        raw.append(ev)
    return _collapse_samples(raw), errors, n_cache_hit, n_llm_calls


def _collapse_samples(evaluations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """同一用例的 k 次采样 → 一条结果：加权分取中位数，极差当噪声估计。

    代表样本选“加权分最接近中位数”的那一条，保留它的 issues/suggestions 证据，
    以免修订反馈拿到一个偶发极值的取证。
    """
    by_case: dict[int, list[dict[str, Any]]] = {}
    for e in evaluations:
        by_case.setdefault(int(e.get("test_case_index", 0) or 0), []).append(e)

    out: list[dict[str, Any]] = []
    for idx in sorted(by_case):
        group: list[dict[str, Any]] = by_case[idx]
        scores = [float(g.get("weighted_score", 0.0) or 0.0) for g in group]
        ordered = sorted(scores)
        mid = ordered[len(ordered) // 2]
        if len(ordered) % 2 == 0:
            mid = (ordered[len(ordered) // 2 - 1] + ordered[len(ordered) // 2]) / 2
        mid = round(mid, 2)
        rep_src: dict[str, Any] = min(
            group, key=lambda g: abs(float(g.get("weighted_score", 0.0)) - mid)
        )
        rep = dict(rep_src)
        rep["test_case_index"] = idx
        rep["weighted_score"] = mid
        rep["passed"] = mid >= PASS_THRESHOLD
        rep["sample_scores"] = [round(s, 2) for s in scores]
        rep["n_samples"] = len(group)
        rep["score_spread"] = round(max(scores) - min(scores), 2) if len(scores) > 1 else 0.0
        out.append(rep)
    return out


def _collect_assertions(runs: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    """汇总事实断言：同一用例的任一样本失败就视为该用例未通过（保守侧）。"""
    out: dict[int, dict[str, Any]] = {}
    for r in runs:
        a = r.get("assertion")
        if not isinstance(a, dict):
            continue
        idx = int(r.get("test_case_index", 0) or 0)
        prev = out.get(idx)
        if prev is None or (prev.get("passed") and not a.get("passed")):
            out[idx] = a
    return out


def evaluate_node(state: State) -> dict:
    node = "evaluate"
    runs = state.get("test_runs", [])
    task = state["task"]
    context = state.get("context", "")
    prompt = state["prompt"]
    judges = _active_judges()
    judge_spec = _judge_spec(judges)

    # 当前版本若被质量门标记（元话语/上下文泄漏），注入让评估器重点核查
    current_iter = state.get("iteration", 0)
    q_warns = [
        q["issues"]
        for q in state.get("prompt_quality_issues", [])
        if q.get("iteration") == current_iter and q.get("issues")
    ]

    evaluations, errors, n_cache_hit, n_llm_calls = _evaluate_runs(
        runs,
        task=task,
        context=context,
        prompt=prompt,
        judges=judges,
        judge_spec=judge_spec,
        q_warns=q_warns,
    )
    evals = [EvaluationResult.model_validate(e) for e in evaluations]
    # 事实断言（ground-truth）：从 test_runs 收集断言结果，参与聚合的一票否决
    assertions = _collect_assertions(runs)
    # 以**声明条数**为基准：用例数不足时 AggregateScore 自己拒绝判达标（C1）
    agg = AggregateScore.from_evaluations(
        evals,
        n_expected=int(state.get("n_test_cases", 0) or 0),
        assertions=assertions or None,
    )
    logger.info(
        "聚合结果：avg=%.2f min=%.2f 下界=%.2f 噪声=%.2f passed=%s（缓存命中 %d）",
        agg.avg_score,
        agg.min_score,
        agg.ci_lower,
        agg.noise,
        agg.passed,
        n_cache_hit,
    )

    # 更新当前版本的分数
    versions = list(state.get("prompt_versions", []))
    if versions:
        versions[-1]["avg_score"] = agg.avg_score
        versions[-1]["min_score"] = agg.min_score

    iteration = state.get("iteration", 0)
    max_iter = state.get("max_iterations", 3)

    # 记下“本轮是针对什么反馈改出来的、结果如何”：修订器下一轮能看到，
    # 就不会在两种解释之间来回震荡（旧版只给本轮反馈，历史全靠模型记性）
    history = list(state.get("revision_history", []) or [])
    history.append(
        {
            "iteration": iteration,
            "avg_score": agg.avg_score,
            "min_score": agg.min_score,
            "ci_lower": agg.ci_lower,
            "noise": agg.noise,
            "feedback": _feedback_digest(state.get("revision_feedback", "")),
        }
    )

    patch: dict[str, Any] = {
        "evaluations": evaluations,
        "aggregate": agg.model_dump(),
        "prompt_versions": versions,
        "revision_history": history,
        "llm_calls": state.get("llm_calls", 0) + n_llm_calls,
    }
    if errors:
        patch["errors"] = errors

    if agg.passed:
        patch["status"] = "passed"
        patch["should_revise"] = False
    elif iteration >= max_iter:
        # 兜底：达到上限仍未达标，交付当前最佳版本 + 未解决问题
        patch["status"] = "max_iterations"
        patch["should_revise"] = False
    else:
        # P3: 修订提前终止——回退/平台期时继续修只会随评估标准摇摆震荡，
        # 直接按未达标终态交付历史最佳版本（report_node 已有该兜底逻辑）。
        avg_scores = [v["avg_score"] for v in versions if v.get("avg_score") is not None]
        stop_reason = early_stop_reason(avg_scores, noise=agg.noise)
        if stop_reason:
            patch["status"] = "early_stopped"
            patch["should_revise"] = False
            patch["early_stop_reason"] = stop_reason
        else:
            patch["should_revise"] = True
            patch["revision_feedback"] = _build_feedback(agg, evals)

    n_arbitrated = sum(1 for e in evaluations if e.get("judge") == "arbiter")
    return _apply(
        state,
        node,
        patch,
        "evaluate_done",
        avg=agg.avg_score,
        min=agg.min_score,
        n_passed=agg.n_passed,
        n_cases=agg.n_cases,
        passed=agg.passed,
        judges=",".join(judges),
        n_cache_hit=n_cache_hit,
        n_arbitrated=n_arbitrated,
    )


# ==========================================================================
# Node 6: 修订
# ==========================================================================
def revise_node(state: State) -> dict:
    node = "revise"
    user_prompt = render(
        REVISER_USER,
        original_task=state["task"],
        previous_prompt=state["prompt"],
        evaluation_feedback=state.get("revision_feedback", "（无）"),
        attempted=_attempted_text(state),
        n=len(state.get("test_cases", [])),
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


# --------------------------------------------------------------------------
# 工具函数
# --------------------------------------------------------------------------
def _cases_short_note(agg: dict[str, Any]) -> str:
    """用例数不足时在报告里显式标注（C1）：避免“分数很高”被当成“结论可靠”。"""
    if agg.get("cases_complete", True):
        return ""
    return f"（不足：期望 {agg.get('n_cases_expected')} 条，本次不予判定达标）"


def _min_dims() -> DimensionScores:
    return DimensionScores(
        task_completion=1,
        format_adherence=1,
        constraint_compliance=1,
        robustness=1,
        quality=1,
    )


def _build_feedback(agg: AggregateScore, evals: list[EvaluationResult]) -> str:
    """把聚合结果整理成 Reviser 可直接消费的反馈文本。"""
    if not evals:
        # resume / 手工 state 场景下可能空列表，len(vals)==0 会直接除零（A6）
        return "（本轮无评估结果，无法定向修订；请先检查测试集与目标模型调用是否正常。）"

    lines = [f"综合评分：平均 {agg.avg_score}，最低 {agg.min_score}（目标 {PASS_THRESHOLD}）", ""]

    # 找出最薄弱维度，提示 Reviser 优先修
    from .schemas import WEIGHTS

    weakest = []
    for dim, w in WEIGHTS.items():
        vals = [getattr(e.dimension_scores, dim) for e in evals]
        weakest.append((dim, w, sum(vals) / len(vals)))
    weakest.sort(key=lambda x: x[2])
    lines.append("各维度平均分（由低到高）：")
    for dim, w, avg in weakest:
        lines.append(f"  - {dim}（权重 {w:.0%}）：{avg:.1f}")
    lines.append("")

    if agg.all_issues:
        lines.append("问题清单：")
        for i in agg.all_issues:
            lines.append(f"  - {i}")
        lines.append("")
    if agg.all_suggestions:
        lines.append("修改建议：")
        for s in agg.all_suggestions:
            lines.append(f"  - {s}")
    return "\n".join(lines)


def _strip_code_fence(text: str) -> str:
    """模型常把提示词包在 ``` 里，去掉外层围栏但保留内容中的代码块。"""
    t = text.strip()
    if t.startswith("```") and t.endswith("```") and t.count("```") == 2:
        first_nl = t.find("\n")
        if first_nl != -1:
            return t[first_nl + 1 : t.rfind("```")].strip()
    return t


# ==========================================================================
# Node 6b/6c: 基线与成对盲评（回答"到底有没有变好"，而不是"最终分是多少"）
# ==========================================================================
def _feature_enabled(key: str, default: bool = True) -> bool:
    """开关型环境变量：1/true/yes 为开，0/false/no 为关。"""
    raw = os.getenv(key, "1" if default else "0").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off", ""):
        return False
    logger.warning("%s=%r 不是可识别的开关值，按默认 %s 处理", key, raw, default)
    return default


_MODEL_PROFILES: tuple[tuple[tuple[str, ...], str], ...] = (
    (
        ("claude", "anthropic"),
        "善用 XML 标签分块；可承载多步推理与显式自检清单；负向约束（禁止做 X）遵循良好。"
        "避免深层嵌套与隐含暗示，规则要能逐条核对。",
    ),
    (
        ("gpt-4", "gpt4", "o1", "o3", "openai"),
        "可承载较长指令链；对结构化输出模板遵循稳定；给「可照抄的示例骨架」比抽象形容有效得多。"
        "避免在同一条里塞多个并列要求。",
    ),
    (
        ("deepseek", "qwen", "llama", "glm", "yi", "moonshot", "kimi", "senseno"),
        "指令要短、直、显式：避免深层嵌套、微妙暗示与长距离回指；把关键约束放在开头和结尾各重申一次。"
        "格式示例必须完整给出，不要指望模型推断。",
    ),
    (("gemini",), "对表格/JSON 等结构格式响应好；安全类措辞不要过度，否则容易触发无谓拒答。"),
)


def _model_profile(target_model: str) -> str:
    """把"适配目标模型"从一句口号变成按模型给出的具体档案。

    旧版只在 system 里写"GPT 可承载复杂指令链、Claude 善用 XML……"，模型并不会据此真的区分；
    现在按 target_model 关键字命中后，只把**这一条**注入 user 段，其余家族噪声不进上下文。
    """
    name = (target_model or "").strip().lower()
    if not name or name in ("未指定", "unknown"):
        return (
            "目标模型未指定：采用通用最佳实践——短句、显式格式模板、不过度嵌套、"
            "关键约束在开头与结尾各出现一次。"
        )
    for keys, profile in _MODEL_PROFILES:
        if any(k in name for k in keys):
            return f"目标模型 {target_model} 的已知倾向与对应写法：{profile}"
    return (
        f"目标模型 {target_model} 无已知档案：采用通用最佳实践——短句、显式格式模板、"
        "不过度嵌套，并对关键约束显式重申。"
    )


def _feedback_digest(feedback: str, limit: int = 400) -> str:
    """把上一轮反馈压成摘要，供修订器判断"这个改法是否已经试过"。"""
    text = " ".join((feedback or "").split())
    return text[:limit] + ("…" if len(text) > limit else "")


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


def baseline_node(state: State) -> dict:
    """基线：把**原始需求原样当 prompt** 喂 target，同口径采样 + 同口径评委评分。

    为什么必须有：没有基线就只能报"最终 8.2 分"，报不出"比不优化好多少"。
    而"分数高"本身几乎不能说明什么——同一批用例、同一个评委，
    只有跟未优化状态对比，才能判断这套流水线是否创造了价值。
    """
    node = "baseline"
    if not _feature_enabled("PM_BASELINE", True):
        return _apply(state, node, {}, "baseline_skipped", reason="PM_BASELINE=0")
    if state.get("baseline_runs"):
        return _apply(state, node, {}, "baseline_skipped", reason="基线已跑过，复用结果保证可比")
    cases = list(state.get("test_cases", []) or [])
    if not cases:
        return _apply(state, node, {}, "baseline_skipped", reason="无测试用例")

    task = state["task"]
    context = state.get("context", "")
    target_model = state.get("target_model", "未指定")
    mode = (state.get("assertion_mode") or "contains").strip()
    expected_list = [
        (c.get("expected") or "") if isinstance(c, dict) else ""
        for c in state.get("seed_cases", []) or []
    ]

    def _expected(i: int) -> str:
        return expected_list[i] if i < len(expected_list) else ""

    judges = _active_judges()
    judge_spec = _judge_spec(judges)
    # 基线用与主路完全相同的采样次数与评分口径，否则 Δ 不可比
    k = _samples_per_case()
    try:
        runs, n_calls = _run_matrix(
            cases, task, target_model, _expected, mode, k, _target_concurrency()
        )
        evals_raw, errors, _, n_eval_calls = _evaluate_runs(
            [r.model_dump() for r in runs],
            task=task,
            context=context,
            prompt=task,  # 基线的"提示词"就是原始需求本身
            judges=judges,
            judge_spec=judge_spec,
            q_warns=[],
        )
    except Exception as e:  # noqa: BLE001 - 基线失败不应阻断主流程
        logger.warning("基线跑失败（不影响主流程）：%s", e)
        return _apply(state, node, {"errors": [f"baseline: {e}"]}, "baseline_failed", error=str(e))

    evals = [EvaluationResult.model_validate(e) for e in evals_raw]
    agg = AggregateScore.from_evaluations(
        evals,
        n_expected=int(state.get("n_test_cases", 0) or 0),
        assertions=_collect_assertions([r.model_dump() for r in runs]),
    )
    logger.info(
        "基线结果：avg=%.2f min=%.2f（与优化版同口径，用于算 Δ）", agg.avg_score, agg.min_score
    )
    return _apply(
        state,
        node,
        {
            "baseline_runs": [r.model_dump() for r in runs],
            "baseline_aggregate": agg.model_dump(),
            "llm_calls": state.get("llm_calls", 0) + n_calls + n_eval_calls,
            **({"errors": errors} if errors else {}),
        },
        "baseline_done",
        n_cases=len(cases),
        n_samples=k,
        avg=agg.avg_score,
        min=agg.min_score,
    )


def _ab_flip(run_id: Any, idx: int) -> bool:
    """成对盲评的 A/B 映射：True 表示把基线输出放在 A 侧。

    用 run_id + 用例号做确定性随机：既消除模型的位置偏好，又保证断点续跑结果可复现。
    """
    return random.Random(f"{run_id}:{idx}").random() < 0.5


def compare_node(state: State) -> dict:
    """成对盲评：把优化版与基线版的输出随机标成 A/B，让评委选边。

    pointwise 绝对打分的可比性差（同一份输出两次能差 1 分），成对偏好一致性好得多；
    要回答"到底有没有变好"，选边比做减法可靠。与 pointwise 结论冲突时如实标注，
    不自动否决——两个信号都可能有偏，冲突本身就是最有价值的信息。
    """
    node = "compare"
    if not _feature_enabled("PM_PAIRWISE", True):
        return _apply(state, node, {}, "compare_skipped", reason="PM_PAIRWISE=0")
    base_runs = [r for r in (state.get("baseline_runs") or []) if isinstance(r, dict)]
    cur_runs = [r for r in (state.get("test_runs") or []) if isinstance(r, dict)]
    if not base_runs or not cur_runs:
        return _apply(state, node, {}, "compare_skipped", reason="缺少基线或本轮输出，无法成对比较")

    def _pick(runs: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
        """每个用例取第一个调用成功的样本作为代表。"""
        out: dict[int, dict[str, Any]] = {}
        for r in runs:
            if r.get("error"):
                continue
            idx = int(r.get("test_case_index", 0) or 0)
            out.setdefault(idx, r)
        return out

    cur, base = _pick(cur_runs), _pick(base_runs)
    common = sorted(set(cur) & set(base))
    if not common:
        return _apply(state, node, {}, "compare_skipped", reason="没有可对齐的用例")

    task = state["task"]
    errors: list[str] = []
    votes = {"better": 0, "worse": 0, "tie": 0}
    details: list[dict[str, Any]] = []
    n_calls = 0

    for idx in common:
        c_out = str(cur[idx].get("output", ""))
        b_out = str(base[idx].get("output", ""))
        if not c_out.strip() and not b_out.strip():
            continue
        # 用 run_id + 用例号做确定性随机：既消除位置偏好，又保证断点续跑结果可复现
        flip = _ab_flip(state.get("run_id"), idx)
        a_out, b_label = (c_out, "cur") if not flip else (b_out, "base")
        user_prompt = render(
            COMPARATOR_USER,
            original_task=task,
            test_input=str(cur[idx].get("test_input", "")),
            output_a=a_out,
            output_b=c_out if flip else b_out,
        )
        try:
            pref, _meta = structured_call(
                "comparator", PreferenceResult, COMPARATOR_SYSTEM, user_prompt
            )
            n_calls += 1
        except Exception as e:  # noqa: BLE001 - 成对评失败不影响主流程
            logger.warning("case#%d 成对比较失败：%s", idx, e)
            errors.append(f"compare#{idx}: {e}")
            continue

        if pref.winner == "tie" or not pref.decisive:
            chosen = "tie"
        elif (pref.winner == "A") == (b_label == "cur"):
            chosen = "better"
        else:
            chosen = "worse"
        votes[chosen] += 1
        details.append(
            {
                "test_case_index": idx,
                "side_a": b_label,
                "winner": pref.winner,
                "verdict": chosen,
                "decisive": pref.decisive,
                "reason": pref.reason,
            }
        )

    decided = votes["better"] + votes["worse"]
    if decided == 0:
        verdict = "no_signal" if not details else "tie"
    elif votes["better"] > votes["worse"]:
        verdict = "better"
    elif votes["worse"] > votes["better"]:
        verdict = "worse"
    else:
        verdict = "tie"

    agg = state.get("aggregate") or {}
    conflict = ""
    if agg.get("passed") and verdict == "worse":
        conflict = "pointwise 判达标，但成对盲评多数倾向基线更好 —— 结论存疑，建议人工复核"
    elif not agg.get("passed") and verdict == "better":
        conflict = "pointwise 未达标，但成对盲评多数认为优化版更好 —— 可能是阈值/噪声问题而非无提升"

    logger.info(
        "成对盲评：优化版胜 %d / 基线胜 %d / 持平 %d → %s%s",
        votes["better"],
        votes["worse"],
        votes["tie"],
        verdict,
        f"（冲突：{conflict}）" if conflict else "",
    )
    return _apply(
        state,
        node,
        {
            "pairwise": {
                "verdict": verdict,
                "votes": votes,
                "n_compared": len(details),
                "conflict": conflict,
                "details": details,
            },
            "llm_calls": state.get("llm_calls", 0) + n_calls,
            **({"errors": errors} if errors else {}),
        },
        "compare_done",
        verdict=verdict,
        votes=votes,
        n_compared=len(details),
        conflict=conflict or None,
    )


# ==========================================================================
# Node 7: 交付报告（纯代码，不额外消耗 LLM）
# ==========================================================================
def report_node(state: State) -> dict:
    """组装最终交付物。

    原文档只说"返回当前最佳版本 + 未解决的问题"，却没实现"最佳"的选取逻辑。
    这里明确：达标即取当前版；未达标则回溯所有版本取 avg_score 最高者，
    并如实标注未达标与遗留问题 —— 不粉饰结果。
    """
    node = "report"
    versions: list[dict] = state.get("prompt_versions", [])
    agg = state.get("aggregate") or {}
    status = state.get("status", "running")
    iteration = state.get("iteration", 0)

    scored = [v for v in versions if v.get("avg_score") is not None]
    if status == "passed" and versions:
        best, best_note = versions[-1], "最终版本（已达标）"
    elif scored:
        best = max(scored, key=lambda v: (v["avg_score"] or 0, v.get("min_score") or 0))
        best_note = f"第 {best.get('iteration')} 版（未达标，取历史最高分）"
    elif versions:
        best, best_note = versions[-1], "最终版本（未获得评分）"
    else:
        best, best_note = {"iteration": 0, "prompt": ""}, "无可用版本"

    lines: list[str] = []
    lines.append("# PromptMaster 交付报告")
    lines.append("")
    lines.append(f"- run_id：`{state.get('run_id')}`")
    lines.append(f"- 状态：`{status}`")
    lines.append(f"- 迭代轮次：{iteration} / {state.get('max_iterations', 3)}")
    if state.get("early_stop_reason"):
        lines.append(f"- 提前终止：{state['early_stop_reason']}")
    lines.append(f"- 目标模型：`{state.get('target_model')}`")
    lines.append(f"- LLM 调用次数：{state.get('llm_calls', 0)}")
    lines.append("")

    lines.append("## 评分总览")
    lines.append("")
    if agg:
        lines.append("| 指标 | 值 |")
        lines.append("|---|---|")
        lines.append(f"| 平均分 | {agg.get('avg_score')} |")
        lines.append(f"| 最低分 | {agg.get('min_score')} |")
        lines.append(f"| 最高分 | {agg.get('max_score')} |")
        lines.append(f"| 用例数 | {agg.get('n_cases')}{_cases_short_note(agg)} |")
        lines.append(f"| 通过用例 | {agg.get('n_passed')} |")
        verdict = f"通过（≥{PASS_THRESHOLD}）" if agg.get("passed") else "未达标"
        lines.append(f"| 判定 | {verdict} |")
    else:
        lines.append("_未产生评估结果_")
    lines.append("")

    # ---- 不确定度：点估计之外还要看采样噪声与下界 ----
    if agg:
        lines.append("## 置信度与采样噪声")
        lines.append("")
        lines.append(f"- 每条用例重复采样：{agg.get('n_samples', 1)} 次（`PM_SAMPLES_PER_CASE`）")
        lines.append(
            f"- 用例间标准误差 SEM：{agg.get('sem', 0.0)}；采样噪声（平均极差）：{agg.get('noise', 0.0)}"
        )
        lines.append(
            f"- 均分保守下界：**{agg.get('ci_lower')}**（判定看这个而不是看点估计 {agg.get('avg_score')}）"
        )
        unstable = agg.get("unstable_cases") or []
        if unstable:
            lines.append(f"- ⚠️ 结论不稳的用例：{', '.join('#' + str(i) for i in unstable)}")
        bias = agg.get("judge_bias", 0.0)
        lines.append(
            f"- 评委自报分与代码加权分的平均偏差：{bias}"
            + (
                "（⚠️ 系统性偏高，评委在放水，建议换评委家族或降温）"
                if agg.get("judge_bias_warning")
                else ""
            )
        )
        lines.append("")

    # ---- 基线对比：没有参照点，“分数很高”本身不构成结论 ----
    base = state.get("baseline_aggregate") or {}
    pw = state.get("pairwise") or {}
    if base:
        d_avg = round(
            float(agg.get("avg_score", 0.0) or 0.0) - float(base.get("avg_score", 0.0) or 0.0), 2
        )
        d_min = round(
            float(agg.get("min_score", 0.0) or 0.0) - float(base.get("min_score", 0.0) or 0.0), 2
        )
        lines.append("## 与基线对比（原始需求直喂 target，同口径采样与评分）")
        lines.append("")
        lines.append("| 指标 | 基线 | 优化后 | Δ |")
        lines.append("|---|---|---|---|")
        lines.append(f"| 平均分 | {base.get('avg_score')} | {agg.get('avg_score')} | {d_avg:+} |")
        lines.append(f"| 最低分 | {base.get('min_score')} | {agg.get('min_score')} | {d_min:+} |")
        lines.append(
            f"| 断言未通过 | {base.get('n_assertions_failed', 0)}/{base.get('n_assertions', 0)} "
            f"| {agg.get('n_assertions_failed', 0)}/{agg.get('n_assertions', 0)} | - |"
        )
        lines.append("")
        if d_avg <= 0:
            lines.append(
                "> ⚠️ 本轮优化相对基线**没有正向提升**。流水线跑得通不等于创造了价值，"
                "建议人工比对两份输出或提高 PM_SAMPLES_PER_CASE 后重跑。"
            )
            lines.append("")
    elif not pw:
        lines.append("## 与基线对比")
        lines.append("")
        lines.append("_未跑基线（PM_BASELINE=0 或基线失败）——因此无法回答“比不优化好多少”。_")
        lines.append("")

    if pw:
        votes = pw.get("votes") or {}
        verdict_text = {
            "better": "优化版多数胜出",
            "worse": "**基线多数胜出（优化未带来优势）**",
            "tie": "两份持平",
            "no_signal": "无有效信号（成对比较全部失败）",
        }.get(str(pw.get("verdict")), str(pw.get("verdict")))
        lines.append("## 成对盲评（优化版 vs 基线）")
        lines.append("")
        lines.append(f"- 结论：{verdict_text}")
        lines.append(
            f"- 投票：优化版胜 {votes.get('better', 0)} / 基线胜 {votes.get('worse', 0)} "
            f"/ 持平 {votes.get('tie', 0)}（共 {pw.get('n_compared', 0)} 例，A/B 已随机映射）"
        )
        if pw.get("conflict"):
            lines.append(f"- ⚠️ 结论冲突：{pw['conflict']}")
        for d in (pw.get("details") or [])[:6]:
            lines.append(
                f"  - case#{d.get('test_case_index')}：{d.get('reason') or '（未给依据）'}"
            )
        lines.append("")

    if len(versions) > 1:
        lines.append("## 版本迭代")
        lines.append("")
        lines.append("| 版本 | 平均分 | 最低分 | 说明 |")
        lines.append("|---|---|---|---|")
        for v in versions:
            lines.append(
                f"| v{v['iteration']} | {v.get('avg_score', '-')} | "
                f"{v.get('min_score', '-')} | {v.get('note', '')} |"
            )
        lines.append("")

    unresolved = state.get("unresolved_questions", []) or []
    if unresolved:
        lines.append("## 需求侧遗留问题")
        lines.append("")
        for q in unresolved:
            lines.append(f"- {q}")
        lines.append("")

    if agg and agg.get("all_issues"):
        lines.append("## 未解决的输出问题")
        lines.append("")
        for i in agg["all_issues"][:20]:
            lines.append(f"- {i}")
        lines.append("")

    if state.get("errors"):
        lines.append("## 运行期错误")
        lines.append("")
        for e in state["errors"][:20]:
            lines.append(f"- `{e}`")
        lines.append("")
    # 事实断言（ground-truth）结果：确定性校验的逐条对错，评委分之外的硬证据
    assert_runs = [
        (r, r["assertion"])
        for r in state.get("test_runs", [])
        if isinstance(r.get("assertion"), dict)
    ]
    if assert_runs:
        lines.append("## 事实断言（ground-truth 校验）")
        lines.append("")
        lines.append("| 用例 | 模式 | 结果 | 说明 |")
        lines.append("|---|---|---|---|")
        for r, a in assert_runs:
            lines.append(
                f"| case#{r.get('test_case_index', '?')} | {a.get('mode', '-')} "
                f"| {'✅ 通过' if a.get('passed') else '❌ 未通过'} | {a.get('detail', '')} |"
            )
        lines.append("")

    # 代码侧质量门警告（元话语/上下文泄漏，评估时已注入核查）
    q_issues = state.get("prompt_quality_issues", []) or []
    if q_issues:
        lines.append("## 提示词质量警告（代码侧规则检测）")
        lines.append("")
        for qi in q_issues:
            lines.append(f"- v{qi.get('iteration', '?')}：{qi.get('issues', '')}")
        lines.append("")

    lines.append(f"## 最终提示词（{best_note}）")
    lines.append("")
    lines.append("```text")
    lines.append(best.get("prompt", ""))
    lines.append("```")
    lines.append("")

    report = "\n".join(lines)
    return _apply(
        state,
        node,
        {"final_report": report, "prompt": best.get("prompt", state.get("prompt", ""))},
        "report_done",
        status=status,
        best_iteration=best.get("iteration"),
    )


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, default=str)
