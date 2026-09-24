#!/usr/bin/env python3
"""评委校准：在锚点样本上对比「评委分」与「人工专家分」，回答"评委打 8 分可信吗"。

为什么需要它：整个闭环的质量上限是评委模型的水位，而"防放水机制"只保证评委
**不虚高**，不回答"它与人类专家的判断差多远"。校准集 = 一批（提示词 × 测试输入 ×
输出 × 人工分）锚点：用与主管道完全相同的评估管线（EVALUATOR_SYSTEM + 同一渲染 +
同一 finalize 加权）跑一遍真实评委，计算偏差与排序一致性——换评委模型、调评分
提示词之后跑一次，"分数可信"就从设计主张变成实测数据。

用法：
    python calibrate_judge.py --write-template   # 从示例生成 samples.json（已存在则跳过）
    python calibrate_judge.py                    # 校准默认评委（evaluator）
    python calibrate_judge.py --judge evaluator_b

流程：
1. 人工给 `judge_calibration/samples.json` 里的每条锚点打 human_score（1-10）；
2. 本工具逐条调用真实评委，得到代码加权分（与主管道同口径）；
3. 输出逐条 Δ、MAE、平均偏差（正=偏松/放水倾向，负=偏严）、Pearson 排序一致性，
   并给对症结论。退出码：0=完成分析（结论好坏都算完成），1=全部评估失败，2=样本问题。

诚实边界：校准告诉你偏差**多大**，不替你提升评委能力；样本 <5 条时数字只有方向性。
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

from pm.bootstrap import ensure_utf8_stdio

ensure_utf8_stdio()

from pm import backend  # noqa: E402
from pm.llm import structured_call  # noqa: E402
from pm.prompts import EVALUATOR_SYSTEM, EVALUATOR_USER, render  # noqa: E402
from pm.schemas import (  # noqa: E402
    PASS_THRESHOLD,
    EvaluationResult,
    judge_disagreement_threshold,
)
DEFAULT_SAMPLES = Path(__file__).parent / "judge_calibration" / "samples.json"
EXAMPLE_SAMPLES = Path(__file__).parent / "judge_calibration" / "samples.example.json"
JUDGE_ROLES = ("evaluator", "evaluator_b", "arbiter")

# 一致性判定阈值（与 schemas.JUDGE_BIAS_ALERT 的默认告警线对齐）
BIAS_ALERT = 1.0  # |平均偏差| 超过该值 → 系统性偏松/偏严
LARGE_DEVIATION = 2.0  # 单条 |Δ| 超过该值 → 点名大偏差样本
CORR_GOOD = 0.8  # Pearson r ≥ 该值 → 排序一致性好
CORR_BAD = 0.5  # r < 该值 → 排序一致性差
MIN_SAMPLES_FOR_R = 3  # 相关系数最少样本数
MIN_SAMPLES_FOR_VERDICT = 5  # 低于该数只给方向性参考

# 下面两个不是"实测出来的经验阈值"，而是**统计量本身在这些条件下无定义或退化**：
# 判定线某一侧一个样本都没有时，一致率恒为 100%、κ 无定义——报数比不报更容易骗人。
MIN_PER_DECISION_SIDE = 1  # 判定线两侧各至少几个样本，判定一致率才有定义
MIN_BANDS_COVERED = 3  # 人工分覆盖的分数带数低于此 → 排序相关是在窄区间里算的

# 与 harvest_anchors.py 用同一套分带，否则"覆盖"这件事两边各说一套。
BANDS: tuple[tuple[str, float, float], ...] = (
    ("<6.0", 0.0, 6.0),
    ("6.0-6.9", 6.0, 7.0),
    ("7.0-7.9", 7.0, 8.0),
    ("8.0-8.9", 8.0, 9.0),
    (">=9.0", 9.0, 10.01),
)

_ROLE_LABEL = {"evaluator": "评委A", "evaluator_b": "评委B", "arbiter": "仲裁评委"}


class Anchors(list):
    """`load_samples` 的返回值：只装**可校准**条目，未确认的挂在 `.pending` 上。

    为什么用 list 子类而不是改成返回二元组：调用方（`run.py calibrate` 与本文件 main）
    以及它们的回归测试都按 list 消费这个返回值，改签名会连带漂移指纹的语义。
    """

    pending: list[dict[str, Any]]

    def __new__(cls, iterable: Any = (), *, pending: Any = ()) -> "Anchors":
        obj = super().__new__(cls, iterable)
        obj.pending = list(pending)
        return obj


# --------------------------------------------------------------------------
# 样本加载
# --------------------------------------------------------------------------
def _confirmed(item: dict[str, Any]) -> bool:
    """这条锚点算不算"人工确认过"。

    缺省 True 是刻意的向后兼容：存量 `samples.json` 那 11 条本来就是人工分的，
    这个字段引入之前它们没有 `confirmed`，判成未确认等于把现有校准一次性作废。
    新采集的候选由 `harvest_anchors.py` 显式写 `confirmed: false`。
    """
    flag = item.get("confirmed", True)
    if isinstance(flag, str):
        return flag.strip().lower() in {"1", "true", "yes"}
    return bool(flag)


def load_samples(path: Path) -> Anchors:
    """加载并校验锚点样本。纯注释项（只有 _comment 等说明键）跳过；真实条目缺字段直接报错。

    注意跳过条件：只跳过「除说明键外什么都没有」的条目。
    像 {"id": "a"} 这种带真实键但缺关键字段的条目是**写错的样本**，
    静默跳过会让用户以为它参与了校准——必须报错。

    `confirmed: false` 的条目进 `.pending` 而不是返回值：人工分没落地之前，候选分数
    与被校对象同源，让它进统计就是拿被校对象当标准答案。这类条目允许 `human_score: null`
    （等人来填）；已确认条目填 null 属写错，报错。
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("样本文件顶层必须是数组")
    samples: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    required = ("id", "original_task", "prompt", "test_input", "test_output", "human_score")
    skip_keys = {"_comment"}
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            raise ValueError(f"第 {i + 1} 条样本必须是对象")
        # 只有说明键、没有任何真实字段 → 注释项，跳过
        if set(item.keys()) <= skip_keys:
            continue
        missing = [k for k in required if k not in item]
        if missing:
            raise ValueError(f"第 {i + 1} 条样本缺字段：{missing}")
        if not _confirmed(item):
            pending.append(item)
            continue
        if item["human_score"] is None:
            raise ValueError(
                f"第 {i + 1} 条样本（{item.get('id')}）confirmed=true 但 human_score 为 null"
            )
        score = float(item["human_score"])
        if not 1.0 <= score <= 10.0:
            raise ValueError(f"第 {i + 1} 条样本 human_score={score} 超出 1-10")
        samples.append(item)
    ids = [str(s["id"]) for s in samples] + [str(s["id"]) for s in pending]
    if len(ids) != len(set(ids)):
        raise ValueError("样本 id 重复")
    return Anchors(samples, pending=pending)


