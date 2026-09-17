"""Node 6b / 6c：基线对照与成对盲评（回答"到底有没有变好"）。

（自原 pm/nodes.py 拆出；原文注释逐字保留。）
复用 execute 的执行口径与 judge 的评分口径，保证 Δ 可比。
"""

from __future__ import annotations

import logging
import random
import re
from typing import Any

from .. import llm
from ..prompts import COMPARATOR_SYSTEM, COMPARATOR_USER, render
from ..schemas import AggregateScore, EvaluationResult, PreferenceResult
from ..state import State
from .common import _apply, _feature_enabled
from .execute import (
    _case_ground_truth,
    _run_matrix,
    _samples_per_case,
    _target_concurrency,
)
from .judge import (
    _active_judges,
    _collect_assertions,
    _evaluate_runs,
    _judge_spec,
    _merge_rule_verdicts,
)

logger = logging.getLogger("pm.nodes.baseline")


def baseline_node(state: State) -> dict[str, Any]:
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
    # 与主路共用同一个口径函数：expected / mode / rules 三者必须逐条一致，否则 Δ 不可比
    _expected, _mode, rules_fn = _case_ground_truth(state)

    judges = _active_judges()
    judge_spec = _judge_spec(judges)
    # 基线用与主路完全相同的采样次数与评分口径，否则 Δ 不可比
    k = _samples_per_case()
    try:
        runs, n_calls = _run_matrix(
            cases, task, target_model, _expected, _mode, k, _target_concurrency()
        )
        evals_raw, errors, _, n_eval_calls = _evaluate_runs(
            [r.model_dump() for r in runs],
            task=task,
            context=context,
            prompt=task,  # 基线的"提示词"就是原始需求本身
            judges=judges,
            judge_spec=judge_spec,
            q_warns=[],
            rules_fn=rules_fn,
        )
    except Exception as e:  # noqa: BLE001 - 基线失败不应阻断主流程
        logger.warning("基线跑失败（不影响主流程）：%s", e)
        return _apply(state, node, {"errors": [f"baseline: {e}"]}, "baseline_failed", error=str(e))

    evals = [EvaluationResult.model_validate(e) for e in evals_raw]
    agg = AggregateScore.from_evaluations(
        evals,
        n_expected=int(state.get("n_test_cases", 0) or 0),
        assertions=_merge_rule_verdicts(_collect_assertions([r.model_dump() for r in runs]), evals),
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


# 保守枚举标记：出现越多，输出越接近「逐项枚举缺失/无法分析」的保守风格
# （历史真实运行发现 comparator 偏好这类输出，与 pointwise 偏好具体产出冲突）
_CONSERVATIVE_MARKERS = (
    "数据缺失",
    "未提供",
    "无法确定",
    "无法判断",
    "信息不足",
    "无法解析",
    "不足以支持",
)
# 具体数值引用：带计量单位的数字（万/千/亿/百分比），是「按目标产出具体结论」的风格信号
_DATA_POINT_RE = re.compile(r"\d+(?:\.\d+)?\s*(?:万|千|亿|%|％)")


def _style_stats(text: str) -> dict[str, int]:
    """对单份输出做确定性风格统计。零 LLM、可单测。"""
    return {
        "chars": len(text),
        "conservative_markers": sum(text.count(m) for m in _CONSERVATIVE_MARKERS),
        "data_points": len(_DATA_POINT_RE.findall(text)),
    }


def _conflict_attribution(
    base: dict[int, dict[str, Any]], cur: dict[int, dict[str, Any]]
) -> dict[str, Any]:
    """对冲突双方的代表输出做风格统计，并给出归因假设（供人工裁定参考，不自动裁定）。"""
    b_txt = "\n".join(str(r.get("output", "")) for r in base.values())
    c_txt = "\n".join(str(r.get("output", "")) for r in cur.values())
    b, c = _style_stats(b_txt), _style_stats(c_txt)

    if c["conservative_markers"] > b["conservative_markers"]:
        hypothesis = (
            "优化版比基线**更保守**（枚举缺失标记更多）——冲突可能是真实质量回退的信号，"
            "建议优先人工核查优化版输出是否过度回避任务"
        )
    elif (
        b["conservative_markers"] > c["conservative_markers"]
        and c["data_points"] >= b["data_points"]
    ):
        hypothesis = (
            "与「评委偏好保守逐项枚举」假设一致：基线更保守（枚举缺失多），"
            "优化版更具体（数值引用多）——盲评偏好前者不代表优化版更差，"
            "建议人工以产出价值优先裁定"
        )
    else:
        hypothesis = "风格统计无法区分两侧（枚举/数值信号持平或方向不明），建议人工逐例比对盲评依据"

    return {
        "base": b,
        "cur": c,
        "hypothesis": hypothesis,
    }


def compare_node(state: State) -> dict[str, Any]:
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
            pref, _meta = llm.structured_call(
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

    # 冲突自动归因（确定性统计，不经评委）：历史真实运行发现 comparator 偏好
    # 「保守、逐项枚举缺失」的输出，而优化版按目标产出具体结论——两个信号打架时
    # 先用风格统计验证这个假设，给人工裁定一个起点而不是一句"自己比吧"。
    attribution = _conflict_attribution(base, cur) if conflict else None

    logger.info(
        "成对盲评：优化版胜 %d / 基线胜 %d / 持平 %d → %s%s",
        votes["better"],
        votes["worse"],
        votes["tie"],
        verdict,
        f"（冲突：{conflict}）" if conflict else "",
    )
    pairwise: dict[str, Any] = {
        "verdict": verdict,
        "votes": votes,
        "n_compared": len(details),
        "conflict": conflict,
        "details": details,
    }
    if attribution is not None:
        pairwise["attribution"] = attribution
    return _apply(
        state,
        node,
        {
            "pairwise": pairwise,
            "llm_calls": state.get("llm_calls", 0) + n_calls,
            **({"errors": errors} if errors else {}),
        },
        "compare_done",
        verdict=verdict,
        votes=votes,
        n_compared=len(details),
        conflict=conflict or None,
    )
