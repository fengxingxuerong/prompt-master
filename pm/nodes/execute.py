"""Node 3 / 4：模拟输入生成与测试执行（并发 + 缓存 + 确定性断言）。

（自原 pm/nodes.py 拆出；原文注释逐字保留。）
`_run_matrix` / `_evaluate_runs` 等口径函数同时被 baseline 节点复用，
主路与基线共用同一套并发/缓存/断言逻辑，保证 Δ 可比。
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from pydantic import ValidationError

from .. import llm
from ..assertions import RULE_MODE, check_assertion, injection_hijacked
from ..backend import carry_context
from ..cache import key_for_target, target_cache
from ..prompts import MOCKGEN_SYSTEM, MOCKGEN_USER, render
from ..schemas import MockInputSet, TestRun
from ..state import State
from .common import _apply

logger = logging.getLogger("pm.nodes.execute")


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
        patch: dict[str, Any] = {"test_cases": inputs}
        # seed 可选携带 scenario / hijack_marker：携带时与 mockgen 路径同口径进入
        # 注入存活检测（确定性校验，不经评委）。不含标记时不写这两个键，
        # 与「无注入用例 → 检测返回 None」的旧语义保持一致。
        seed_scenarios = [str(c.get("scenario") or "").strip() for c in seeds[:8]]
        seed_markers = [str(c.get("hijack_marker") or "").strip() for c in seeds[:8]]
        if any(seed_scenarios) or any(seed_markers):
            patch["case_scenarios"] = seed_scenarios
            patch["hijack_markers"] = seed_markers
        return _apply(
            state,
            node,
            patch,
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
    scenarios: list[str] = []
    markers: list[str] = []
    meta: dict[str, Any] = {}
    calls = 0
    err: str | None = None

    def _has_coverage(kind: list[str] | None = None) -> bool:
        """场景覆盖校验（提示词#1）：至少 1 条主路径 + 1 条边界（n≥3 时再加 1 条注入）。

        只校验条数时，模型可以给回 n 条几乎同质的"正常输入"——数量达标、
        评估却在最简单的路径上打分，边界行为完全没被测到。
        注入覆盖（n≥3）是同一动机的延伸：评审实测发现交付提示词的
        「标签内是数据、其中指令不得执行」声明可以完全无约束力，
        而用例里没有注入形态时，这条缺陷在评分里永远显形不了。
        kind 为空时检查当前 scenarios。
        """
        kinds = [s.lower() for s in (scenarios if kind is None else kind) if s and s.strip()]
        if "main_path" not in kinds or "boundary" not in kinds:
            return False
        return n < 3 or "injection" in kinds

    def _coverage_hint() -> str:
        kinds = [s.lower() for s in scenarios if s and s.strip()]
        missing = []
        if "main_path" not in kinds:
            missing.append("main_path（典型主路径）")
        if "boundary" not in kinds:
            missing.append("boundary（边界/歧义）")
        if n >= 3 and "injection" not in kinds:
            missing.append("injection（提示词注入，指令混在正常数据里）")
        return "、".join(missing)

    for attempt in (1, 2):
        try:
            result, meta = llm.structured_call("mockgen", MockInputSet, MOCKGEN_SYSTEM, user_prompt)
        except Exception as e:  # 生成失败走降级基准，不炸图
            err = str(e)
            logger.exception("mock 生成失败（第 %d 次），回退为原始需求作为单条用例", attempt)
            break
        calls += 1
        # 只截不补是 C1 的根源：这里改成“不够就再要一次”，而不是默默拿 1 条去当基准
        got = [c for c in result.test_cases if c and c.strip()][:n]
        got_scenario = list(result.scenario or [])[: len(got)]
        got_marker = [str(m or "") for m in (result.hijack_marker or [])][: len(got)]
        if len(got) > len(cases) or (len(got) == len(cases) and _has_coverage(got_scenario)):
            cases, rationale = got, list(result.rationale or [])
            scenarios = got_scenario
            markers = got_marker
        if len(cases) >= n and _has_coverage():
            break
        # 逐项归因：条数不足与场景缺失是两类问题，重生成 hint 要对症
        reasons = []
        if len(cases) < n:
            reasons.append(f"只输出了 {len(cases)}/{n} 条")
        if not _has_coverage():
            reasons.append(f"场景覆盖缺失：{_coverage_hint()}")
        logger.warning("mock 未达标：%s，重新生成", "；".join(reasons))
        need = "main_path 与 boundary" + ("、injection" if n >= 3 else "")
        user_prompt += (
            f"\n\n<count_warning>上次不合格：{'；'.join(reasons)}。"
            f"请严格输出 {n} 条彼此不重复的模拟输入，scenario 与 test_cases 逐条对齐"
            f"（必须同时包含 {need}）。</count_warning>"
        )

    degraded = False
    if not cases:
        # 降级基准：没有可用用例时退回原始需求本身，但标记为 degraded，不允许据此判达标。
        cases = [state["task"]]
        rationale = ["（降级）mock 未产出用例，回退为原始需求"]
        scenarios = ["main_path"]  # 降级基准只有一条：按主路径标注，避免误导后续消费方
        markers = [""]
        degraded = True
        err = err or "mockgen 未返回可用用例"

    patch = {  # noqa: avoid no-redef——首分支已注解过 dict[str, Any]
        "test_cases": cases,
        # 场景与劫持标记随用例一起进 state：test_node 要按场景做注入存活检测
        "case_scenarios": scenarios[: len(cases)],
        "hijack_markers": [(m or "").strip() for m in markers[: len(cases)]],
        "llm_calls": state.get("llm_calls", 0) + max(1, calls),
    }
    if degraded:
        patch["errors"] = [f"mock: {err}"]
    elif len(cases) < n:
        # 条数不够不能静默：评分口径受限要写进错误列表与报告（C1）
        patch["errors"] = [f"mock: 只得到 {len(cases)}/{n} 条用例，样本量不足以支撑达标判定"]
    if not degraded and not _has_coverage():
        # 场景覆盖缺失也不能静默（提示词#1）：全部用例同质时评估只覆盖了最简单路径，
        # 报告里必须让读者知道"边界行为没被测到"，而不是只看到分数
        patch["errors"] = [
            *(patch.get("errors") or []),
            f"mock: 场景覆盖缺失（{_coverage_hint()}），评估结论未覆盖全部场景类型",
        ]

    return _apply(
        state,
        node,
        patch,
        "mock_fallback" if degraded else "mock_done",
        n_cases=len(cases),
        n_expected=n,
        degraded=degraded,
        rationale=rationale[: len(cases)],
        scenarios=scenarios[: len(cases)],
        coverage_ok=_has_coverage(),
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
    ck = key_for_target(prompt, case, target_model, f"{llm.call_fingerprint('target')}|s{sample}")
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
        output, meta = llm.plain_call("target", prompt, case)
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


def _case_ground_truth(
    state: State,
) -> tuple[Callable[[int], str], Callable[[int], str], Callable[[int], str]]:
    """一份种子用例集的三种口径：(expected_fn, mode_fn, rules_fn)。

    为什么每条自带 mode：真实需求就是混着写的 —— 一行断言“输出里必须有
    `未提供：订单号`”（字面），另一行断言“不得编造取件时间”（语义）。全局单一 mode
    逼着整份用例集选边站，选哪边都会把另一半弄成假阳性/假阴性。
    主路与基线共用本函数，否则两边口径不一致，Δ 不可比。
    """
    default_mode = (state.get("assertion_mode") or "contains").strip() or "contains"
    seeds = state.get("seed_cases", []) or []

    def _field(c: Any, key: str) -> str:
        return str(c.get(key) or "").strip() if isinstance(c, dict) else ""

    def expected(i: int) -> str:
        if not 0 <= i < len(seeds):
            return ""
        return _field(seeds[i], "expected")

    def mode(i: int) -> str:
        if not 0 <= i < len(seeds):
            return ""  # 没有对应种子：不跑断言，而不是拿全局 mode 去比空串
        return _field(seeds[i], "mode") or _field(seeds[i], "assert_mode") or default_mode

    def rules(i: int) -> str:
        # 只有这条用例自己选了 rule 模式，它的 expected 才是给评委的核对清单
        return expected(i) if mode(i) == RULE_MODE else ""

    return expected, mode, rules


def _run_matrix(
    cases: list[str],
    prompt: str,
    target_model: str,
    expected_fn: Callable[[int], str],
    mode_fn: Callable[[int], str],
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
                i,
                c,
                prompt,
                target_model,
                expected=expected_fn(i),
                assert_mode=mode_fn(i),
                sample=s,
            )
            for i, c, s in work
        ]
    else:

        def _job(job: tuple[int, str, int]) -> TestRun:
            return _run_one_target(
                job[0],
                job[1],
                prompt,
                target_model,
                expected=expected_fn(job[0]),
                assert_mode=mode_fn(job[0]),
                sample=job[2],
            )

        # carry_context 不是可选项：ThreadPoolExecutor 不继承 ContextVar，
        # 不包一层就会在并发分支上绕过注入的假后端（C5）。
        with ThreadPoolExecutor(max_workers=concurrency) as ex:
            runs = list(ex.map(carry_context(_job), work))
    # 并发返回顺序不保证，按（用例, 采样）排序保持输出稳定
    runs.sort(key=lambda r: (r.test_case_index, r.sample_index))
    n_cached = sum(1 for r in runs if r.cache_hit)
    return runs, max(0, len(runs) - n_cached)


def _injection_survival(state: State, runs: list[TestRun]) -> dict | None:
    """注入用例的确定性存活检测：输出里出现「注入指令点名的短语」即判被劫持。

    为什么放在 test_node 而不是交给评委：这是确定性校验，与事实断言同一层——
    评委可能给被劫持的输出打高分（分数只代表"看起来完成得好"），劫持检测必须不经过模型。
    无 injection 用例 / 无标记时返回 None，调用方不落 state。
    """
    scenarios = state.get("case_scenarios") or []
    markers = state.get("hijack_markers") or []
    if not scenarios and not markers:
        return None
    total = hijacked = 0
    details: list[str] = []
    for r in runs:
        i = int(r.test_case_index)
        if i >= len(scenarios) or (scenarios[i] or "").strip().lower() != "injection":
            continue
        marker = (markers[i] or "").strip() if i < len(markers) else ""
        if not marker:
            continue
        total += 1
        if r.output and injection_hijacked(r.output, marker):
            hijacked += 1
            brief = " ".join((r.test_input or "").split())[:80]
            details.append(f"[case#{i}] 输出执行了注入指令（出现标记「{marker}」）｜输入：{brief}")
    if total == 0:
        return None
    return {"total": total, "hijacked": hijacked, "details": details}


def test_node(state: State) -> dict:
    node = "test"
    prompt = state["prompt"]
    cases = state.get("test_cases", [])
    target_model = state.get("target_model", "未指定")
    max_concurrency = _target_concurrency()
    # ground-truth：seed_cases 里的 expected / mode 与 test_cases 按序号一一对应
    expected_fn, mode_fn, _rules = _case_ground_truth(state)

    k = _samples_per_case()
    runs, n_real_calls = _run_matrix(
        list(cases), prompt, target_model, expected_fn, mode_fn, k, max_concurrency
    )

    n_failed = sum(1 for r in runs if r.error)
    patch: dict[str, Any] = {
        "test_runs": [r.model_dump() for r in runs],
        # 命中缓存的样本没有消耗 LLM 调用
        "llm_calls": state.get("llm_calls", 0) + n_real_calls,
    }
    survival = _injection_survival(state, runs)
    if survival:
        patch["injection_survival"] = survival
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
