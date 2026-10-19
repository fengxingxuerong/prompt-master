"""纯统计：MAE/bias/κ/AUC/留一折/可估性。

2026-10-19 从 `pm/calibration.py` 拆出。零调用、零 IO —— 只吃分数数组。
"""

from __future__ import annotations

import math
import random
from typing import Any

from ..schemas import PASS_THRESHOLD
from ..scoring import has_empty_deliverable_profile

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
# ⚠️ 2026-10-19 拆包时漏过一次：它带类型标注（`BANDS: tuple[...] =`），
# 按 `ast.Assign` 提取会漏掉 `ast.AnnAssign` —— 表现是 `NameError: BANDS` 直到跑起来才炸。
BANDS: tuple[tuple[str, float, float], ...] = (
    ("<6.0", 0.0, 6.0),
    ("6.0-6.9", 6.0, 7.0),
    ("7.0-7.9", 7.0, 8.0),
    ("8.0-8.9", 8.0, 9.0),
    (">=9.0", 9.0, 10.01),
)


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


def _percentile(sorted_vals: list[float], q: float) -> float:
    """线性插值分位数（与 numpy 默认 linear 法一致）。q ∈ [0,100]，空表是调用方的 bug。"""
    if not sorted_vals:
        raise ValueError("分位数的输入为空")
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    idx = q / 100 * (len(sorted_vals) - 1)
    lo = math.floor(idx)
    hi = math.ceil(idx)
    if lo == hi:
        return sorted_vals[lo]
    frac = idx - lo
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * frac


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


# --------------------------------------------------------------------------
# 判别力与可估性（纯算术，零 LLM 调用）
# --------------------------------------------------------------------------
# 为什么要单列这一层：MAE / r / 一致率回答的都是"这次读数与人工差多少"，
# 没有任何一个回答"评委分关于达标这件事到底有没有信息"。而后者才决定
# 事后处理（换阈值、平移、缩放）救不救得回来——AUC≈0.5 时一律救不回来，
# 此时换判据是白费，只能换量具或补标签。
#
# 可估性门槛不是拍的：κ 的标准误 SE = sqrt[θ(1-θ)/(n(1-θ₀)²)]（Fleiss 1971），
# 在"正例占比 20%、真实 κ=0.6、95% CI 半宽 ≤0.3"这组要求下反解得 n≈47 → 每个格子 ≥9，
# 取整为 10；同一组要求再用蒙特卡洛核对过公式（探针 logs/probe_r3_power.py，
# 推导与实测读数记在 docs/evaluation.md §三十一）。
# 对比：MIN_PER_DECISION_SIDE=1 只管 κ 有没有**定义**，不管它**能不能读**——
# 1 个正例时 κ 照样算得出来，只是那个数由构造决定地趋近 0。
KAPPA_CI_HALF_WIDTH_MAX = 0.3  # κ 的 95% CI 半宽大于它 → 点值不许当结论报


MIN_ESTIMABLE_CELL = 10  # 2×2 四个格子各自的最小条数（由上面的功率反解得出）


DISCRIMINATION_BOOTSTRAP = 1000  # 配对重抽次数（单轮校准里每条锚点一条配对）


DISCRIMINATION_SEED = 20261006  # 固定种子：同一批配对必须每次算出同一个 CI


CUT_SCAN_STEP = 0.05  # 阈值扫描步长，比评委分的最小可见粒度（0.05）不粗


def _auc_vs_label(scores: list[float], labels: list[bool]) -> float | None:
    """Mann-Whitney AUC：任取一条达标与一条不达标，评委分把前者排高的概率。并列算半分。

    0.5 = 评委分与标签无关（任何阈值都切不出来）；1.0 = 完全可分。
    任一侧为空时返回 None——那不是"判别力差"，是"没测"。
    """
    pos = [s for s, t in zip(scores, labels, strict=False) if t]
    neg = [s for s, t in zip(scores, labels, strict=False) if not t]
    if not pos or not neg:
        return None
    wins = 0.0
    ties = 0.0
    for a in pos:
        for b in neg:
            if a > b:
                wins += 1.0
            elif a == b:
                ties += 1.0
    return (wins + 0.5 * ties) / (len(pos) * len(neg))


def _cuts_in_range(scores: list[float]) -> list[float]:
    """只扫评委分**可表达**的区间。允许 cut 超出 max 就等于允许"全部判不达标"，
    那是一条输出而不是一个阈值，会被误读成"评委被校准好了"。"""
    lo, hi = min(scores), max(scores)
    steps = int((hi - lo) / CUT_SCAN_STEP) + 1
    return [round(lo + i * CUT_SCAN_STEP, 4) for i in range(steps + 1)]


