#!/usr/bin/env python3
"""评委校准：在锚点样本上对比「评委分」与「人工专家分」，回答"评委打 8 分可信吗"。

为什么需要它：整个闭环的质量上限是评委模型的水位，而"防放水机制"只保证评委
**不虚高**，不回答"它与人类专家的判断差多远"。校准集 = 一批（提示词 × 测试输入 ×
输出 × 人工分）锚点：用与主管道完全相同的评估管线（EVALUATOR_SYSTEM + 同一渲染 +
同一 finalize 加权）跑一遍真实评委，计算偏差与排序一致性——换评委模型、调评分
提示词之后跑一次，"分数可信"就从设计主张变成实测数据。

用法（两种入口，同一个引擎）：
    python run.py calibrate --repeat 3          # 主管道入口（推荐；结果进漂移账本）
    python -m pm.calibration --write-template   # 从示例生成 samples.json（已存在则跳过）
    python -m pm.calibration --judge evaluator_b

流程：
1. 人工给 `judge_calibration/samples.json` 里的每条锚点打 human_score（1-10）；
2. 本工具逐条调用真实评委，得到代码加权分（与主管道同口径）；
3. 输出逐条 Δ、MAE、平均偏差（正=偏松/放水倾向，负=偏严）、Pearson 排序一致性，
   并给对症结论。退出码：0=完成分析（结论好坏都算完成），1=全部评估失败，2=样本问题。

诚实边界：校准告诉你偏差**多大**，不替你提升评委能力；样本 <5 条时数字只有方向性。

2026-09-25 从仓库根 `calibrate_judge.py` 搬进包里：它是 `run.py calibrate` 的引擎，
但作为根目录脚本不会随 `pip install .` 装走 —— 于是装包之后 `calibrate` 子命令是坏的
（当时的兜底是"往 sys.path 塞仓库根再按名 import"，那只在这份检出里有效）。
"""

from __future__ import annotations

import argparse
import json
import math
import random
import statistics
import sys
from pathlib import Path
from typing import Any

from . import backend
from .bootstrap import ensure_utf8_stdio
from .llm import structured_call
from .prompts import EVALUATOR_SYSTEM, EVALUATOR_USER, render
from .schemas import PASS_THRESHOLD, EvaluationResult, judge_disagreement_threshold
from .scoring import has_empty_deliverable_profile

# 仓库根（本文件在 pm/ 下，parents[1] 才是检出根）：锚点目录 judge_calibration/ 在这里
_REPO_ROOT = Path(__file__).resolve().parents[1]

DEFAULT_SAMPLES = _REPO_ROOT / "judge_calibration" / "samples.json"
EXAMPLE_SAMPLES = _REPO_ROOT / "judge_calibration" / "samples.example.json"
ANCHOR_DIR = _REPO_ROOT / "judge_calibration"
JUDGE_ROLES = ("evaluator", "evaluator_b", "arbiter")

# ---- 人工分的**出处**（2026-10-03 补）----
# 48 条候选里有 25 条不是产品所有者打的，是 AI 按所有者已确认判例外推的（GLM-5.3，
# 与评委 B glm-5.2 同族）。这件事此前只写在提交说明和 score_form.csv 的备注栏里，
# 锚点 JSON 里**没有机读位** ⇒ 报告那句"64 条人工锚点、bias +3.09"拆不出严格人工那一半。
# 同源披露本来就是这个项目的习惯（评委同源要检测要报警），锚点侧同源却是口头承认——补平。
HUMAN_SOURCE_OWNER = "owner"
HUMAN_SOURCE_AI_PROXY = "ai_proxy"
HUMAN_SOURCE_UNKNOWN = "unknown"