# --------------------------------------------------------------------------
# 评估（与主管道同口径）
# --------------------------------------------------------------------------
def evaluate_sample(role: str, sample: dict[str, Any]) -> EvaluationResult:
    """用与 pm.nodes._evaluate_one 相同的提示词渲染 + schema 调用真实评委。

    校准不另写评分提示词——否则校准的就不再是线上跑的那台评委。
    """
    user = render(
        EVALUATOR_USER,
        original_task=sample["original_task"],
        context=sample.get("context") or "（无）",
        prompt=sample["prompt"],
        test_input=sample["test_input"],
        test_output=sample["test_output"],
    )
    ev, _meta = structured_call(role, EvaluationResult, EVALUATOR_SYSTEM, user)
    return ev.finalize()


# --------------------------------------------------------------------------
# 一致性分析（纯代码，零 LLM）
# --------------------------------------------------------------------------
def pearson(xs: list[float], ys: list[float]) -> float | None:
    """Pearson 相关系数。样本 <3 或任一方差为零 → None（无法评估）。"""
    n = len(xs)
    if n < MIN_SAMPLES_FOR_R or n != len(ys):
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx == 0.0 or syy == 0.0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    return sxy / math.sqrt(sxx * syy)


def _ranks(xs: list[float]) -> list[float]:
    """平均秩（并列取均秩）。算 Spearman 时不给均秩就等于把 ties 判成强序。"""
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    ranks = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def spearman(xs: list[float], ys: list[float]) -> float | None:
    """Spearman 秩相关。与 Pearson 并报的理由是实测的失效形态：评委在**两套口径之间跳档**
    （仲裁同一输入稳定落在 5.15 与 7.10 两个模式），Pearson 会被这种台阶拉花，
    而秩相关只问"谁比谁好"——闭环真正依赖的是判定线之上排没排对序。

    口径写清楚免得被读反：并列取均秩，所以"两次同分、第三次跳档"不会算出 ρ=1；
    ρ 衡量序关系，不衡量档位一致。
    """
    if len(xs) < MIN_SAMPLES_FOR_R or len(xs) != len(ys):
        return None
    if len(set(xs)) < 2 or len(set(ys)) < 2:
        return None
    return pearson(_ranks(xs), _ranks(ys))