def _best_cut(scores: list[float], labels: list[bool]) -> tuple[float | None, float]:
    """同批数据上的最优阈值。这是**上界**，只能用来判"有没有空间"，不能当读数。"""
    cuts = _cuts_in_range(scores)
    if not cuts:
        return None, 0.0
    best_cut, best_acc = cuts[0], -1.0
    for c in cuts:
        acc = sum(1 for s, t in zip(scores, labels, strict=False) if (s >= c) == t) / len(scores)
        if acc > best_acc:
            best_cut, best_acc = c, acc
    return best_cut, best_acc


def _loo_cut_accuracy(scores: list[float], labels: list[bool]) -> float | None:
    """留一折重定阈值：每条用其余 n-1 条挑阈值，再判这一条。

    必须留一折：同批拟合的最优阈值恒 ≥ 平凡基线（它总可以退化成"全判一边"），
    拿它报"调阈值带来了增益"是同义反复。
    """
    n = len(scores)
    if n < 3:
        return None
    hit = 0
    counted = 0
    for i in range(n):
        rest_s = [s for q, s in enumerate(scores) if q != i]
        rest_t = [t for q, t in enumerate(labels) if q != i]
        cut, _ = _best_cut(rest_s, rest_t)
        if cut is None:
            continue
        counted += 1
        hit += 1 if ((scores[i] >= cut) == labels[i]) else 0
    return hit / counted if counted else None


def _kappa_and_agree(
    humans: list[float], judges: list[float], line: float
) -> tuple[float, float | None]:
    """(一致率, κ)。κ 在两侧边际退化时返回 None（与 _decision_stats 同一判法）。"""
    n = len(humans)
    h_pass = [h >= line for h in humans]
    j_pass = [j >= line for j in judges]
    agree = sum(1 for a, b in zip(h_pass, j_pass, strict=False) if a == b) / n
    p_a = sum(h_pass) / n
    p_b = sum(j_pass) / n
    pe = p_a * p_b + (1 - p_a) * (1 - p_b)
    return agree, (None if pe >= 1.0 else (agree - pe) / (1 - pe))


def _decision_ci(
    humans: list[float],
    judges: list[float],
    line: float,
    reps: int = DISCRIMINATION_BOOTSTRAP,
    seed: int = DISCRIMINATION_SEED,
    group_keys: list[str] | None = None,
) -> dict[str, Any]:
    """配对（或按锚点聚类）重抽，给一致率、κ 与 AUC 各一个 95% CI。

    重抽单位由 `group_keys` 决定：单轮校准里一条锚点只有一条配对，留空即按配对重抽；
    跨轮聚合（aggregate_history）里同一锚点在 K 轮出现 K 次，配对之间**不独立**，
    必须把同一锚点的读数捆在一起走 —— 口径与 `aggregate_history` 的 MAE 聚类自助法一致，
    否则 CI 系统性偏窄（这正是 §十四 当初单列聚类的原因）。
    """
    n = len(humans)
    if group_keys is None:
        units: list[list[int]] = [[i] for i in range(n)]
    else:
        buckets: dict[str, list[int]] = {}
        for i, key in enumerate(group_keys):
            buckets.setdefault(str(key), []).append(i)
        units = [buckets[key] for key in sorted(buckets)]
    if len(units) < 2 or reps <= 0:
        return {"ci_skipped": f"重抽单位 {len(units)} 个 < 2，重抽退化，不给 CI"}
    rng = random.Random(seed)
    agrees: list[float] = []
    kappas: list[float] = []
    aucs: list[float] = []
    kappa_undef = 0
    auc_undef = 0
    for _ in range(reps):
        # 每次重抽取 len(units) 个单位（有放回）再摊平：未分组时一个单位=一条配对，
        # 与"按配对重抽 n 次"完全等价；分组时一个单位=同一锚点的全部轮次读数。
        drawn = [units[rng.randrange(len(units))] for _ in range(len(units))]
        idx = [j for u in drawn for j in u]
        hs = [humans[i] for i in idx]
        js = [judges[i] for i in idx]
        a, k = _kappa_and_agree(hs, js, line)
        agrees.append(a)
        if k is None:
            kappa_undef += 1
        else:
            kappas.append(k)
        v = _auc_vs_label(js, [h >= line for h in hs])
        if v is None:
            auc_undef += 1
        else:
            aucs.append(v)
    agrees.sort()
    out: dict[str, Any] = {
        "agree_ci95": [round(_percentile(agrees, 2.5), 3), round(_percentile(agrees, 97.5), 3)]
    }
    # 无定义的重抽不扔掉、也不当门槛：扔掉等于把 CI 条件化在"正例恰好被抽中"上；
    # 拿它当门槛又太松——1 个正例时 κ 照样"有定义"，只是**由构造决定地趋近 0**。
    # 真正的可估性判据是格子计数（见 discrimination_stats 里的 MIN_ESTIMABLE_CELL）。
    out["auc_undefined_resamples"] = auc_undef
    out["kappa_undefined_resamples"] = kappa_undef
    if len(aucs) >= 20:
        aucs.sort()
        lo, hi = round(_percentile(aucs, 2.5), 3), round(_percentile(aucs, 97.5), 3)
        out["auc_ci95"] = [lo, hi]
        out["auc_distinguishable_from_chance"] = lo > 0.5
    else:
        out["auc_ci_skipped"] = f"可用重抽仅 {len(aucs)} 次，分位数不稳，不给 AUC CI"
    if kappas:
        kappas.sort()
        lo = round(_percentile(kappas, 2.5), 3)
        hi = round(_percentile(kappas, 97.5), 3)
        out["kappa_ci95"] = [lo, hi]
        out["kappa_ci_half_width"] = round((hi - lo) / 2, 3)
    return out