def classify_human_source(item: dict[str, Any]) -> str:
    """单条锚点的人工分出处。缺字段一律 `unknown`，**不当成 owner**——
    把"没标注"读成"人打的"正好把这个洞重新盖回去。"""
    prov = item.get("provenance")
    raw = ""
    if isinstance(prov, dict):
        raw = str(prov.get("human_score_source") or "")
    elif isinstance(prov, str):
        raw = prov
    if not raw:
        return HUMAN_SOURCE_UNKNOWN
    low = raw.lower()
    if HUMAN_SOURCE_OWNER in low or low.startswith("owner"):
        return HUMAN_SOURCE_OWNER
    if HUMAN_SOURCE_AI_PROXY in low or "代判" in raw:
        return HUMAN_SOURCE_AI_PROXY
    return HUMAN_SOURCE_UNKNOWN


def render_human_source_split(pooled: dict[str, Any]) -> list[str]:
    """把合并读数按人工分出处拆开写。只有一组有数据时不啰嗦（那说明没得拆）。"""
    groups = pooled.get("by_human_source") or {}
    known = {k: v for k, v in groups.items() if v.get("n_pairs")}
    if len(known) < 2:
        return []
    label = {
        HUMAN_SOURCE_OWNER: "所有者亲判",
        HUMAN_SOURCE_AI_PROXY: "AI 代判（与评委 B 同族）",
        HUMAN_SOURCE_UNKNOWN: "未标注出处",
    }
    out = ["", "- 人工分出处拆分（同一批配对重算，零调用）："]
    for src in (HUMAN_SOURCE_OWNER, HUMAN_SOURCE_AI_PROXY, HUMAN_SOURCE_UNKNOWN):
        g = known.get(src)
        if not g:
            continue
        bias = g.get("bias")
        bias_s = f"{bias:+.2f}" if isinstance(bias, (int, float)) else "-"
        out.append(
            f"  - {label.get(src, src)}：{g['n_anchors']} 条锚点 / {g['n_pairs']} 条配对，bias {bias_s}"
        )
    ai = known.get(HUMAN_SOURCE_AI_PROXY, {}).get("n_anchors", 0)
    tot = sum(g.get("n_anchors", 0) for g in known.values())
    if ai and tot and ai / tot >= 0.5:
        out.append(
            f"  - ⚠️ 合并 bias 里 {ai}/{tot} 条锚点的人工分来自 AI 代判 —— 那一部分量的是"
            "**家族一致性**，不是与人一致性；引用整句 bias 时必须带上这一句。"
        )
    return out


def load_human_sources(anchor_dir: Path | None = None) -> dict[str, str]:
    """扫锚点目录，返回 `{锚点 id: owner|ai_proxy|unknown}`。

    跨轮聚合只消费账本里的 id，所以这里要把所有样本文件并起来看；
    同一 id 在多个文件里出现时以**先出现的非 unknown** 为准（确定性顺序，不靠 dict 迭代随机）。
    """
    base = anchor_dir or ANCHOR_DIR
    out: dict[str, str] = {}
    try:
        paths = sorted(p for p in base.glob("samples*.json"))
    except OSError:
        return out
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        rows = (
            data
            if isinstance(data, list)
            else (data.get("samples") if isinstance(data, dict) else None)
        )
        if not isinstance(rows, list):
            continue
        for item in rows:
            if not isinstance(item, dict) or item.get("id") is None:
                continue
            sid = str(item["id"])
            src = classify_human_source(item)
            if out.get(sid, HUMAN_SOURCE_UNKNOWN) == HUMAN_SOURCE_UNKNOWN:
                out[sid] = src
    return out