def _band_hist(values: list[float]) -> dict[str, int]:
    hist = {name: 0 for name, _, _ in BANDS}
    for v in values:
        for name, lo, hi in BANDS:
            if lo <= v < hi:
                hist[name] += 1
                break
    return hist


def _decision_stats(humans: list[float], judges: list[float], line: float) -> dict[str, Any]:
    """判定一致率 + Cohen κ。r/MAE 回答"差多少分"，没人回答"过没过线"。

    闭环唯一的决策是 `weighted_score >= 8.0`，而 r=0.97 的评委完全可能把 7.4 系统性
    抬成 8.3：分差 0.9 在 MAE 里不算大，在决策上就是把不可用读成可上线。
    所以这一项单列，并且分方向报——**漏放**（人工不过、评委过）比误杀危险。
    """
    n = len(humans)
    if n == 0:
        return {"n": 0, "usable": False}
    h_pass = [h >= line for h in humans]
    j_pass = [j >= line for j in judges]
    agree = sum(1 for a, b in zip(h_pass, j_pass, strict=True) if a == b) / n
    lenient = sum(1 for a, b in zip(h_pass, j_pass, strict=True) if (not a) and b)
    strict_miss = sum(1 for a, b in zip(h_pass, j_pass, strict=True) if a and (not b))
    p_a = sum(h_pass) / n
    p_b = sum(j_pass) / n
    pe = p_a * p_b + (1 - p_a) * (1 - p_b)
    return {
        "n": n,
        "line": line,
        "agree": round(agree, 3),
        "kappa": None if pe >= 1.0 else round((agree - pe) / (1 - pe), 3),
        "n_lenient": lenient,
        "n_strict": strict_miss,
        "n_human_pass": sum(h_pass),
        "n_judge_pass": sum(j_pass),
        # 两侧都无样本时一致率是 100%，κ 无定义——这时报数比不报更容易骗人
        "usable": min(sum(h_pass), n - sum(h_pass)) >= MIN_PER_DECISION_SIDE
        and min(sum(j_pass), n - sum(j_pass)) >= MIN_PER_DECISION_SIDE,
    }