def discrimination_stats(
    humans: list[float],
    judges: list[float],
    line: float,
    reps: int = DISCRIMINATION_BOOTSTRAP,
    seed: int = DISCRIMINATION_SEED,
    group_keys: list[str] | None = None,
) -> dict[str, Any]:
    """评委分对"人工达标"这件事的判别力，以及这张考卷能不能读出达标一致性。

    返回的每一项都对应一个可判的动作，不是装饰：
    - auc：事后处理有没有空间（≈0.5 就别再调阈值了）；
    - trivial_accuracy：平凡基线（一律判不达标/不达标里人数多的那一边）的正确率——
      任何"调阈值有效"的说法必须显式赢过它；
    - loo_accuracy / loo_gain_vs_trivial：留一折重定阈值的净增益，可为负；
    - kappa_ci_half_width / estimable：点值能不能当结论。
    """
    n = len(humans)
    if n == 0:
        return {"n": 0, "usable": False}
    labels = [h >= line for h in humans]
    n_pos = sum(labels)
    n_judge_pass = sum(1 for j in judges if j >= line)
    cell_min = min(n_pos, n - n_pos, n_judge_pass, n - n_judge_pass)
    trivial = max(n_pos, n - n_pos) / n
    auc = _auc_vs_label(judges, labels)
    best_cut, best_acc = _best_cut(judges, labels)
    loo = _loo_cut_accuracy(judges, labels)
    ci = _decision_ci(humans, judges, line, reps=reps, seed=seed, group_keys=group_keys)
    half = ci.get("kappa_ci_half_width")
    estimable = (
        cell_min >= MIN_ESTIMABLE_CELL
        and isinstance(half, (int, float))
        and half <= KAPPA_CI_HALF_WIDTH_MAX
    )
    return {
        "n": n,
        "line": line,
        "n_human_pass": n_pos,
        "n_judge_pass": n_judge_pass,
        "min_cell": cell_min,
        "auc": None if auc is None else round(auc, 3),
        "trivial_accuracy": round(trivial, 3),
        "best_cut": None if best_cut is None else round(best_cut, 2),
        "best_accuracy": round(best_acc, 3),
        "loo_accuracy": None if loo is None else round(loo, 3),
        "loo_gain_vs_trivial": None if loo is None else round(loo - trivial, 3),
        "kappa_ci_half_width": half,
        "estimable": estimable,
        **ci,
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
    # 空产出高分（2026-09-27 人工锚点实测）：空壳/纯拒答画像的输出被评委判可上线。
    # 警告不否决——告的从来不是"输出差"，是"高分与交付物缺失并存"这个矛盾。
    empty_high = [
        p["id"]
        for s, p in zip(samples, pairs, strict=False)
        if p["judge"] >= PASS_THRESHOLD
        and has_empty_deliverable_profile(str(s.get("test_output") or ""))
    ]
    return {
        "n": n,
        "pairs": pairs,
        "bias": bias,
        "mae": mae,
        "r": None if r is None else round(r, 3),
        "rho": None if rho is None else round(rho, 3),
        "flags": [p["id"] for p in pairs if p["flag"]],
        "empty_high": empty_high,
        "human_band_hist": hist,
        "bands_covered": sum(1 for c in hist.values() if c),
        "decision": _decision_stats(humans, judges, PASS_THRESHOLD),
        "discrimination": discrimination_stats(humans, judges, PASS_THRESHOLD),
    }