def split_by_human_source(
    pairs: list[dict[str, Any]], sources: dict[str, str]
) -> dict[str, dict[str, Any]]:
    """把逐锚点配对按人工分出处分组，各算 bias/MAE。纯算术，零调用。"""
    groups: dict[str, dict[str, Any]] = {}
    for p in pairs:
        src = sources.get(str(p.get("id")), HUMAN_SOURCE_UNKNOWN)
        g = groups.setdefault(src, {"pairs": [], "anchors": set()})
        try:
            human = float(p["human"])
            judge = float(p["judge"])
        except (KeyError, TypeError, ValueError):
            continue
        g["pairs"].append(judge - human)
        g["anchors"].add(str(p.get("id")))
    out: dict[str, dict[str, Any]] = {}
    for src, g in sorted(groups.items()):
        deltas = g["pairs"]
        n = len(deltas)
        out[src] = {
            "n_anchors": len(g["anchors"]),
            "n_pairs": n,
            "bias": round(sum(deltas) / n, 3) if n else None,
            "mae": round(sum(abs(d) for d in deltas) / n, 3) if n else None,
        }
    return out


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


class Anchors(list[dict[str, Any]]):
    """`load_samples` 的返回值：只装**可校准**条目，未确认的挂在 `.pending` 上。

    为什么用 list 子类而不是改成返回二元组：调用方（`run.py calibrate` 与本文件 main）
    以及它们的回归测试都按 list 消费这个返回值，改签名会连带漂移指纹的语义。
    """

    pending: list[dict[str, Any]]

    def __new__(cls, iterable: Any = (), *, pending: Any = ()) -> Anchors:
        # 注意：`super().__new__(cls, iterable)` 里那个 iterable 是被 object.__new__ 丢掉的
        # （实测：只调 __new__ 得到的实例是空 list）。真正填充发生在随后自动执行的
        # list.__init__(obj, iterable) —— 所以这里不传它，语义一字不变，
        # 但少了 mypy 的"参数过多"假错。参数本身要留着：它是调用方传进来的样本。
        obj = super().__new__(cls)
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


# --------------------------------------------------------------------------
# 校准主流程（与 CLI 分离，便于测试注入假评委）
# --------------------------------------------------------------------------
def calibrate(
    samples: list[dict[str, Any]], role: str, mode: str = "impression"
) -> tuple[dict[str, Any], list[tuple[str, str]]]:
    """逐条评估并聚合。返回 (分析结果, 失败锚点列表)。"""
    ok_samples: list[dict[str, Any]] = []
    ok_scores: list[float] = []
    errors: list[tuple[str, str]] = []
    for s in samples:
        sid = str(s.get("id") or "?")
        try:
            ev = evaluate_sample(role, s, mode)
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
    per_item: list[dict[str, Any]] = []
    with backend.with_cache_disabled():
        for s in samples:
            sid = str(s.get("id") or "?")
            scores: list[float] = []
            err = ""
            for _ in range(max(1, times)):
                try:
                    scores.append(float(evaluate_sample(role, s, mode).weighted_score))
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
# 跨轮聚合：把账本里同口径的最近几轮合并成「带不确定度的读数」（纯代码，零 LLM）
# --------------------------------------------------------------------------
# README §三 的问题："这些读数本身不稳……单次读数不能当'这把尺子的精度'"。
# 单轮校准把 n 条锚点读成一个数（MAE/bias/κ），锚点本身的取样波动 + 评委的逐轮抖动
# 都被抹进这一个数里。聚合做的事：① 逐锚点聚类自助法（cluster bootstrap）给出
# "换一批锚点重测"的重抽不确定度；② 轮间极差给出"同一把尺子逐轮的漂移幅度"。
# 两个数回答两个不同的问题，混用任何一个都会把噪声读成信号。
AGGREGATE_BOOTSTRAP = 2000
AGGREGATE_LAST_ROUNDS = 4
# 与 repeatability 文档里"极差阈值"同一件事的披露线：CI 宽过它，两次校准差值小于它的比较不成立
AGGREGATE_CI_WIDTH_NOTE = 0.5


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


def _round_mode(r: dict[str, Any]) -> str:
    """账本记录的评分协议。mode 字段引入之前的记录一律是印象式（与漂移对比同口径）。"""
    return str(r.get("mode") or "impression")