def analyze(samples: list[dict[str, Any]], judge_scores: list[float]) -> dict[str, Any]:
    """逐条 Δ + 聚合指标。调用方保证两列表等长且只含成功样本。"""
    pairs: list[dict[str, Any]] = []
    # strict=False：调用方已保证等长，防御性容忍调用侧偏差而不炸
    for i, (s, j) in enumerate(zip(samples, judge_scores, strict=False)):
        human = float(s["human_score"])
        delta = round(j - human, 2)
        pairs.append(
            {
                "id": str(s.get("id") or f"#{i}"),
                "human": human,
                "judge": round(j, 2),
                "delta": delta,
                "flag": abs(delta) > LARGE_DEVIATION,
            }
        )
    n = len(pairs)
    bias = round(sum(p["delta"] for p in pairs) / n, 2)
    mae = round(sum(abs(p["delta"]) for p in pairs) / n, 2)
    humans = [p["human"] for p in pairs]
    judges = [p["judge"] for p in pairs]
    r = pearson(humans, judges)
    rho = spearman(humans, judges)
    hist = _band_hist(humans)
    return {
        "n": n,
        "pairs": pairs,
        "bias": bias,
        "mae": mae,
        "r": None if r is None else round(r, 3),
        "rho": None if rho is None else round(rho, 3),
        "flags": [p["id"] for p in pairs if p["flag"]],
        "human_band_hist": hist,
        "bands_covered": sum(1 for c in hist.values() if c),
        "decision": _decision_stats(humans, judges, PASS_THRESHOLD),
    }


# --------------------------------------------------------------------------
# 报告渲染
# --------------------------------------------------------------------------
def _bias_verdict(bias: float) -> str:
    if bias > BIAS_ALERT:
        return f"⚠️ 评委系统性**偏松**（平均比人工高 {bias} 分）——存在放水倾向，建议降温或换模型家族"
    if bias < -BIAS_ALERT:
        return (
            f"⚠️ 评委系统性**偏严**（平均比人工低 {abs(bias)} 分）——会压制合格输出，建议校准评分锚点"
        )
    return "✅ 整体偏差在容差内（±1 分）"


def _r_verdict(r: float | None, rho: float | None, bands: int) -> str:
    if r is None:
        return "⚠️ 排序一致性无法评估（样本不足或方差为零）"
    tail = ""
    if bands < MIN_BANDS_COVERED:
        tail = f"（人工分只覆盖 {bands} 个分数带，**这个 r 是在窄区间里算的**，换个集就不能比）"
    both = f"（Spearman ρ={rho}）" if rho is not None else ""
    if r >= CORR_GOOD:
        return f"✅ 排序一致性好（r={r}{both}）：评委分高的人工分也高{tail}"
    if r < CORR_BAD:
        return f"⚠️ 排序一致性差（r={r}{both}）：评委的**相对判断**不可信，比分数本身更严重{tail}"
    return f"排序一致性中等（r={r}{both}）{tail}"


def _decision_verdict(dec: dict[str, Any]) -> str:
    if not dec.get("usable"):
        return (
            f"判定一致率 {dec.get('agree')}（人工过线 {dec.get('n_human_pass')}/{dec.get('n')}、"
            f"评委过线 {dec.get('n_judge_pass')}/{dec.get('n')}）——"
            f"⚠️ 线某一侧样本 < {MIN_PER_DECISION_SIDE}，**这个一致率读的是取样，不是评委行为**"
        )
    k = dec.get("kappa")
    ks = "κ 无定义（两侧全一致）" if k is None else f"κ={k}"
    return (
        f"以 {dec['line']} 判定线计：一致率 **{dec['agree']:.0%}**、{ks}；"
        f"漏放（人工判不可用、评委判可用）**{dec['n_lenient']} 条**、"
        f"误杀 {dec['n_strict']} 条"
    )


