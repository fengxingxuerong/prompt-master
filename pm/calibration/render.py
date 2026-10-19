"""读数渲染：把统计结果变成人读的文本。

2026-10-19 从 `pm/calibration.py` 拆出。只做字符串组装，不做计算。
"""

from __future__ import annotations

from typing import Any

from ..schemas import PASS_THRESHOLD
from .stats import (
    BANDS,
    BIAS_ALERT,
    CORR_BAD,
    CORR_GOOD,
    KAPPA_CI_HALF_WIDTH_MAX,
    LARGE_DEVIATION,
    MIN_BANDS_COVERED,
    MIN_ESTIMABLE_CELL,
    MIN_PER_DECISION_SIDE,
    MIN_SAMPLES_FOR_VERDICT,
)

_ROLE_LABEL = {"evaluator": "评委A", "evaluator_b": "评委B", "arbiter": "仲裁评委"}


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


def render_discrimination(disc: dict[str, Any]) -> list[str]:
    """把判别力读数渲染成"下一步该做什么"，不是又一行指标。

    三行各自挡掉一种自欺：
    - AUC≈0.5 时还去调阈值/换判据 —— 那是把噪声切成更整的噪声；
    - 只报"同批最优阈值正确率 93%" —— 那是"全部判不达标"，留一折才看得见它有没有净增益；
    - 正例几条就下"评委可信/不可信" —— κ 的 CI 宽度直接说明这是取样不是行为。
    """
    n = int(disc.get("n") or 0)
    if n == 0:
        return []
    out: list[str] = []
    auc = disc.get("auc")
    ci = disc.get("auc_ci95")
    if auc is None:
        out.append(
            f"- 判别力：AUC 不可测（人工达标 {disc.get('n_human_pass')}/{n} —— 标签某一侧为空，"
            "这张考卷没有可分的两类，谈不上评委有没有判别力）"
        )
    else:
        # 判据只跟 0.5 比，且看重抽 CI 而不是点值：0.6 这种"看着比瞎猜高"的数，
        # 在 n=22 的考卷上离 0.5 不到一个 CI，拿它下结论就是自欺。
        if ci is None:
            verdict = (
                f"点值 {auc}，但没有可用的重抽 CI"
                f"（{disc.get('auc_ci_skipped') or disc.get('ci_skipped') or '样本不足'}）"
            )
        elif disc.get("auc_distinguishable_from_chance"):
            verdict = f"显著高于瞎猜（95% CI {ci[0]}~{ci[1]}，下界 >0.5）"
        else:
            verdict = (
                f"**与随机猜不可区分**（95% CI {ci[0]}~{ci[1]} 覆盖 0.5）"
                "→ 换阈值/平移/缩放都救不回来，只能换量具或补人工标签"
            )
        out.append(f"- 判别力：AUC={auc}（0.5=与随机猜无异）→ {verdict}")
    loo = disc.get("loo_accuracy")
    trivial = disc.get("trivial_accuracy")
    gain = disc.get("loo_gain_vs_trivial")
    if loo is not None and gain is not None:
        arrow = "**没有净增益**" if gain <= 0 else "有净增益"
        out.append(
            f"- 调阈值的净增益：留一折重定阈值 {loo:.3f} − 平凡基线（一律判多数那一侧）{trivial:.3f} "
            f"= **{gain:+.3f}**（{arrow}；同批拟合的上界 {disc.get('best_accuracy')} 不算数）"
        )
    cell = disc.get("min_cell")
    half = disc.get("kappa_ci_half_width")
    if cell is not None and cell < MIN_ESTIMABLE_CELL:
        out.append(
            f"- 可估性：⚠️ 判定 2×2 里最薄的一格只有 **{cell} 条**（< {MIN_ESTIMABLE_CELL}；"
            f"人工达标 {disc.get('n_human_pass')}/{n}、评委达标 {disc.get('n_judge_pass')}/{n}）—— "
            f"**这张考卷答不了「达标一致性」**，上面那个 κ 点值读的是考卷设计，不是评委行为。"
            f"门槛怎么来的见 docs/evaluation.md §三十一。"
        )
    elif half is None:
        note = disc.get("kappa_ci_skipped") or disc.get("ci_skipped") or "CI 未计算"
        out.append(f"- 可估性：⚠️ {note} —— **按不可估处理**，不许拿点值下「可信/不可信」的结论")
    elif half > KAPPA_CI_HALF_WIDTH_MAX:
        out.append(
            f"- 可估性：⚠️ κ 的 95% CI 半宽 {half} > {KAPPA_CI_HALF_WIDTH_MAX} —— "
            f"**这张考卷答不了「达标一致性」**，上面那个点值读的是取样（人工达标仅 "
            f"{disc.get('n_human_pass')}/{n} 条）。判据推导见 docs/evaluation.md §三十一。"
        )
    else:
        out.append(f"- 可估性：κ 的 95% CI 半宽 {half} ≤ {KAPPA_CI_HALF_WIDTH_MAX}，点值可作结论")
    return out