def aggregate_history(
    history: list[dict[str, Any]],
    *,
    mode: str = "impression",
    judge: str = "evaluator",
    model: str | None = None,
    rubric: str | None = None,
    last: int = AGGREGATE_LAST_ROUNDS,
    bootstrap: int = AGGREGATE_BOOTSTRAP,
    seed: int = 20260925,
) -> dict[str, Any] | None:
    """合并账本里同 judge+mode、带逐锚点明细的最近 `last` 轮，产出带不确定度的读数。

    返回 None 的两种情形都不是错误：账本里还没有该口径的带明细记录（旧账本只有
    轮级聚合数，重算不出逐锚点分布），或轮记录里一条有效配对都没有。调用方按
    "本轮不聚合"处理即可，不该报错吓人。

    为什么按锚点聚类而不是按配对重抽：同一锚点在 K 轮里出现 K 次，配对之间**不独立**
    ——按配对重抽会把"锚点好/坏"的方差撕碎进 2K 个独立样本里，CI 系统性偏窄，
    那是在用统计学制造精度幻觉。聚类自助法把每条锚点（连同它的全部轮次读数）作为
    一个整体重抽，锚点外推（换一批锚点重测）的不确定度才是这个 CI 想量的东西。

    与漂移对比（_comparable）刻意的不同：漂移只认"指纹完全一致"的轮，因为它是
    信号检测；聚合刻意放宽到同 judge+mode 即可，因为它是**量具刻画**——考卷不同
    时照常合并（对锚点总体的估计依然成立），但会显式声明，不给读数的人埋雷。

    `model`（2026-10-04 评委换型落地）：聚合的"量具"不只是考卷，还有**评委本身**
    ——bias 是"这把尺子相对人工分的系统性偏移"，换评委模型等于换尺子。flash-lite
    时代的 +3.09 与 deepseek-v4-flash 的 +0.03 混进同一份聚合，披露行刻画的就是
    "两把尺的平均"，对当前仪表是错的数。传 model 时只合并账本轮记录 `model` 字段
    匹配的轮次（该字段自换型前的记录起就有，见 _save_entry）；None = 不过滤
    （旧行为，历史读数复现路径保持不变）。换型后新模型尚无轮次时返回 None——
    披露行缺席，等第一次新校准入账。

    `rubric`（同日落地的第二层）：model 过滤后仍可能混进**旧 rubric 时代的同模型
    轮次**（实测：deepseek-v4-flash 在 2026-09-27 旧 rubric 下 bias +4.2、在现行
    rubric 下 +0.29——评分标准变了，同模型也不是同一把尺）。传 rubric 时要求轮
    记录的 `rubric` 指纹一致；模型×rubric 二元组才是"当前仪表"的完整刻画。锚点集
    刻意不进过滤——锚点不同的同代考卷照常合并是 §十四 立过的先例。
    """
    rounds = [
        r
        for r in history
        if r.get("judge") == judge
        and _round_mode(r) == mode
        # 空 items 列表 = 这轮没落明细（与"没有 items 字段"同罪），不算有明细的轮
        and isinstance(r.get("items"), list)
        and r.get("items")
        and (model is None or r.get("model") == model)
        and (rubric is None or r.get("rubric") == rubric)
    ]
    if not rounds:
        return None
    rounds = rounds[-max(1, last) :] if last > 0 else rounds
    pairs: list[dict[str, Any]] = []
    for r in rounds:
        for it in r.get("items") or []:
            if not isinstance(it, dict):
                continue
            try:
                float(it["human"]), float(it["judge"])
            except (KeyError, TypeError, ValueError):
                continue
            pairs.append(it)
    if not pairs:
        return None

    humans = [float(p["human"]) for p in pairs]
    judges = [float(p["judge"]) for p in pairs]
    deltas = [j - h for h, j in zip(humans, judges, strict=True)]
    n_pairs = len(pairs)
    pooled: dict[str, Any] = {
        "n_rounds": len(rounds),
        "n_pairs": n_pairs,
        "n_anchors": len({str(p["id"]) for p in pairs}),
        "mae": round(sum(abs(d) for d in deltas) / n_pairs, 3),
        "bias": round(sum(deltas) / n_pairs, 3),
        # 合并判定统计：κ/一致率是按混淆计数定义的，把各轮配对并起来直接重算即可
        "decision": _decision_stats(humans, judges, PASS_THRESHOLD),
        # 聚合读数是 README 真正引用的那一行，所以判别力/可估性必须在这里也算一遍：
        # 只在单轮报告里立门槛、最被引用的那个数不受管，等于门槛没上门。
        # 重抽单位按锚点聚类（同 §十四 的 MAE CI 口径）——同一锚点跨轮出现 K 次，
        # 按配对独立重抽会把 K 份相关读数当成 K 个独立样本，CI 系统性偏窄。
        "discrimination": discrimination_stats(
            humans, judges, PASS_THRESHOLD, group_keys=[str(p.get("id")) for p in pairs]
        ),
    }

    # 人工分出处拆分：把"bias +3.09"这一句拆成"严格人工 / AI 代判 / 没标注"三份读。
    # 纯账本重算，零调用；读不到锚点文件就是 unknown 一组，不编数。
    sources = load_human_sources()
    pooled["by_human_source"] = split_by_human_source(pairs, sources)
    _ai = (pooled["by_human_source"].get(HUMAN_SOURCE_AI_PROXY) or {}).get("n_anchors", 0)
    pooled["ai_proxy_anchors"] = _ai
    pooled["owner_anchors"] = (pooled["by_human_source"].get(HUMAN_SOURCE_OWNER) or {}).get(
        "n_anchors", 0
    )

    # 聚类自助法 CI：锚点为重抽单位（见 docstring）。锚点 <2 时重抽是常数，CI 退化为
    # 一个点——报出来就是"看起来很精确的零信息"，宁可声明跳过。
    clusters: dict[str, list[tuple[float, float]]] = {}
    for p in pairs:
        clusters.setdefault(str(p["id"]), []).append((float(p["human"]), float(p["judge"])))
    ids = sorted(clusters)
    if len(ids) >= 2 and bootstrap > 0:
        rng = random.Random(seed)
        by_id = [clusters[sid] for sid in ids]
        maes: list[float] = []
        biases: list[float] = []
        for _ in range(bootstrap):
            ds = [j - h for cl in (by_id[rng.randrange(len(by_id))] for _ in by_id) for h, j in cl]
            maes.append(sum(abs(d) for d in ds) / len(ds))
            biases.append(sum(ds) / len(ds))
        maes.sort()
        biases.sort()
        pooled["mae_ci95"] = [round(_percentile(maes, 2.5), 3), round(_percentile(maes, 97.5), 3)]
        pooled["bias_ci95"] = [
            round(_percentile(biases, 2.5), 3),
            round(_percentile(biases, 97.5), 3),
        ]
    else:
        pooled["ci_skipped"] = f"锚点 {len(ids)} 条 < 2，聚类自助法退化，不给 CI"

    def _span(key: str) -> dict[str, float] | None:
        vals = [float(r[key]) for r in rounds if isinstance(r.get(key), (int, float))]
        if not vals:
            return None
        return {
            "min": round(min(vals), 3),
            "max": round(max(vals), 3),
            "range": round(max(vals) - min(vals), 3),
            "n_rounds": len(vals),
        }

    rep_rounds = [
        r
        for r in rounds
        if isinstance(r.get("rep_range_max"), (int, float)) and r.get("rep_threshold")
    ]
    return {
        "judge": judge,
        "mode": mode,
        "rounds_used": len(rounds),
        # 旧→新，读的时候"最近一轮"是最后一行
        "rounds": [
            {
                "ts": r.get("ts"),
                "n": r.get("n"),
                "mae": r.get("mae"),
                "bias": r.get("bias"),
                "decision_agree": r.get("decision_agree"),
                "decision_kappa": r.get("decision_kappa"),
                "r": r.get("r"),
                "rep_range_max": r.get("rep_range_max"),
            }
            for r in rounds
        ],
        "pooled": pooled,
        "dispersion": {k: _span(k) for k in ("mae", "bias", "decision_agree", "decision_kappa")},
        "rep_threshold_crossings": sum(
            1 for r in rep_rounds if float(r["rep_range_max"]) > float(r["rep_threshold"])
        ),
        "n_rep_rounds": len(rep_rounds),
        "papers_differ": len({r.get("n") for r in rounds}) > 1,
    }