def render_report(role: str, analysis: dict[str, Any]) -> str:
    label = _ROLE_LABEL.get(role, role)
    lines: list[str] = [f"# 评委校准报告：{label}（{role}）", ""]
    lines.append("| 锚点 | 人工分 | 评委分 | Δ | 标记 |")
    lines.append("|---|---|---|---|---|")
    for p in analysis["pairs"]:
        mark = "⚠️ 大偏差" if p["flag"] else ""
        lines.append(f"| {p['id']} | {p['human']} | {p['judge']} | {p['delta']:+} | {mark} |")
    lines.append("")
    lines.append(f"- 样本数：{analysis['n']}（排除评估失败的锚点，也排除未人工确认的条目）")
    lines.append(f"- 平均绝对误差 MAE：{analysis['mae']}")
    lines.append(
        f"- 平均偏差（评委 − 人工）：{analysis['bias']:+} → {_bias_verdict(analysis['bias'])}"
    )
    lines.append(
        f"- 排序一致性：{_r_verdict(analysis['r'], analysis.get('rho'), int(analysis.get('bands_covered', 0)))}"
    )
    lines.append(f"- 过线判定：{_decision_verdict(analysis.get('decision') or {'usable': False})}")
    hist = analysis.get("human_band_hist") or {}
    if hist:
        lines.append(
            "- 人工分分布：" + "　".join(f"{b}:{n}" for b, n in hist.items())
            + f"（覆盖 {analysis.get('bands_covered', 0)}/{len(BANDS)} 带）"
        )
    if analysis["flags"]:
        lines.append(
            f"- ⚠️ 大偏差样本（|Δ| > {LARGE_DEVIATION}）：{', '.join(analysis['flags'])}——优先人工复核这几条"
        )
    if analysis["n"] < MIN_SAMPLES_FOR_VERDICT:
        lines.append(
            f"> ⚠️ 样本 {analysis['n']} < {MIN_SAMPLES_FOR_VERDICT}：以上数字仅方向性参考，"
            "不足以支撑「评委可信/不可信」的结论，继续积累锚点。"
        )
    return "\n".join(lines)


def render_provenance(total: int, pending: int) -> str:
    """采集来源披露。它挡的是一种很具体的自欺：候选集越大，输出越像"校准很充分"，
    而未确认的条目既不进分母也不参与打分——不写出来的话，读报告的人会以为 45 条全用了。
    """
    if pending <= 0:
        return ""
    return (
        f"\n> ℹ️ 样本文件共 {total} 条，其中 **{pending} 条未经人工确认（confirmed=false），"
        f"已从本次校准中排除**；实际参与 {total - pending} 条。"
        "未确认的候选分数与被校评委同源，纳入等于用被校对象当标准答案。"
    )


def render_repeatability(rep: dict[str, Any]) -> str:
    """复现性读数。它回答的是"这台仪表自己稳不稳"，与"准不准"是两件事。"""
    lines = [
        "",
        f"## 复现性（同输入重复 {rep['times']} 次，已绕开评估缓存）",
        "",
        f"- 参与条数：{rep['n_items']}；极差均值 **{rep.get('range_mean')}**，最大 **{rep.get('range_max')}**"
        f"（最差那条：`{rep.get('worst_id')}`）",
        f"- 双评委分差阈值：{rep.get('disagreement_threshold')}",
    ]
    if rep.get("n_items"):
        if rep.get("threshold_exceeded"):
            lines.append(
                "- ⚠️ **抖动已淹没阈值**：该评委对同一份输入的自我分歧就能越过分差阈值，"
                "于是「双评委分歧 → 仲裁」有相当比例是在读它自己的抖动，不是在读用例的难度。"
                "把判定角色换成自我极差更小的模型，或把阈值提到极差之上，再谈 Δ 的分辨率。"
            )
        else:
            lines.append("- ✅ 自我极差未越过分差阈值：分歧信号目前多于仪表噪声。")
        lines.append(
            "- 注意 median-of-3 只对**围绕真值的单峰噪声**有效；若分数在两个模式之间跳"
            "（实测评委 B / 仲裁就是这样），中位数是在两个口径之间投票，可能比单次更差。"
        )
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 校准主流程（与 CLI 分离，便于测试注入假评委）
# --------------------------------------------------------------------------
def calibrate(
    samples: list[dict[str, Any]], role: str
) -> tuple[dict[str, Any], list[tuple[str, str]]]:
    """逐条评估并聚合。返回 (分析结果, 失败锚点列表)。"""
    ok_samples: list[dict[str, Any]] = []
    ok_scores: list[float] = []
    errors: list[tuple[str, str]] = []
    for s in samples:
        sid = str(s.get("id") or "?")
        try:
            ev = evaluate_sample(role, s)
            ok_samples.append(s)
            ok_scores.append(ev.weighted_score)
        except Exception as e:  # noqa: BLE001 - 单锚点失败不中断校准
            errors.append((sid, f"{type(e).__name__}: {e}"))
    if not ok_samples:
        return {}, errors
    return analyze(ok_samples, ok_scores), errors