def render_report(role: str, analysis: dict[str, Any]) -> str:
    label = _ROLE_LABEL.get(role, role)
    lines: list[str] = [f"# 评委校准报告：{label}（{role}）", ""]
    lines.append("| 锚点 | 人工分 | 评委分 | Δ | 标记 |")
    lines.append("|---|---|---|---|---|")
    for p in analysis["pairs"]:
        mark = "⚠️ 大偏差" if p["flag"] else ""
        lines.append(f"| {p['id']} | {p['human']} | {p['judge']} | {p['delta']:+} | {mark} |")
    lines.append("")
    n_failed = int(analysis.get("n_failed") or 0)
    failed_line = f"- 样本数：{analysis['n']}（排除评估失败的锚点，也排除未人工确认的条目）"
    if n_failed:
        who = "、".join(
            f"{f.get('id')}（{str(f.get('error') or '')[:60]}）"
            for f in analysis.get("failed") or []
        )
        failed_line = (
            f"- 样本数：{analysis['n']}（另有 **{n_failed} 条锚点评估失败被排除**：{who}）\n"
            "- ⚠️ 分母变了就不叫同一张考卷：与任何 n 不同的历史记录比 MAE/bias 都不成立"
            "（漂移对比已按 n 拦，但 A/B 两臂必须配对后再看）"
        )
    lines.append(failed_line)
    lines.append(f"- 平均绝对误差 MAE：{analysis['mae']}")
    lines.append(
        f"- 平均偏差（评委 − 人工）：{analysis['bias']:+} → {_bias_verdict(analysis['bias'])}"
    )
    lines.append(
        f"- 排序一致性：{_r_verdict(analysis['r'], analysis.get('rho'), int(analysis.get('bands_covered', 0)))}"
    )
    lines.append(f"- 过线判定：{_decision_verdict(analysis.get('decision') or {'usable': False})}")
    lines.extend(render_discrimination(analysis.get("discrimination") or {}))
    hist = analysis.get("human_band_hist") or {}
    if hist:
        lines.append(
            "- 人工分分布："
            + "　".join(f"{b}:{n}" for b, n in hist.items())
            + f"（覆盖 {analysis.get('bands_covered', 0)}/{len(BANDS)} 带）"
        )
    if analysis["flags"]:
        lines.append(
            f"- ⚠️ 大偏差样本（|Δ| > {LARGE_DEVIATION}）：{', '.join(analysis['flags'])}——优先人工复核这几条"
        )
    if analysis.get("empty_high"):
        lines.append(
            f"- ⚠️ 空产出获高分（≥{PASS_THRESHOLD}）：{', '.join(analysis['empty_high'])}"
            " —— 输出呈空壳/纯拒答画像，没有实质交付物；"
            "「拒答得体」应体现为 task_completion 低分，不该换来可上线判定"
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