def latest_pooled(
    judge: str = "evaluator", model: str | None = None, rubric: str | None = None
) -> dict[str, Any] | None:
    """当前评分口径下，校准账本的跨轮聚合读数（None=没有可聚合的账本）。

    给交付报告的校准披露行用：报告渲染不自己翻账本算 bootstrap——量具
    刻画全部收口在 aggregate_history，这里只负责"找到账本、对上口径"。
    读不到账本不是错误（新检出没有校准历史时报告不披露即可）；文件坏了
    也返回 None，披露行缺席总比渲染崩掉好。

    `model`（2026-10-04 换型纪元）：调用方应传**本轮实际解析到的评委模型**
    （llm.build_config(judge).model），聚合只合并同模型的轮次——换型后旧
    模型轮次不再污染新仪表的披露行。None = 不过滤（旧行为）。
    """
    from .cli.support import log_dir  # 局部 import：晚绑定 PM_LOG_DIR，不顶层拉 CLI 层
    from .scoring import scoring_mode

    path = log_dir() / "judge_calibration_history.json"
    try:
        history = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(history, list):
        return None
    # aggregate_history 返回 {pooled, rounds, ...} 外层结构；披露行只消费 pooled。
    result = aggregate_history(
        history, judge=judge, mode=scoring_mode(), model=model, rubric=rubric
    )
    return result.get("pooled") if result else None


