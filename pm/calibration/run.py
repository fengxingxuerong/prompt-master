"""单轮校准的执行：真调评委、算逐条 Δ。

2026-10-19 从 `pm/calibration.py` 拆出。这是本包里唯一会发起真实 LLM 调用的部分。
"""

from __future__ import annotations

import statistics
from typing import Any

from .. import backend
from ..llm import structured_call
from ..prompts import EVALUATOR_SYSTEM, EVALUATOR_USER, render
from ..schemas import EvaluationResult, judge_disagreement_threshold
from .stats import analyze


# --------------------------------------------------------------------------
# 评估（与主管道同口径）
# --------------------------------------------------------------------------
def evaluate_sample(
    role: str, sample: dict[str, Any], mode: str = "impression"
) -> EvaluationResult:
    """用与 pm.nodes._evaluate_one 相同的提示词渲染 + schema 调用真实评委。

    校准不另写评分提示词——否则校准的就不再是线上跑的那台评委。
    `mode="checklist"` 走判定式协议（同一份渲染前缀 + `PM_SCORING_MODE` 那套算分），
    两种协议共用一条入口正是 A/B 测量要的：同输入、同模型、只换协议。
    """
    user = render(
        EVALUATOR_USER,
        original_task=sample["original_task"],
        context=sample.get("context") or "（无）",
        prompt=sample["prompt"],
        test_input=sample["test_input"],
        test_output=sample["test_output"],
    )
    if mode == "checklist":
        return _evaluate_checklist(role, sample, user)
    ev, _meta = structured_call(role, EvaluationResult, EVALUATOR_SYSTEM, user)
    return ev.finalize()


def _evaluate_checklist(role: str, sample: dict[str, Any], base_user: str) -> EvaluationResult:
    """判定式协议跑一条锚点：清单与数字候选的拼法、算分全部走 `pm.scoring` 那一份实现。

    不在这里重复拼清单：主管道用的是同一份 `scoring.prepare`，两份实现迟早会长得不一样，
    那时候 A/B 测的就不是"将来会上线的那条路径"了。
    """
    from pm.scoring import evaluate_with_checklist, prepare

    prep = prepare(
        sample["prompt"], sample["original_task"], sample["test_input"], sample["test_output"]
    )
    if prep is None:
        raise ValueError("被测提示词的标签段摊不出够用的清单，判定式对这条不适用")
    checklist, numbers, block = prep
    ev, _meta = evaluate_with_checklist(role, base_user + block, checklist, numbers)
    return ev


# --------------------------------------------------------------------------
# 校准主流程（与 CLI 分离，便于测试注入假评委）
# --------------------------------------------------------------------------
def calibrate(
    samples: list[dict[str, Any]], role: str, mode: str = "impression"
) -> tuple[dict[str, Any], list[tuple[str, str]]]:
    """逐条评估并聚合。返回 (分析结果, 失败锚点列表)。"""
    # 延迟从**包命名空间**取（而不是本模块里的 `evaluate_sample`）：本仓的测试与排障
    # 一律补 `pm.calibration.evaluate_sample`，拆包后模块内的直接引用会让补丁静默失效
    # —— 测试变绿、改的却是另一份函数。原单文件时代两者是同一个命名空间，没有这个坑。
    from . import evaluate_sample as _eval

    ok_samples: list[dict[str, Any]] = []
    ok_scores: list[float] = []
    errors: list[tuple[str, str]] = []
    for s in samples:
        sid = str(s.get("id") or "?")
        try:
            ev = _eval(role, s, mode)
            ok_samples.append(s)
            ok_scores.append(ev.weighted_score)
        except Exception as e:  # noqa: BLE001 - 单锚点失败不中断校准
            errors.append((sid, f"{type(e).__name__}: {e}"))
    if not ok_samples:
        return {}, errors
    analysis = analyze(ok_samples, ok_scores)
    # 失败清单挂进 analysis：报告里要说清"少的那几条是谁、为什么"，而不只报一个变小的 n。
    # （返回值里本来就有 errors 元组，但报告渲染只拿到 analysis。）
    analysis["n_failed"] = len(errors)
    analysis["failed"] = [{"id": sid, "error": err} for sid, err in errors]
    return analysis, errors


# --------------------------------------------------------------------------
# 复现性：评委跟**自己**比（不与人工比）
# --------------------------------------------------------------------------
def repeatability(
    samples: list[dict[str, Any]], role: str, times: int = 3, mode: str = "impression"
) -> dict[str, Any]:
    """同一份渲染连续打 `times` 次，量极差。绕开评估缓存——否则第二次永远命中，
    测出来的极差恒为 0，那是自欺而不是"很稳定"。

    为什么要单列一个指标：bias / MAE / r 全是"与人工比"，一个每次调用在不同口径之间
    跳变的模型在这些数字里可以看着完全正常（抖动甚至会把 MAE 摊平）。而双评委分差阈值、
    仲裁触发率、Δ 的噪声带，全都隐含假设"同一个输入会给同一个分"。
    实测（2026-09-19，真端点）：评委 A 同内容 3 次极差 0.25~0.70，评委 B 1.30~2.60，
    仲裁一次给 4.45、一次给 8.88（极差 4.43）；**把 temperature 降到 0 并不改善**。
    结论是这类抖动是"选中哪套口径"的双峰，不是围绕真值的噪声——median-of-3 救不了。
    """
    from . import evaluate_sample as _eval

    per_item: list[dict[str, Any]] = []
    with backend.with_cache_disabled():
        for s in samples:
            sid = str(s.get("id") or "?")
            scores: list[float] = []
            err = ""
            for _ in range(max(1, times)):
                try:
                    scores.append(float(_eval(role, s, mode).weighted_score))
                except Exception as e:  # noqa: BLE001 - 单条失败不影响整体测量
                    err = f"{type(e).__name__}: {e}"
                    break
            if len(scores) < 2:
                per_item.append({"id": sid, "n": len(scores), "error": err or "样本不足"})
                continue
            per_item.append(
                {
                    "id": sid,
                    "n": len(scores),
                    "scores": scores,
                    "range": round(max(scores) - min(scores), 2),
                    "median": round(statistics.median(scores), 2),
                }
            )
    ok = [x for x in per_item if "range" in x]
    if not ok:
        return {"role": role, "times": times, "n_items": 0, "per_item": per_item}
    ranges = [x["range"] for x in ok]
    worst = max(ok, key=lambda x: x["range"])
    return {
        "role": role,
        "times": times,
        "n_items": len(ok),
        "range_mean": round(sum(ranges) / len(ranges), 2),
        "range_max": max(ranges),
        "worst_id": worst["id"],
        # 判定阈值的含义：分差阈值小于这个数时，"双评委分歧"主要在读评委自己的抖动
        "disagreement_threshold": judge_disagreement_threshold(),
        "threshold_exceeded": max(ranges) > judge_disagreement_threshold(),
        "per_item": per_item,
    }