# --------------------------------------------------------------------------
# 复现性：评委跟**自己**比（不与人工比）
# --------------------------------------------------------------------------
def repeatability(samples: list[dict[str, Any]], role: str, times: int = 3) -> dict[str, Any]:
    """同一份渲染连续打 `times` 次，量极差。绕开评估缓存——否则第二次永远命中，
    测出来的极差恒为 0，那是自欺而不是"很稳定"。

    为什么要单列一个指标：bias / MAE / r 全是"与人工比"，一个每次调用在不同口径之间
    跳变的模型在这些数字里可以看着完全正常（抖动甚至会把 MAE 摊平）。而双评委分差阈值、
    仲裁触发率、Δ 的噪声带，全都隐含假设"同一个输入会给同一个分"。
    实测（2026-09-19，真端点）：评委 A 同内容 3 次极差 0.25~0.70，评委 B 1.30~2.60，
    仲裁一次给 4.45、一次给 8.88（极差 4.43）；**把 temperature 降到 0 并不改善**。
    结论是这类抖动是"选中哪套口径"的双峰，不是围绕真值的噪声——median-of-3 救不了。
    """
    per_item: list[dict[str, Any]] = []
    with backend.with_cache_disabled():
        for s in samples:
            sid = str(s.get("id") or "?")
            scores: list[float] = []
            err = ""
            for _ in range(max(1, times)):
                try:
                    scores.append(float(evaluate_sample(role, s).weighted_score))
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


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="评委校准：锚点样本上对比评委分与人工分")
    ap.add_argument("--samples", default=str(DEFAULT_SAMPLES), help="锚点样本 JSON 路径")
    ap.add_argument(
        "--judge", choices=list(JUDGE_ROLES), default="evaluator", help="要校准的评委角色"
    )
    ap.add_argument(
        "--repeat",
        type=int,
        default=1,
        help="同一份输入连续打 N 次，量评委与**自己**的一致性（绕开评估缓存）。"
        "代价是 N 倍调用；1=不测",
    )
    ap.add_argument(
        "--write-template",
        action="store_true",
        help=f"从示例 {EXAMPLE_SAMPLES.name} 生成样本文件（已存在则跳过）",
    )
    args = ap.parse_args()

    if args.write_template:
        target = Path(args.samples)
        if target.exists():
            print(f"已存在，跳过：{target}")
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(EXAMPLE_SAMPLES.read_text(encoding="utf-8"), encoding="utf-8")
            print(f"已生成样本模板：{target}")
            print("人工核对每条的 human_score（1-10）后重新运行本命令（不带 --write-template）。")
        return 0

    try:
        samples = load_samples(Path(args.samples))
    except (OSError, ValueError, json.JSONDecodeError) as e:
        print(f"样本加载失败：{e}", file=sys.stderr)
        return 2
    if not samples:
        n_pending = len(getattr(samples, "pending", []))
        hint = (
            f"（{n_pending} 条候选全部未人工确认——把 confirmed 改成 true 并填 human_score 再跑）"
            if n_pending
            else "（先用 --write-template 生成模板）"
        )
        print(f"样本为空：{args.samples}{hint}", file=sys.stderr)
        return 2
    if getattr(samples, "pending", None):
        print(
            render_provenance(len(samples) + len(samples.pending), len(samples.pending)),
            file=sys.stderr,
        )

    analysis, errors = calibrate(samples, args.judge)
    for sid, err in errors:
        print(f"  [SKIP] {sid}：{err}", file=sys.stderr)
    if not analysis:
        print("全部锚点评估失败，无法校准（先跑 run.py --preflight 检查端点）", file=sys.stderr)
        return 1
    print(render_report(args.judge, analysis))
    if args.repeat >= 2:
        rep = repeatability(samples, args.judge, args.repeat)
        analysis["repeatability"] = rep
        print(render_repeatability(rep))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