def render_aggregate(agg: dict[str, Any] | None) -> str:
    """跨轮聚合读数的渲染。agg 为 None 返回空串——"没有可聚合的账本"不是一件事。"""
    if not agg:
        return ""
    pooled = agg["pooled"]
    label = _ROLE_LABEL.get(str(agg["judge"]), str(agg["judge"]))
    lines = [
        "",
        f"## 跨轮聚合（最近 {agg['rounds_used']} 轮 · {agg['mode']} · {label} · 纯账本重算，零调用）",
        "",
        "| 轮(旧→新) | 时间 | n | MAE | bias | 一致率 | κ | r | 复现极差max |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for i, r in enumerate(agg["rounds"], 1):
        lines.append(
            f"| {i} | {r.get('ts') or '-'} | {r.get('n') if r.get('n') is not None else '-'} "
            f"| {r.get('mae')} | {r.get('bias')} "
            f"| {r.get('decision_agree') if r.get('decision_agree') is not None else '-'} "
            f"| {r.get('decision_kappa') if r.get('decision_kappa') is not None else '-'} "
            f"| {r.get('r') if r.get('r') is not None else '-'} "
            f"| {r.get('rep_range_max') if r.get('rep_range_max') is not None else '-'} |"
        )
    mae_ci = pooled.get("mae_ci95")
    bias_ci = pooled.get("bias_ci95")
    mae_line = f"MAE **{pooled['mae']}**" + (
        f"［95% CI {mae_ci[0]}~{mae_ci[1]}］" if mae_ci else ""
    )
    bias_line = f"bias **{pooled['bias']:+}**" + (
        f"［95% CI {bias_ci[0]:+}~{bias_ci[1]:+}］" if bias_ci else ""
    )
    dec = pooled.get("decision") or {}
    agree_s = f"一致率 {dec.get('agree')}" if dec.get("agree") is not None else "一致率 -"
    kappa_s = "-" if dec.get("kappa") is None else f"κ {dec.get('kappa')}"
    lines.append("")
    lines.append(
        f"- 合并读数：{pooled['n_pairs']} 条配对（{pooled['n_anchors']} 条锚点）——"
        f"{mae_line}，{bias_line}，{agree_s}、{kappa_s}"
    )
    if pooled.get("ci_skipped"):
        lines.append(f"- ⚠️ {pooled['ci_skipped']}")
    lines.extend(render_discrimination(pooled.get("discrimination") or {}))
    lines.extend(render_human_source_split(pooled))
    disp = agg.get("dispersion") or {}
    spans = []
    if agg.get("rounds_used", 0) >= 2:  # 单轮没有"轮间"可言，极差 0.0 是噪声不是信息
        for key, name in (
            ("mae", "MAE"),
            ("bias", "bias"),
            ("decision_agree", "一致率"),
            ("decision_kappa", "κ"),
        ):
            s = disp.get(key)
            if s:
                spans.append(f"{name} {s['min']}~{s['max']}（极差 {s['range']}）")
    if spans:
        lines.append(f"- 轮间极差（单轮读数的摆动幅度）：{'、'.join(spans)}")
    if agg.get("n_rep_rounds"):
        lines.append(
            f"- 复现极差越线：{agg['rep_threshold_crossings']}/{agg['n_rep_rounds']} 轮"
            "超过分差阈值——判定式「双评委分歧→仲裁」在这些轮里部分读的是评委自己的抖动"
        )
    if agg.get("papers_differ"):
        lines.append(
            "- ⚠️ 各轮参与的锚点条数不同（换过考卷）：合并读数是对**锚点总体**的估计依然成立，"
            "但轮间数值对比（含上表的逐轮趋势）不成立。"
        )
    if mae_ci and mae_ci[1] - mae_ci[0] > AGGREGATE_CI_WIDTH_NOTE:
        lines.append(
            f"- ⚠️ MAE 的 CI 宽 {round(mae_ci[1] - mae_ci[0], 2)}：两次校准的 MAE 差值小于"
            "这个宽度的，都不构成「评委变好/变坏」的证据。"
        )
    lines.append(
        "> 怎么读（口径见 docs/evaluation.md §十四）：CI 是「换一批锚点重测」的重抽不确定度，"
        "轮间极差是「这台仪表逐轮的漂移」——前者管比较的分辨率，后者管单轮读数能信多宽。"
        "两者都不收敛之前，任何单轮读数都是 ±极差量级的方向参考。"
    )
    return "\n".join(lines)


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
        "--scoring-mode",
        choices=("impression", "checklist"),
        default="impression",
        help="impression=现行五个 1-10 整数；checklist=二值判定 + 代码算分（两套协议的 A/B 入口）",
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

    analysis, errors = calibrate(samples, args.judge, args.scoring_mode)
    for sid, err in errors:
        print(f"  [SKIP] {sid}：{err}", file=sys.stderr)
    if not analysis:
        print("全部锚点评估失败，无法校准（先跑 run.py --preflight 检查端点）", file=sys.stderr)
        return 1
    print(render_report(args.judge, analysis))
    if args.repeat >= 2:
        rep = repeatability(samples, args.judge, args.repeat, args.scoring_mode)
        analysis["repeatability"] = rep
        print(render_repeatability(rep))
    return 0


if __name__ == "__main__":
    # 编码兜底放在这里而不是 import 期：`run.py calibrate` 会 import 本模块，
    # 在 import 期动 sys.stdout 等于把引导副作用塞进别人的启动路径（旧脚本就是这么写的）。
    ensure_utf8_stdio()
    raise SystemExit(main())
