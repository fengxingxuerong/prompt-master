"""Node 5：评估（双评委交叉验证 + 仲裁 + 结果缓存 + 聚合）。

（自原 pm/nodes.py 拆出；原文注释逐字保留。）
`_evaluate_runs` 同时被 baseline 节点复用，保证主路与基线评分口径一致。
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from typing import Any

from pydantic import ValidationError

from .. import llm
from ..assertions import RULE_MODE
from ..cache import eval_cache, key_for_eval
from ..prompts import EVALUATOR_RULES, EVALUATOR_SYSTEM, EVALUATOR_USER, render
from ..quality import evaluator_warning_block
from ..schemas import (
    PASS_THRESHOLD,
    AggregateScore,
    DimensionScores,
    EvaluationResult,
    RuleCheck,
    early_stop_reason,
)
from ..state import State
from .common import _apply
from .execute import _case_ground_truth

logger = logging.getLogger("pm.nodes.judge")


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
            parts.append(f"{j}:{llm.build_config(j).model}")
        except Exception:  # noqa: BLE001 - 配置读取失败不影响缓存键构造
            parts.append(j)
    return "+".join(parts)


def _call_evaluator(
    judge: str, user_prompt: str, idx: int
) -> tuple[EvaluationResult, dict[str, Any]]:
    ev, meta = llm.structured_call(judge, EvaluationResult, EVALUATOR_SYSTEM, user_prompt)
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


def _merge_rule_checks(ev_a: EvaluationResult, ev_b: EvaluationResult) -> list[RuleCheck]:
    """保守合并两位评委的规则判定：同一规则只要有一侧判未满足，就按未满足算。

    按规则原文对齐（两位评委拿到的是同一份清单）；只有一侧给出判定时取那一侧。
    """
    out: list[RuleCheck] = []
    seen: dict[str, RuleCheck] = {}
    for src in (ev_a, ev_b):
        for c in src.rule_checks or []:
            key = c.rule.strip()
            prev = seen.get(key)
            if prev is None:
                copy = c.model_copy()
                seen[key] = copy
                out.append(copy)
            elif prev.satisfied and not c.satisfied:
                prev.satisfied = False
                prev.evidence = c.evidence or prev.evidence
    return out


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
            # 规则判定必须跟着合并结果走：不带的话，双评委（无分歧）时两位的 rule_checks
            # 一起被丢掉，报告里那一行凭空消失——只有触发仲裁才碰巧留下（真实跑发现）。
            rule_checks=_merge_rule_checks(ev_a, ev_b),
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


def _rule_veto_enabled() -> bool:
    """`PM_RULE_VETO=1` 时，评委判定的“规则未满足”也一票否决（默认只提醒）。

    默认不否决是因为：评委判定本身带噪声，拿它否决等于把主观判断伪装成客观事实；
    而确定性断言（contains/exact/regex/custom）才是硬证据。想要硬约束就显式开。
    """
    return (os.getenv("PM_RULE_VETO") or "").strip().lower() in {"1", "true", "yes", "on"}


def _rules_for_case(state: State) -> Callable[[int], str]:
    """给评委的核对清单：仅当**该条用例自己**选了 rule 模式。

    为什么需要：真实跑里人写的 expected 九成是“必须标注缺失项”这种规则，
    拿它去跑 contains 只会永远命不中，基线与优化版一起被打死，对比失去意义。
    口径与主路/基线共用 `_case_ground_truth`，避免两边对同一条用例理解不一致。
    """
    return _case_ground_truth(state)[2]


def _merge_rule_verdicts(
    assertions: dict[int, dict[str, Any]], evals: list[EvaluationResult]
) -> dict[int, dict[str, Any]]:
    """把评委的 rule_checks 折进断言视图：一个用例一条，任一规则不满足即视为未通过。

    已有确定性断言的用例不被覆盖（硬证据优先）。默认标为 advisory：只提醒不否决。
    """
    veto = _rule_veto_enabled()
    for ev in evals:
        checks = list(getattr(ev, "rule_checks", None) or [])
        if not checks:
            continue
        idx = int(ev.test_case_index)
        if idx in assertions:
            continue
        bad = [c for c in checks if not c.satisfied]
        assertions[idx] = {
            "mode": RULE_MODE,
            "passed": not bad,
            "advisory": not veto,
            "advisory_kind": "semantic",
            "detail": (
                "；".join(f"「{c.rule[:40]}」未满足：{c.evidence or '未找到证据'}" for c in bad[:3])
                if bad
                else f"{len(checks)} 条规则全部满足"
            ),
            "expected": "；".join(c.rule for c in checks)[:600],
            "output_excerpt": "",
            "score": 1.0 if not bad else 0.0,
        }
    return assertions


def _min_dims() -> DimensionScores:
    return DimensionScores(
        task_completion=1,
        format_adherence=1,
        constraint_compliance=1,
        robustness=1,
        quality=1,
    )


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
    rules: str = "",
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
    if rules:
        # 规则模式：把 expected 当核对清单（不是字面片段）交给评委逐条核验
        user_prompt += render(EVALUATOR_RULES, rules=rules)

    # 1) 命中缓存直接复用（断点续跑 / 相同输出重复评估）
    cache = eval_cache()
    # 键里带上原始需求 / 上下文 / 质量警告：不同 task 不能串用同一份评估结果（H1）
    extra = f"{task}\n{context}\n{'|'.join(q_warns)}"
    if rules:
        # 规则变了评估结论就会变：不进键就会拿到旧清单的判定
        extra += f"\n<RULES>{rules}"
    ck = key_for_eval(
        prompt,
        run["test_input"],
        run["output"],
        judge_spec,
        extra=extra,
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
    rules_fn: Callable[[int], str] | None = None,
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
            rules=rules_fn(int(run.get("test_case_index", 0) or 0)) if rules_fn else "",
        )
        n_llm_calls += calls
        n_cache_hit += 1 if hit else 0
        raw.append(ev)
    return _collapse_samples(raw), errors, n_cache_hit, n_llm_calls


def _safe_int(value: Any) -> int:
    """尽力取非负整数，取不到就是 0（用于来自 state 的、类型不可信的字段）。"""
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _is_empty_output_eval(evaluation: dict[str, Any]) -> bool:
    """该评估结果是否来自"目标模型返回空内容"（无效样本，非真实低分）。

    标记由 `pm/nodes/execute.py` 写入：空输出时给 TestRun.error="empty_output"，
    评分层沿用"调用失败"分支，把原因写进 issues。
    """
    return any("empty_output" in str(i) for i in (evaluation.get("issues") or []))


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
        # 2026-09-16：空输出（推理耗尽预算，未抛异常）不是"1 分的真实结果"，
        # 它是**无效样本**。此前这类样本被计入均值，把基线系统性压低 → Δ 被夸大
        # （矩阵 A 实测：基线 8 条里 4 条空输出，Δ=+6.02 含此偏差）。
        # 只要该用例还有有效采样，就只聚合有效样本；全为空时才保留（否则用例会凭空消失）。
        valid = [g for g in group if not _is_empty_output_eval(g)]
        if valid:
            group = valid
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


def _hard_failures(state: Any) -> list[str]:
    """确定性校验的失败项（事实断言 / 注入存活）——比评委分数更硬，必须优先修。

    为什么单独拎出来：评委分是"模型觉得好不好"，断言与劫持检测是"事实对不对"。
    真实运行里出现过评委给 9 分、而断言期望片段根本没出现在输出里的情况，
    把这些信号混在 issues 里会被一大堆措辞类建议淹没。
    """
    out: list[str] = []
    if not isinstance(state, dict):
        return out
    for r in state.get("test_runs") or []:
        if not isinstance(r, dict):
            continue
        a = r.get("assertion")
        if not isinstance(a, dict) or a.get("passed") or a.get("advisory"):
            continue
        exp = str(a.get("expected") or "")[:80]
        out.append(
            f"[case#{r.get('test_case_index', '?')}] 事实断言未通过（{a.get('mode', '-')}）："
            f"{a.get('detail', '')}｜期望片段：{exp}"
        )
    surv = state.get("injection_survival")
    if isinstance(surv, dict):
        for d in surv.get("details") or []:
            out.append(f"提示词注入得手（确定性检测）：{d}")
    return out


def _build_feedback(agg: AggregateScore, evals: list[EvaluationResult], state: Any = None) -> str:
    """把聚合结果整理成 Reviser 可直接消费的反馈文本。"""
    if not evals:
        # resume / 手工 state 场景下可能空列表，len(vals)==0 会直接除零（A6）
        return "（本轮无评估结果，无法定向修订；请先检查测试集与目标模型调用是否正常。）"

    lines = [f"综合评分：平均 {agg.avg_score}，最低 {agg.min_score}（目标 {PASS_THRESHOLD}）", ""]

    # 硬信号排在软信号前面：确定性失败比"哪个维度分低"更值得先修
    hard = _hard_failures(state)
    if hard:
        lines.append("确定性校验失败（比评委分数更硬，优先修这几条）：")
        for h in hard[:10]:
            lines.append(f"  - {h}")
        lines.append("")

    # 找出最薄弱维度，提示 Reviser 优先修
    from ..schemas import WEIGHTS

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


def _feedback_digest(feedback: str, limit: int = 400) -> str:
    """把上一轮反馈压成摘要，供修订器判断"这个改法是否已经试过"。"""
    text = " ".join((feedback or "").split())
    return text[:limit] + ("…" if len(text) > limit else "")


def evaluate_node(state: State) -> dict[str, Any]:
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
        rules_fn=_rules_for_case(state),
    )
    evals = [EvaluationResult.model_validate(e) for e in evaluations]
    # 事实断言（ground-truth）：从 test_runs 收集断言结果，参与聚合的一票否决
    assertions = _merge_rule_verdicts(_collect_assertions(runs), evals)
    # 规则判定只活在 aggregate 里的话，报告的断言表看不到它（真实跑暴露的）：
    # 回填到对应用例的 assertion 上，让“事实/语义”两张面孔共用同一处呈现。
    rule_only = {k: v for k, v in assertions.items() if v.get("mode") == RULE_MODE}
    patch_runs = bool(rule_only)
    if patch_runs:
        runs = [dict(r) for r in runs]
        for r in runs:
            verdict = rule_only.get(int(r.get("test_case_index", -1)))
            if verdict and not r.get("assertion"):
                r["assertion"] = verdict
    n_rule = sum(1 for a in assertions.values() if a.get("mode") == RULE_MODE)
    if n_rule:
        logger.info(
            "规则核验：%d 条用例走了 <RULES> 清单，未满足 %d 条（否决=%s）",
            n_rule,
            sum(
                1 for a in assertions.values() if a.get("mode") == RULE_MODE and not a.get("passed")
            ),
            _rule_veto_enabled(),
        )
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
    if patch_runs:
        patch["test_runs"] = runs
    if errors:
        patch["errors"] = errors

    # ---- 注入门禁（OpenClaw 方向，2026-09-16）----
    # 注入存活检测此前只渲染进报告，不影响 passed：评委 8+ 分 + 注入被劫持的
    # 版本会以「已达标」身份交付出去。安全缺陷不能用分数赎回——达标即要求
    # 注入用例 0 劫持（没有注入用例时 gate 为 None，不拦截）。
    surv = state.get("injection_survival")
    gate: dict[str, Any] | None = None
    # 类型必须容错：injection_survival 可能来自旧版 checkpoint / 外部构造的 state，
    # 直接 int("abc") 会让整条评估链崩在门禁上（测试实证）。
    if isinstance(surv, dict):
        total, hijacked = _safe_int(surv.get("total")), _safe_int(surv.get("hijacked"))
        if total > 0:
            gate = {"total": total, "hijacked": hijacked}
    # 条件里直接判 gate（而不是用 bool(...) 存成另一个变量）：
    # 类型收窄不跨变量，用 `injection_gate_blocked` 当条件会让 mypy 无法确认
    # gate 非空（原写法 4 处 Optional 索引报错）。逻辑等价，但类型可证。
    injection_gate_blocked = False
    if gate and gate["hijacked"] > 0:
        injection_gate_blocked = True
        patch["injection_gate"] = {"blocked": True, **gate}
        patch["unresolved_questions"] = [
            *list(state.get("unresolved_questions") or []),
            f"注入存活检测未通过（{gate['hijacked']}/{gate['total']} 条被劫持）："
            "达标结论已被门禁压制，修复「标签内数据指令不得执行」的约束后再交付。",
        ]
        logger.warning(
            "注入门禁拦截：%d/%d 条注入用例被劫持，passed 强制为 False",
            gate["hijacked"],
            gate["total"],
        )

    if agg.passed and not injection_gate_blocked:
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
        # 单次采样时极差恒为 0，那不是“没噪声”而是“测不出噪声”：传 None 让余量放宽
        # 并关掉平台期规则（M4）——否则一次运气好的采样就能把修订提前卡死。
        noise = agg.noise if (agg.n_samples or 1) >= 2 else None
        stop_reason = early_stop_reason(avg_scores, noise=noise)

        # 成本闸（PM_MAX_LLM_CALLS）：下一轮修订的预估调用数会突破任务预算 →
        # 立即止损，按 early_stopped 终态交付当前最佳版本。复用既有终态而不是新造
        # status（Agent/CLI/API 的终态集合都不用改），reason 写明是预算而非收敛。
        # used 必须是「本轮评估后的最新用量」：state.llm_calls 还是进节点时的旧值，
        # 本轮刚消耗的 n_llm_calls 不算进去的话，超预算会整整晚一轮才被发现（实测）。
        budget_raw = (os.getenv("PM_MAX_LLM_CALLS") or "").strip()
        if budget_raw and not stop_reason:
            try:
                budget = max(0, int(budget_raw))
                case_ids = {int(r.get("test_case_index", 0)) for r in runs}
                n_cases = len(case_ids) or 1
                n_samples = max(1, len(runs) // n_cases)
                per_iter_est = 1 + n_cases * n_samples * len(judges)
                used = state.get("llm_calls", 0) + n_llm_calls
                if used + per_iter_est > budget:
                    patch["status"] = "early_stopped"
                    patch["should_revise"] = False
                    patch["early_stop_reason"] = (
                        f"调用预算耗尽：已用 {used} 次，下一轮修订预计还需约 "
                        f"{per_iter_est} 次（PM_MAX_LLM_CALLS={budget}）"
                    )
                    logger.warning("预算闸触发：%s", patch["early_stop_reason"])
            except ValueError:
                logger.warning("PM_MAX_LLM_CALLS=%r 不是整数，预算闸忽略", budget_raw)

        if stop_reason:
            patch["status"] = "early_stopped"
            patch["should_revise"] = False
            patch["early_stop_reason"] = stop_reason
        elif patch.get("status") != "early_stopped":
            patch["should_revise"] = True
            patch["revision_feedback"] = _build_feedback(agg, evals, state)

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
        injection_gate_blocked=injection_gate_blocked,
    )
