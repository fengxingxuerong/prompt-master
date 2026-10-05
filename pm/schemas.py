"""
数据契约层：所有 LLM 节点的输出都用 Pydantic 模型约束。

设计要点（针对原文档的缺陷修复）：
1. 原文档在 JSON 模板里写 `"is_clear": boolean`、`"score": 1-10` 这类类型占位符，
   模型经常原样输出字面量导致解析失败。这里改用 Pydantic -> JSON Schema，
   通过 function calling / structured output 强制约束，模型无法输出非法类型。
2. 原文档给了 5 个维度权重却没给汇总公式，导致维度分与总分自相矛盾。
   这里把加权公式固化在代码里（WEIGHTS + compute_weighted_score），
   模型自报分只用于监控偏差，不作为判定依据。
"""

from __future__ import annotations

import json
import math
import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationInfo, field_validator, model_validator

# --------------------------------------------------------------------------
# 评分权重：与原文档 <evaluation_criteria> 保持一致，但固化成可执行常量
# --------------------------------------------------------------------------
WEIGHTS: dict[str, float] = {
    "task_completion": 0.25,
    "format_adherence": 0.20,
    "constraint_compliance": 0.25,
    "robustness": 0.15,
    "quality": 0.15,
}


def _env_str(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _env_float(name: str, default: float, *, positive_only: bool = False) -> float:
    """容错读浮点：空值/非法值回退默认，不让一个手抖的 .env 炸掉整条链（A2 同源问题）。"""
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        val = float(raw)
    except ValueError:
        return default
    return val if (not positive_only or val > 0) else default


PASS_THRESHOLD = _env_float("PM_PASS_THRESHOLD", 8.0, positive_only=True)  # >= 阈值视为生产可用

# 评委抖动（复现性）：同一份输入重复打分时量到的**最大极差**。
# 只有 `run.py calibrate --judge X --repeat 3` 量出来之后才填得进去；不填=0，行为与旧版一致。
JUDGE_JITTER = _env_float("PM_JUDGE_JITTER", 0.0)
# 极差 → 标准差的换算因子 d2(n)，随采样次数走（n 越大极差越"撞运气"，除得越多）。
# ⚠️ 曾经硬编码 `_RANGE_TO_SD = 1.693`（= d2(3)），而 `--repeat` 是可配的：
#    于是 `--repeat 5` 量出来的数会被当成 sd 直接用，比真实值松 2.326/1.693≈1.37 倍
#    ——**多花 5 倍钱去校准，仲裁反而更容易触发**，方向是反的。
#    反过来 k=2 偏严 1.50 倍、k=8 偏松 0.60 倍。
_D2_BY_N = {2: 1.128, 3: 1.693, 4: 2.059, 5: 2.326, 6: 2.534, 7: 2.704, 8: 2.847}
# 兼容旧名字：n=3 那一个值仍然是默认口径（`.env.example` 教用户 `--repeat 3` 量）。
_RANGE_TO_SD = _D2_BY_N[3]


def _range_to_sd(n_samples: int) -> float:
    """把极差换算成 sd，d2 随采样次数查表。

    k>8 走 `1.25 * n` 外推（d2 的渐近值是 √2n，1.25n 是一条经验直线）。
    ⚠️ **这条外推没有实测校验过**（账本里最大只到 n=8），仅作兜底；
    真要精确请重新 `calibrate --repeat <k>` 并把 n 加进表里。
    """
    if n_samples in _D2_BY_N:
        return _D2_BY_N[n_samples]
    return round(1.25 * max(2, n_samples), 3)


# 三把评委的复现性差着数量级（实测平均极差：A 0.475、B 1.9、仲裁 2.5），一个全局数
# 会把最抖那把的误差棒套在所有人头上。`PM_<ROLE>_JITTER` 单独测过再填，只用于
# **仲裁触发线**（那条线读的就是两位各自的手抖）；`ci_lower` 仍用全局值，取保守口径。
_JITTER_SEATS = ("evaluator", "evaluator_b", "arbiter")


def _jitter_ledger_path() -> Path:
    """校准账本路径（`run.py calibrate` 每次实测都往里追加一条）。

    ⚠️ 必须走 `pm.cli.support.log_dir()`（每次现读 `PM_LOG_DIR`），不能自己
    `Path(os.getenv("PM_LOG_DIR", "logs"))`：`calibrate.py` 写账本用的就是前者，
    两处口径分叉过一次（"我把产物挪到别处了"之后，写账本跟着走、读账本没跟）。
    这里曾读死 `logs/`，于是测试把 `PM_LOG_DIR` 指到 tmp 之后，
    读的仍是仓库里那本**真实**账本（`evaluator` 0.54），噪声带悄悄带上了它。
    """
    from .cli.support import log_dir  # 局部 import：晚绑定 PM_LOG_DIR

    return log_dir() / "judge_calibration_history.json"


def current_judge_model(seat: str = "evaluator") -> str:
    """当前这把评委用的模型名（读不到时返回 `(unknown)`）。

    给 `_measured_jitter` 当**仪表身份**用：账本条目的 `model` 与它不一致时，
    那不是当前这把尺子的实测（换过评委/换过 rubric 都算）。
    与 `calibrate._calib_fingerprints()` 同源同判据 —— 两处必须对同一本账本给
    同一个答案，否则"校准说不算、噪声带照算"（见 `_measured_jitter` 的事故注记）。
    """
    try:
        from .llm import build_config

        return build_config(seat).model
    except Exception:  # noqa: BLE001 - 读不到配置不该让噪声带崩掉，按 unknown 过滤
        return "(unknown)"


@lru_cache(maxsize=16)
def _measured_jitter(
    ledger_path: str, model: str | None = None
) -> dict[str, tuple[float, int, str]]:
    """从账本里读**当前这把尺子**在各座位的实测极差。

    返回 `{seat: (rep_range_mean, n_samples, ts)}`。
    ⚠️ `n_samples` 必须**一起**读出来：换算成 sd 要用 d2(n)，而 n 各条目可能不同
    （早期账本条目没有 `rep_times`，只有默认的 3）。

    ⚠️⚠️ **必须按仪表身份过滤**（2026-10-05 实测出来的缺陷）：
    账本现存那条 `{judge, rep_range_mean: 0.54, ts}` **没有 `model` 字段**，
    而 `calibration.aggregate_history` 按 `model` 过滤、明确把它判为"不是当前仪表"，
    返回 `None`。同一本账本，本函数却把 0.54 当成 evaluator 的实测计进噪声带 ——
    **两本读者给出相反答案**，而噪声带是产品侧唯一会引用的那个。

    这正是 §十八 要挡的那类错误（拿不属于当前仪表的数当实测），只是换了个入口：
    §十八 是"配置常数冒充实测"，这次是"**另一把尺子**的实测冒充当前尺子的实测"。
    换型（换评委模型 / 换 rubric）之后旧记录留着是正常的，不该删——
    该做的是不把它们算进当前仪表的噪声带。

    ⚠️ `model=None` **不是"{不过滤}”，而是"{无法确定当前仪表是谁} ⇒ 一条都不算。
    留一个不过滤的默认值等于给这个缺陷留后门：本轮第一版把 `model` 默认成 None，
    而判据写成 `(entry.get("model") or "") != (model or "")` —— 在 `model=None` 时
    缺 model 的条目被判成"{匹配}，刚修掉的 bug 从参数默认值绕了回来。
    调用方必须说清自己认为当前是哪把尺子；说不清就一条都不算。
    该做的是不把它们算进当前仪表的噪声带。
    """
    if not model:
        return {}
    try:
        raw = json.loads(Path(ledger_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, list):
        return {}
    out: dict[str, tuple[float, int, str]] = {}
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        seat = entry.get("judge")
        rng = entry.get("rep_range_mean")
        if not isinstance(seat, str) or not isinstance(rng, (int, float)):
            continue
        # 仪表身份不匹配的一律不算当前实测。`model` 缺失同样不算：
        # 无法证明它是这把尺子量的，就不能当这把尺子的读数用（宁可无，不可伪）。
        if (entry.get("model") or "") != model:
            continue
        out[seat] = (float(rng), _repeat_count(entry.get("rep_times")), str(entry.get("ts", "")))
    return out


def _repeat_count(times: object) -> int:
    """账本条目里的重复次数，认两种记法，越界一律回落 3。

    `rep_times` 早期记成**列表**（重复打分的原始分），后来记成**整数**（次数）。
    只认 list 会把所有整数记法的条目当成 n=3，d2 就用错了；只认 int 则反过来。
    `bool` 是 `int` 的子类，必须先挡掉（`True` 不该被读成"重复 1 次"）。

    独立成函数而不是留在 `_measured_jitter` 的循环里：留在那儿会让它的
    圈复杂度越过 CC 10 而进复杂度台账（`tests/test_complexity_ratchet.py`）——
    而这里真正需要登记的是"形状容错"这个决定，不是它的分支数。
    """
    if isinstance(times, bool):
        return 3
    if isinstance(times, int):
        return times if times >= 2 else 3
    if isinstance(times, list):
        return len(times) if len(times) >= 2 else 3
    return 3


def _seat_sd(seat: str, measured: dict[str, tuple[float, int, str]]) -> float:
    """某个座位的抖动，**已经换算成 sd**。

    ⚠️⚠️ 三条取值路径全都是**极差**，都必须除以 `_range_to_sd`——
    曾经只除了账本那一条，把 env/全局当成 sd 直接用，于是：
    填了 `.env` 的本机仲裁线从 3.18 抬到 5.39，越过 selftest 场景 3 的分差 5.0，
    **`[FAIL] 双评委分歧触发了仲裁`**——而 1354 条单测全绿，什么都没抓到
    （单测把座位键 delenv 了，selftest 读宿主 `.env`）。
    `.env.example:116` 明确写了那些值是 `--repeat 3` 量出来的极差。
    """
    if seat in _JITTER_SEATS:
        own = _env_float(f"PM_{seat.upper()}_JITTER", -1.0)
        if own >= 0:
            return own / _range_to_sd(3)  # env 的填写口径来自 `--repeat 3`
    hit = measured.get(seat)
    if hit is not None:
        rng, n, _ts = hit
        return rng / _range_to_sd(n)  # 账本自带真实 n
    return JUDGE_JITTER / _range_to_sd(3)


def _measured_jitter_band(seat: str = "") -> float:
    """账本里各座位的实测极差（**不换算**），`seat=""` 时取投票席最大。

    与 `_measured_jitter_sd()` 的区别只有一处，但那一处有后果：
    · `_seat_sd()` 读的是 sd（极差 ÷ d2）→ 给**仲裁触发线**，那条线按 2σ 走；
    · 本函数读的是极差 → 给**噪声带**，那里 `measurement_noise` 用的是 `hypot`，
      按 §十八 的审计口径与目标模型采样极差**同尺度**直接合成。

    早先在这里调了 `_measured_jitter_sd()`，带宽被悄悄除了一次 d2（1.594 → 1.534），
    即噪声带**变窄 3.8%**，`ci_lower` 随之乐观。两个函数名的差别就是这个除号。
    """
    measured = _measured_jitter(str(_jitter_ledger_path()), current_judge_model("evaluator"))
    if seat:
        hit = measured.get(seat)
        return hit[0] if hit else 0.0
    return max(
        (measured[s][0] for s in _JITTER_SEATS if s != "arbiter" and s in measured),
        default=0.0,
    )


def _measured_jitter_sd(seat: str = "") -> float:
    """账本里各座位的**实测抖动**（sd）；`seat=""` 时取投票席的最大值。

    只认账本实测值——冻结常数不算"测过"。与 `_seat_sd` 不同，这里**完全不回退**
    到 env / 全局：调用方要回答的是"到底测过没有"，回退会把"配了个数"又算成实测。
    （曾在这里调 `_seat_sd()`，于是只配了 `.env` 的机器上 2.6 照样进带——正是
    §十八 要挡的那件事。）
    """
    measured = _measured_jitter(str(_jitter_ledger_path()), current_judge_model("evaluator"))
    if seat:
        hit = measured.get(seat)
        return hit[0] / _range_to_sd(hit[1]) if hit else 0.0
    seats = [s for s in _JITTER_SEATS if s != "arbiter"]
    return max(
        (measured[s][0] / _range_to_sd(measured[s][1]) for s in seats if s in measured),
        default=0.0,
    )


def judge_jitter(seat: str = "") -> float:
    """该评委座位的复现性极差；没单独测过就回退全局 `PM_JUDGE_JITTER`。

    ⚠️ 返回的是**极差**，不是 sd —— 换算是消费方的事（见 `_seat_sd`），
    在这里换一遍会让调用方再除一次（d2 被除两次）。

    显式填了 `PM_<ROLE>_JITTER=0` 按 0 处理（"这把测过、确实不抖"），与"没填"不同。
    """
    if seat in _JITTER_SEATS:
        own = _env_float(f"PM_{seat.upper()}_JITTER", -1.0)
        if own >= 0:
            return own
    measured = _measured_jitter(
        str(_jitter_ledger_path()), current_judge_model(seat if seat else "evaluator")
    )
    hit = measured.get(seat) if seat else None
    if hit is not None:
        return hit[0]
    return JUDGE_JITTER


def measurement_noise(target_noise: float | None) -> float | None:
    """把目标模型采样噪声与**评委打分噪声**合成一条噪声带。

    两者独立，按方差相加。评委这一项此前完全没进不确定度：报告的 `ci_lower` 只吸收
    "同一用例重复采样"的极差，而实测同一份输入让评委 B 重复打能差 2.60、仲裁差 4.43，
    且 `temperature=0` 不改善——所以"保守下界"其实不保守。

    ⚠️ `None` **必须原样透传**（曾在这里返回 `JUDGE_JITTER`）：
    `None` 的含义是"采样次数不足以估计噪声"，调用方据此把 `REGRESSION_MARGIN`
    换成更大的 `UNESTIMATED_MARGIN`（0.5）并**关闭平台期判定**。
    在这里吞掉它，那条降级路径就永远走不到——单次采样会照常宣布"平台期"。

    ⚠️ 评委那一项取的是**有出处**的抖动（`jitter_provenance().jitter`），
    不是 import 期冻结的 `JUDGE_JITTER` —— 后者分不出"实测过的"和"配出来的"，
    而 §十八 的归因审计证明：60.6% 的噪声带来自那个从未被校准验证过的配置常数。

    ⚠️ 用 `_measured_jitter_band()`（**极差**，与采样极差同尺度）而不是
    `_measured_jitter_sd()`（sd）：噪声带是 `hypot` 合成，§十八 的审计读数
    （1.138 / 2.891）就是按同尺度口径算的，换成 sd 会让带窄一截而无人察觉。
    """
    return measurement_noise_with_jitter(target_noise, _measured_jitter_band())


def measurement_noise_with_jitter(target_noise: float | None, jitter: float) -> float | None:
    """与 `measurement_noise` 同式，但用**调用方给定的**评委抖动（已经过出处过滤）。

    分成两个函数是因为：全局 `JUDGE_JITTER` 是 import 期冻结的常量，分不出
    "实测过的"与"配出来的"，而这正是 §十八 要切开的那一刀。
    """
    if jitter <= 0:
        return target_noise
    # ⚠️ `None` 是"采样次数不足以估计噪声"的哨兵，**即使评委抖动有实测值也必须透传**：
    # 它的含义是"这一项压根没测出来"，拿别的项去补会把"测不出"改写成"很小"。
    if target_noise is None:
        return None
    return round(math.hypot(target_noise, jitter), 3)


def effective_disagreement_threshold(base: float | None = None) -> float:
    """仲裁的真实触发线：取"配置值"与"评委自我分歧能造出的分差"之中较大者。

    两位独立评委各带 sd 的抖动时，其**差**的 sd 是 √(sdA²+sdB²)；越过 2σ 才算真分歧
    （1σ 会有约 32% 误触）。sd 由 `_seat_sd()` 给（**三条取值路径都按极差除 d2**）。
    默认两座位都是 0 ⇒ 结果完全等于 `PM_JUDGE_DISAGREEMENT`；填了实测值只会让门更严，
    绝不会把已经存在的仲裁放松。门一旦被抬到无穷高，等于宣布"这套评委测不出分歧信号"，
    报告里会说破，不会让人误以为双评委在交叉验证。

    为什么按座位分别取数（2026-09-20）：三把评委的复现性差着数量级，只填一个全局值时
    最抖的那把会替所有人决定这条线——实测 A 0.475 / B 1.9 时，全局按 2.6 会把触发线
    抬到 4.35（几乎不再仲裁），而按两位各自的抖动算只有 3.18。

    为什么**不含仲裁席**：三席形式会把这条线推到 6.12，而仲裁自身的极差只有 4.43
    ——永远触发不了仲裁。那样等于把仲裁功能静默关掉却让门看起来更严。
    """
    configured = judge_disagreement_threshold() if base is None else base
    measured = _measured_jitter(str(_jitter_ledger_path()), current_judge_model("evaluator"))
    sd_a = _seat_sd("evaluator", measured)
    sd_b = _seat_sd("evaluator_b", measured)
    if sd_a <= 0 and sd_b <= 0:
        return configured
    diff_sd = math.sqrt(sd_a**2 + sd_b**2)
    return max(configured, round(2.0 * diff_sd, 2))


# --------------------------------------------------------------------------
# P3: 修订提前终止（回退 / 平台期）—— 来自真实 revise 收敛实验的教训
# v0 8.63 → v1 8.93 → v2 8.02 → v3 8.02：修订器忠实响应反馈，
# 但 LLM 评估器标准摇摆导致分数非单调，继续修只会浪费迭代预算。
# --------------------------------------------------------------------------
REGRESSION_MARGIN = 0.2  # 回退判定：当前轮 avg 低于历史最佳超过该值
PLATEAU_MARGIN = 0.2  # 平台期判定：连续两轮未能超过历史最佳（含该余量）
# 噪声不可估（每条用例只采样 1 次）时，0.2 这个量级完全落在抖动里：
# 要么把判定放宽到看得见的差距，要么就别下“平台期”这种结论。
UNESTIMATED_MARGIN = _env_float("PM_UNESTIMATED_MARGIN", 0.5, positive_only=True)

# 采样噪声阈值：同一用例重复采样的分差超过该值 → 该用例结论标记为不稳
UNSTABLE_SPREAD = _env_float("PM_UNSTABLE_SPREAD", 1.5, positive_only=True)
# 评委自报分与代码加权分的平均偏差超过该值 → 判定为系统性放水
JUDGE_BIAS_ALERT = _env_float("PM_JUDGE_BIAS_ALERT", 1.0, positive_only=True)

# 评分量纲下限（1-10 制）。分数恰好等于它**且**极差为 0 的用例只可能来自失败兜底
# ——连接失败 / 空输出时评委没有可评的东西，给的是下限而不是一次真实测量。
# 这个常量只服务 §三十·一 的 `untrusted_case_indices`，不参与任何达标判定。
_MIN_VALID_SCORE = 1.0


def judge_disagreement_threshold() -> float:
    """双评委加权分差超过它 → 走仲裁。**调用时读 env**，所以测试与按角色覆盖都有效。

    放在这里而不是只留在 nodes/judge.py：复现性测量（calibrate_judge --repeat）必须拿它
    当参照才能说"抖动是否淹没阈值"，而 calibrate_judge 刻意不 import pm.nodes（那会把
    langgraph 整条依赖链拉进一个几块钱的校准命令）。两处各写一份 2.0 一定会漂，故共用此源。
    """
    try:
        return max(0.1, float(os.getenv("PM_JUDGE_DISAGREEMENT", "2.0")))
    except ValueError:
        return 2.0


def noise_margin(noise: float | None = 0.0) -> float:
    """迭代收益判定余量：至少 2× 采样噪声。

    旧版写死 0.2，而评委自身抖动就 >0.2，导致“没提升”与“测不出提升”无法区分。

    `noise=None` 表示**噪声不可估**（`PM_SAMPLES_PER_CASE=1`，没有极差可依据）：
    此时不能把 0.2 当成“真实回退”的依据，否则一次运气好的采样就能把修订提前卡死，
    所以退到 `UNESTIMATED_MARGIN`（默认 0.5）并关掉平台期规则。
    """
    if noise is None:
        return max(PLATEAU_MARGIN, UNESTIMATED_MARGIN)
    return max(PLATEAU_MARGIN, 2.0 * max(0.0, float(noise or 0.0)))


def early_stop_reason(avg_scores: list[float], noise: float | None = 0.0) -> str | None:
    """根据版本分数轨迹判断是否应提前终止修订。

    avg_scores: 按版本顺序的各轮 avg 分（最后一个是当前轮）。
    noise: 采样噪声（平均极差）；传 None 表示采样次数不足以估计噪声。
    返回终止原因（str）或 None（应继续修订）。

    规则：
    1. 回退：当前轮低于历史最佳超过余量 —— 继续修大概率随评估标准摇摆震荡，
       直接交付历史最佳版本。
    2. 平台期：最近两轮都未能超过更早的历史最佳（含余量）—— 修订已无实质提升。
       **仅在噪声可估（k≥2）时才启用**：单次采样下“连续两轮没超过”完全可能是噪声。
    余量 = max(REGRESSION_MARGIN, 2×noise)：噪声大时不能拿 0.2 当“真实回退”的依据。
    """
    if len(avg_scores) < 2:
        return None
    if any(s is None for s in avg_scores):  # 容错：未完成评分的版本不参与判定
        avg_scores = [s for s in avg_scores if s is not None]
        if len(avg_scores) < 2:
            return None
    # 余量拉开到噪声之上：否则“测不出提升”会被当成“没有提升”而提前停下
    margin = noise_margin(noise)
    cur = avg_scores[-1]
    best_prev = max(avg_scores[:-1])
    if best_prev - cur > margin:
        return f"修订回退：当前 {cur} 低于历史最佳 {best_prev}（余量 {round(margin, 2)}）"
    if len(avg_scores) >= 3 and noise is not None:
        best_before_last2 = max(avg_scores[:-2])
        if cur <= best_before_last2 + margin and avg_scores[-2] <= best_before_last2 + margin:
            return (
                f"平台期：连续两轮未能超过历史最佳 {best_before_last2}（余量 {round(margin, 2)}）"
            )
    return None


def _unwrap_json_blob(text: str) -> str:
    """把 `'{"type": "...", "description": "..."}'` 这种"字符串里装 JSON"摊回可读文本。

    真实数据（2026-09-18，sales_mockgen 一轮）：评委把 issues 写成**一个 JSON 字符串**
    而不是普通句子，修订器与报告里就出现一整段带引号花括号的噪声——取证内容本身是有用的。
    只在"看起来确实是 JSON 对象"时才摊平，普通文本原样返回。
    """
    s = (text or "").strip()
    if not (s.startswith("{") and s.endswith("}")):
        return text
    try:
        data = json.loads(s)
    except (ValueError, TypeError):
        return text
    if not isinstance(data, dict):
        return text
    bits = [
        f"{k}：{data[k]}"
        for k in ("description", "issue", "problem", "detail", "text")
        if data.get(k)
    ]
    if not bits:
        bits = [f"{k}：{v}" for k, v in data.items() if isinstance(v, (str, int, float))]
    return "；".join(str(b) for b in bits) if bits else text


def compute_weighted_score(dims: DimensionScores) -> float:
    """按固定权重计算总分。判定以本函数结果为准，不信任模型自报分。"""
    raw = {
        "task_completion": dims.task_completion,
        "format_adherence": dims.format_adherence,
        "constraint_compliance": dims.constraint_compliance,
        "robustness": dims.robustness,
        "quality": dims.quality,
    }
    total = sum(WEIGHTS[k] * v for k, v in raw.items())
    return round(total, 2)


# --------------------------------------------------------------------------
# Node 1: 需求澄清
# --------------------------------------------------------------------------
class InferredContext(BaseModel):
    target_audience: str = Field(description="推断的目标受众")
    domain: str = Field(description="推断的领域")
    output_format: str = Field(description="推断的输出格式")
    tone: str = Field(description="推断的语气")
    constraints: list[str] = Field(default_factory=list, description="推断的其他约束")


class ClarificationResult(BaseModel):
    is_clear: bool = Field(description="需求是否已足够清晰，可直接生成提示词")
    task_summary: str = Field(description="对需求的简洁概括，1-2 句话")
    inferred_context: InferredContext = Field(description="基于理解的推断上下文，均为猜测值")
    clarifying_questions: list[str] = Field(
        default_factory=list,
        description="最多 3 个最关键澄清问题；需求清晰时为空列表",
        max_length=3,
    )
    suggested_next_step: Literal["proceed_to_optimizer", "ask_user"] = Field(
        description="下一步建议"
    )

    @field_validator("clarifying_questions")
    @classmethod
    def _trim(cls, v: list[str]) -> list[str]:
        return v[:3]


# --------------------------------------------------------------------------
# Node 5: 评估
# --------------------------------------------------------------------------
class DimensionScores(BaseModel):
    # float：单个评委填整数；双评委合并时可能出现 .5，允许小数保持精度
    task_completion: float = Field(ge=1, le=10, description="任务完成度")
    format_adherence: float = Field(ge=1, le=10, description="格式遵从")
    constraint_compliance: float = Field(ge=1, le=10, description="约束遵守")
    robustness: float = Field(ge=1, le=10, description="鲁棒性：幻觉/矛盾/越界猜测")
    quality: float = Field(ge=1, le=10, description="质量与深度")

    @field_validator("*", mode="before")
    @classmethod
    def _unwrap_score_objects(cls, v: Any) -> Any:
        """维度值写成 `{"score": 3, "reasoning": "…"}` 时也要认，别作废整轮评估。

        真跑实测（2026-09-19，仲裁 deepseek-v4-flash）：10 次把维度值包成对象，
        内层键名还在 `reasoning` / `evidence` 之间漂。这实际上是模型在**逐维给证据**——
        正是评分提示词要的"先取证后打分"——结果被 schema 判成非法输出、整轮重来（多一次
        计费往返 + 几十秒退避）。与 `_adopt_near_miss_keys` 同一族：形状抖动在解析层收，
        不放大成结论错误。证据文本这里刻意丢弃：判定只用数值，文本要留应去 `issues`。
        """
        if isinstance(v, dict):
            for key in ("score", "value", "points", "rating", "raw"):
                if key in v and not isinstance(v[key], (dict, list)):
                    return v[key]
        return v

    @model_validator(mode="before")
    @classmethod
    def _adopt_paraphrased_keys(cls, data: Any) -> Any:
        return _adopt_near_miss_keys(cls, data)


def _adopt_near_miss_keys(cls: type[BaseModel], data: Any) -> Any:
    """把模型改写过的键名认回规范名，别让一次同义替换作废整条评估。

    实测（2026-09-18，deepseek-v4-flash 做仲裁）：`quality` 的 description 写的是
    「质量与深度」，模型就把键吐成 `quality_depth` → 必填维度缺失 → ValidationError，
    这一轮仲裁连同前面双评委的 4 次计费调用全部报废。description 是喂给模型的措辞，
    它反过来被当成键名，是 schema 自身的产物，不是模型的随机错误——同类问题在
    `issues`（对象数组）上已经用 `_flatten_evidence_items` 处理过。

    三重收窄，缺一个就不认：
    1. 目标必须是**必填**字段。可选字段本有默认值，认错了会把一次成功解析改成类型错误
       （例如把 `samples: [...]` 塞进可选的 `n_samples: int`）；
    2. 候选必须**唯一**（子串双向匹配）。"取第一个未知键"会把两个维度并成同一个数，
       那才是把格式问题放大成结论错误；
    3. 目标必须**当前缺失或为 null**，不覆盖模型已经写对的值。
    """
    if not isinstance(data, dict):
        return data
    names = {f for f, mi in cls.model_fields.items() if mi.is_required()}
    extra = [k for k in data if k not in cls.model_fields and data[k] is not None]
    if not extra:
        return data
    out = dict(data)
    for key in extra:
        candidates = [f for f in names if f != key and (f in key or key in f)]
        if len(candidates) == 1 and out.get(candidates[0]) is None:
            out[candidates[0]] = out.pop(key)
    return out


class RuleCheck(BaseModel):
    """评委对**语义规则**的逐条核验（`--assert-mode rule`）。

    为什么需要它：`contains`/`exact` 只能比字面片段，而大多数 ground-truth 标注写的是
    “必须标注缺失项”这类规则——对它们做确定性比对永远命不中，只会把基线与优化版
    一起打死。规则就交给评委判，但**与打分解耦**：它只回答“满足没满足”，
    不影响维度分（否则同一件事被计入两次）。
    """

    rule: str = Field(description="规则原文（从核对清单里原样抄回来）")
    satisfied: bool = Field(description="输出是否满足该条规则")
    evidence: str = Field(
        default="", description="判据：从测试输出里摘的一句证据；找不到写“未找到”"
    )


class CheckVerdict(BaseModel):
    """评委对清单里**一条**的二值判定。

    为什么是二值而不是 1-10：实测评委的复现性问题不是"围绕真值抖"，是**每次调用先选一套
    口径**（仲裁同一输入稳定落在 5.15 与 7.10 两个模式，`temperature=0` 无效）。
    五个 1-10 整数给了它 10^5 种"口径组合"，而"这条约束满足没有"是它能稳定回答的题型。
    """

    item: str = Field(description="清单条目原文（照抄回来，代码按文本回配）")
    satisfied: bool = Field(description="被测输出是否满足该条")
    evidence: str = Field(default="", description="从测试输出摘的一句证据；找不到写“未找到”")


class UnsourcedClaim(BaseModel):
    """输出里一个"输入中找不到来源"的断言及其推导依据。

    候选清单由代码给出（数字回查），评委只能**解释**不能**省略**：漏报按违规处理。
    这条针对的是校准实测里唯一那条漏放——「各月均在均值 ±2 倍标准差内」没有任何 σ 计算
    支撑，评委给了 8.6~8.85 而人工只给 7.0；它落在判定线之上，是闭环会真金白银放行的那种。
    """

    claim: str = Field(description="输出里那句无直接来源的断言/数值，原文摘出")
    basis: str = Field(
        default="",
        description="它是怎么从输入算出来的（写得出算式就写算式）；写不出来就是编造，留空",
    )


class ChecklistEvaluation(BaseModel):
    """判定式评分协议下评委的完整输出：**没有任何 1-10 的自由整数**。

    分数由 `pm.scoring.score_checklist()` 从这些二值判定算出来，所以维度分与总分
    不可能自相矛盾（印象式下"issues 说编造、鲁棒性给 8"这类形态在结构上消失了）。
    唯一保留的自由判断是 `quality_band`——内容深度确实无法二值化，但把它从
    1-10 收成 5 个具名档位，也砍掉了同一维度上 2/3 的可跳空间。

    `n_items_checked` 排在最前不是凑数：文本降级通道上实测到模型直接吐**裸数组**
    `[{...verdict...}, ...]`，于是解析器把数组第一项当成顶层对象校验，报
    "verdicts / quality_band 缺失"。一个开头的标量把响应钉回对象形状，
    顺带当漏答交叉核对（声明条数 ≠ 清单条数即留痕）。
    """

    n_items_checked: int = Field(
        ge=1,
        description="本次核对的清单条目数（照抄清单条数）。先写它，再逐条判定",
    )
    verdicts: list[CheckVerdict] = Field(description="对核对清单的逐条二值判定，一条不落")
    unsourced: list[UnsourcedClaim] = Field(
        default_factory=list,
        description="代码点名的每个无来源断言都要在这里解释；未列出的按编造处理",
    )
    quality_band: int = Field(ge=1, le=5, description="质量与深度档位（1 最低、5 最高）")
    issues: list[str] = Field(
        default_factory=list, description="未满足条目与编造问题的成文说明（给修订环节看）"
    )
    suggestions: list[str] = Field(default_factory=list, description="提示词层面的修改动作")


class EvaluationResult(BaseModel):
    """评委的结构化输出。**字段声明顺序即生成顺序**，不要随手重排。

    `issues` / `suggestions` 必须排在 `dimension_scores` 之前：自回归生成下，
    JSON 属性按 schema 顺序逐字产出，若分数在前，评委就在拿到证据之前把分数 commit 了，
    EVALUATOR_SYSTEM 的「先取证、后打分」流程在结构上无法执行（只能先给数再回头补取证）。

    反向教训：**排在末尾的键不要设成必填**。把 issues 提成必填后模型先吐一大段取证文本，
    尾部的键更容易被漏掉或被 max_tokens 截断（2026-09-18 真端点实测：`should_revise`
    缺失 2 次、直接作废 1 条评估）。所以 `should_revise` 带默认值——它本就是咨询性建议，
    是否修订由代码按加权分决定。
    """

    issues: list[str] = Field(
        description="具体问题描述，每条含测试输出的原文证据；确实没有问题时给空列表"
    )
    suggestions: list[str] = Field(
        description="可操作的改进建议，指向提示词层面的修改动作；没有则给空列表"
    )
    dimension_scores: DimensionScores
    model_reported_score: float = Field(
        ge=1, le=10, description="模型自报总分，仅用于监控评分偏差，不参与判定"
    )
    should_revise: bool = Field(
        default=False,
        description="模型建议是否需要修订（咨询性）；未达标时代码会强制置为 True",
    )

    @field_validator("issues", "suggestions", mode="before")
    @classmethod
    def _flatten_evidence_items(cls, v: Any) -> Any:
        """把 `[{"issue": "..."}]` 这类对象条目压回字符串。

        实测（2026-09-18，deepseek-v4-flash 做评委）：把 issues 提到 schema 首位并设为必填后，
        模型更爱把它"结构化"成对象数组——`list[str]` 直接 ValidationError，
        整位评委的这条评估作废（judge.py 按异常计 1 分）。取证内容本身是好的，
        不该因为包装形状丢掉，这里按常见文本键取一次。
        """
        if v is None:
            return []  # 数组键写成 null 也是形状抖动，不是"没有结论"
        if not isinstance(v, list):
            return v
        out: list[Any] = []
        for item in v:
            if isinstance(item, dict):
                for key in (
                    "issue",
                    "text",
                    "description",
                    "detail",
                    "problem",
                    "content",
                    "reason",
                ):
                    if isinstance(item.get(key), str) and item[key].strip():
                        out.append(item[key].strip())
                        break
                else:
                    parts = [str(x) for x in item.values() if isinstance(x, (str, int, float))]
                    out.append("：".join(parts) if parts else str(item))
            elif isinstance(item, str):
                out.append(_unwrap_json_blob(item))
            elif item is not None:
                out.append(item)
        return out

    @model_validator(mode="before")
    @classmethod
    def _tolerate_missing_evidence_keys(cls, data: Any) -> Any:
        """必填是为了进骨架、逼模型先产证据；缺键不能在解析层炸。

        两面都要顾：
        - 声明为必填 → 出现在 `_schema_skeleton` 的照抄骨架里，且排在维度分之前，
          评委在两条通道上都是"先写证据再打分"；
        - 但有些端点的结构化输出会直接省掉空数组，或模型在长输出后被截断——
          此时按"没有问题"处理即可。让它抛 ValidationError 会让整位评委的评估
          报废（judge.py 按异常计入 errors 并给 1 分），把一个格式问题放大成结论错误。

        进来先做一次同义键认领（`dimension_scores` 被写成 `scores` 之类），
        理由与 `_adopt_near_miss_keys` 相同。
        """
        if isinstance(data, dict):
            data = _adopt_near_miss_keys(cls, data)
            data = dict(data)
            data.setdefault("issues", [])
            data.setdefault("suggestions", [])
        return data

    test_case_index: int = Field(default=0, description="本条评估对应的测试用例序号")
    rule_checks: list[RuleCheck] = Field(
        default_factory=list,
        description="<RULES> 清单的逐条核验结果；没给清单就留空",
    )

    # --- 代码侧派生字段（不交给模型填写） ---
    weighted_score: float = Field(default=0.0, description="按 WEIGHTS 加权计算的总分")
    passed: bool = Field(default=False, description="weighted_score >= PASS_THRESHOLD")

    # --- 重复采样元信息（修复“单次采样当结论”）---
    sample_scores: list[float] = Field(
        default_factory=list, description="同一用例各次采样的加权分（按采样序号）"
    )
    n_samples: int = Field(default=1, description="该用例实际采样次数")
    sample_index: int = Field(
        default=0, description="作为代表样本的采样序号（加权分最接近中位数的那一次）"
    )
    score_spread: float = Field(
        default=0.0, description="该用例采样分数的极差（max-min），反映 target/评委噪声"
    )

    # --- 双评委/仲裁元信息（P0: 单一 LLM 自评防放水） ---
    judge: str = Field(
        default="evaluator",
        description="最终评定来源：evaluator / evaluator_b / merged / arbiter / conservative / system",
    )
    judge_scores: dict[str, float] = Field(
        default_factory=dict, description="各评委的加权分（评委名 → 加权分），用于追溯"
    )
    judge_disagreement: float | None = Field(
        default=None, description="双评委加权分差；超过阈值时触发仲裁"
    )
    cache_hit: bool = Field(default=False, description="本条评估是否命中本地缓存")
    scoring_mode: str = Field(
        default="impression",
        description="本条分数出自哪套协议：impression（五个 1-10 整数）/ checklist（二值判定+代码算分）",
    )
    checklist_detail: dict[str, Any] = Field(
        default_factory=dict,
        description="判定式明细（违规条目、未答条目、封顶）；印象式下为空",
    )

    @field_validator(
        "judge", "judge_scores", "cache_hit", "sample_index", "should_revise", mode="before"
    )
    @classmethod
    def _coerce_code_side_fields(cls, v: Any, info: ValidationInfo) -> Any:
        """代码侧派生字段容错。

        真实 e2e 发现：模型不知道 judge_scores 等字段的含义，可能按 schema
        填 null，导致 Pydantic 校验失败、评估被 system 低分拖垮。
        这些字段本来就会由代码在 finalize/merge 时重写，模型填什么都无所谓，
        这里把 null 恢复为默认值即可。
        """
        if v is None:
            return {
                "judge": "evaluator",
                "judge_scores": {},
                "cache_hit": False,
                "sample_index": 0,
                # should_revise 是咨询性建议：合并时按"任一评委说要修就修"取 OR，
                # 且未达标的用例会被代码强制置 True，所以 null 回退默认值不丢判定。
                "should_revise": False,
            }.get(info.field_name)
        return v

    def finalize(self) -> EvaluationResult:
        """模型输出后由代码调用，重算总分并判定。"""
        self.weighted_score = compute_weighted_score(self.dimension_scores)
        self.passed = self.weighted_score >= PASS_THRESHOLD
        return self


class JitterProvenance(BaseModel):
    """抖动那个数的出处。**引用噪声带时必须连它一起引用。**

    §十八 的归因审计发现：`PM_JUDGE_JITTER=2.6` 冻结在 `.env` 后再没被任何一轮
    校准验证过，而它是合成噪声带的主项——真实采样噪声带 2.891 里有 60.6% 来自
    这个配置常数。于是 C2 判据（Δ > 本轮自报带）在很大程度上测的是这个常数
    而不是产品差异，剥掉后 7 轮里 3 轮翻转；同一个常数还经 `ci_lower` 进了达标判定，
    22 个臂里有 3 个的 `ci_lower` 闸因此翻转。

    四种出处：
    - `measured`：账本里有 `--repeat` 实测记录，**可以进带**；
    - `measured_override`：实测存在，但显式配置覆盖了它（要留痕）；
    - `unverified_config`：配了数但账本里查不到实测 → **不进带**，但必须披露；
    - `unmeasured`：什么都没配 → 0。
    """

    source: Literal["measured", "measured_override", "unverified_config", "unmeasured"] = (
        "unmeasured"
    )
    jitter: float = Field(default=0.0, description="计入噪声带的那个值（未验证配置时为 0）")
    configured: float | None = Field(
        default=None, description="配置里的原值（被排除时也留着，供报告披露）"
    )
    measured: bool = Field(default=False, description="账本里是否真有实测记录")
    n_samples: int = Field(default=0, description="实测的重复次数（d2 换算要用）")
    ts: str = Field(default="", description="最近一次实测的时间戳")
    seat: str = Field(default="", description="哪个座位")

    def as_dict(self) -> dict[str, Any]:
        return dict(self.model_dump())


def jitter_provenance(seat: str = "") -> JitterProvenance:
    """抖动取数的出处。`seat=""` 时按投票席（evaluator / evaluator_b）取较"重"的一条。

    ⚠️ 这条**只认账本实测**：`PM_JUDGE_JITTER` 这类冻结常数会原样记进 `configured`
    供披露，但 `jitter` 返回 0 —— 未验证的数不得进噪声带，也不得进 `ci_lower`。
    """
    measured = _measured_jitter(str(_jitter_ledger_path()), current_judge_model("evaluator"))
    configured_raw = JUDGE_JITTER
    if seat and seat in _JITTER_SEATS:
        own = _env_float(f"PM_{seat.upper()}_JITTER", -1.0)
        if own >= 0:
            configured_raw = own
    elif seat:
        configured_raw = 0.0

    seats = [seat] if seat else [s for s in _JITTER_SEATS if s != "arbiter"]
    hits = [measured[s] for s in seats if s in measured]
    if not hits:
        return JitterProvenance(
            source="unverified_config" if configured_raw else "unmeasured",
            jitter=0.0,
            configured=configured_raw if configured_raw else None,
            measured=False,
            seat=seat,
        )
    # 同一座位多条记录时取**最近**的一条（ts 同格式，字典序即时间序）。
    best = max(hits, key=lambda h: h[2])
    if configured_raw:
        return JitterProvenance(
            source="measured_override",
            jitter=configured_raw,
            configured=configured_raw,
            measured=True,
            n_samples=best[1],
            ts=best[2],
            seat=seat,
        )
    # ⚠️ 返回的是**极差**，不是 sd —— 换算是消费方的事（`_seat_sd` / `_measured_jitter_sd`）。
    # 在这里换一遍，下游再除一次就成了除两次 d2（3.18 → 1.88，静默变松一倍）。
    return JitterProvenance(
        source="measured",
        jitter=best[0],
        configured=None,
        measured=True,
        n_samples=best[1],
        ts=best[2],
        seat=seat,
    )


class AggregateScore(BaseModel):
    """多条测试用例的聚合结果。avg 看整体，min 卡短板——任一用例崩了就不算过。

    `n_cases_expected` / `cases_complete`（修复 C1）：实际用例数少于声明条数时，
    单次采样噪声太大，分数不可信 —— 此时**无论多高都不判达标**，并在 issues 里说明原因。
    原设计只在提示词里要求「生成 N 条」，代码层没有兜住，模型只返 1 条也能“生产可用”。
    """

    avg_score: float
    min_score: float
    max_score: float
    n_cases: int
    n_cases_expected: int = 0
    cases_complete: bool = True
    n_passed: int
    passed: bool
    all_issues: list[str] = Field(default_factory=list)
    all_suggestions: list[str] = Field(default_factory=list)
    # 事实断言（ground-truth）聚合（LLM 分配评审追记）：确定性校验对达标有一票否决权
    n_assertions: int = 0
    n_assertions_failed: int = 0
    assertion_veto: bool = False
    # ---- 不确定度（重复采样 + 多用例）----
    n_samples: int = 1
    sem: float = Field(default=0.0, description="用例间标准误差：stdev(用例中位分)/sqrt(n_cases)")
    noise: float = Field(default=0.0, description="同一用例重复采样的平均极差（采样噪声）")
    # 0.0 是**二义读数**：既可能是"重复打分完全一致"（好），也可能是
    # "根本没重复采样、压根测不出来"（此时报 0.0 是在骗人）。
    # 默认 `PM_SAMPLES_PER_CASE=1` 落在后者——报告曾把这种 0.0 当成"噪声极小"印出来。
    untrusted_case_indices: list[int] = Field(
        default_factory=list,
        description=(
            "分数落在量纲下限且极差为 0 的用例下标 —— 连接失败/输出为空导致评委给 1 分。"
            "与 `noise_measurable` 分开回答两个问题（§三十·一）："
            "那个问『带测不测得出来』，这个问『这条数据本身可不可信』。"
        ),
    )
    noise_measurable: bool = Field(
        default=False, description="噪声带是测出来的，还是测不出来只好记 0"
    )
    noise_total: float = Field(
        default=0.0,
        description="合成噪声带（目标采样极差 ⊕ 评委复现性抖动）；ci_lower 用的是这个",
    )
    judge_jitter: float = Field(default=0.0, description="评委复现性极差（实测且有出处的才计入）")
    jitter_provenance: dict[str, Any] | None = Field(
        default=None, description="抖动取数出处：measured / unverified_config / unmeasured"
    )
    ci_lower: float = Field(default=0.0, description="均分的保守下界；passed 看这个而不是看点估计")
    unstable_cases: list[int] = Field(
        default_factory=list, description="采样间极差过大的用例序号：这些用例的结论不稳"
    )
    # ---- 评委可靠性 ----
    judge_bias: float = Field(
        default=0.0, description="mean(评委自报分 - 代码加权分)：持续为正且较大 = 评委在放水"
    )
    judge_bias_warning: bool = False

    @classmethod
    def from_evaluations(
        cls,
        evals: list[EvaluationResult],
        threshold: float = PASS_THRESHOLD,
        n_expected: int | None = None,
        assertions: dict[int, Any] | None = None,
    ) -> AggregateScore:
        """assertions: {test_case_index: AssertionResult.model_dump()}，仅含实际执行了断言的用例。"""
        expected = int(n_expected or 0)
        complete = expected <= 0 or len(evals) >= expected
        if not evals:
            return cls(
                avg_score=0.0,
                min_score=0.0,
                max_score=0.0,
                n_cases=0,
                n_cases_expected=expected,
                cases_complete=False,
                n_passed=0,
                passed=False,
                all_issues=[
                    f"[评估口径] 未产生任何评估结果（期望 {expected or 'N'} 条用例），不足以下结论"
                ],
            )
        scores = [e.weighted_score for e in evals]
        issues, suggestions = [], []
        for e in evals:
            for i in e.issues:
                issues.append(f"[case#{e.test_case_index}] {i}")
            for s in e.suggestions:
                suggestions.append(f"[case#{e.test_case_index}] {s}")
        if not complete:
            issues.insert(
                0,
                f"[评估口径] 用例数不足：期望 {expected} 条，实际只有 {len(evals)} 条。"
                "单次采样噪声过大，本轮不予判定为达标（需重新生成测试集后再评）。",
            )

        # ---- 事实断言（ground-truth）：一票否决 + 放水检测 ----
        # 键归一为 int：state 经检查点序列化后整数键可能变成 "0" 这样的字符串，
        # 那时 veto 仍按条数生效，但“评委高分 + 断言失败”的比对会静默对不上号。
        raw_assertions = assertions or {}
        assertions = {
            (int(k) if isinstance(k, str) and k.strip().lstrip("-").isdigit() else k): v
            for k, v in raw_assertions.items()
        }
        # 疑似“把规则当片段”的断言只提醒不否决：否则用户一个笔误就能让整轮优化永远不达标，
        # 而且基线与优化版会同款失败，对比彻底失去信息量（真实跑踩过一次）。
        advisory_items = [(k, a) for k, a in assertions.items() if (a or {}).get("advisory")]
        scoring = {k: a for k, a in assertions.items() if not (a or {}).get("advisory")}
        n_assert = len(scoring)
        failed_items = [(k, a) for k, a in scoring.items() if not (a or {}).get("passed")]
        failed_keys = {k for k, _ in failed_items}
        n_assert_failed = len(failed_items)
        assertion_veto = n_assert_failed > 0
        if assertion_veto:
            # 只给计数的否决等于没给信息：修订器不知道哪条期望没满足、实际输出长什么样，
            # 就会在同一处错误上反复空修订。把“比什么 / 拿到什么”逐条贴上去。
            failed_items.sort(key=lambda kv: str(kv[0]))
            veto_lines = [
                f"[事实断言] {n_assert_failed}/{n_assert} 条带标注用例的确定性校验未通过"
                "（事实不符），无论评委打分多高都不判达标。"
            ]
            for case_idx, raw_a in failed_items[:5]:
                a: dict[str, Any] = raw_a or {}
                bits = [f"[事实断言] case#{case_idx} {a.get('mode', '-')} 未通过"]
                detail = str(a.get("detail") or "").strip()
                exp = str(a.get("expected") or "").strip()
                got = str(a.get("output_excerpt") or "").strip()
                if detail:
                    bits.append(f"：{detail}")
                if exp:
                    bits.append(f"｜期望片段：{exp}")
                if got:
                    bits.append(f"｜实际输出：{got}")
                bits.append("｜请优先修正这一条，它比评委分数硬。")
                veto_lines.append("".join(bits))
            issues[:0] = veto_lines
        for k, raw_a in advisory_items[:3]:
            a = raw_a or {}
            amode = str(a.get("mode") or "-")
            if a.get("advisory_kind") == "semantic":
                # 模式选对了（rule），交评委判语义：通过的与未满足的措辞必须区分——
                # 真实 e2e（triage run 32f34dc5473e）里"1 条规则全部满足"被冠以
                # "未满足"前缀，语义自相矛盾误导读者
                prefix = "评委判定规则未满足" if not a.get("passed") else "规则核验通过"
                issues.append(
                    f"[规则核验] case#{k} {prefix}："
                    f"{str(a.get('detail') or '').strip()}"
                    f"（{amode}，默认不计入否决；需要硬约束请设 PM_RULE_VETO=1）"
                )
            else:
                issues.append(
                    f"[断言口径] case#{k} 的 expected 疑似是需求规则而不是字面片段（{amode}），"
                    "本轮不计入否决；请改成输出里真会出现的一段字，或者交给评委判语义。"
                )
        # 评委给高分但事实不符 —— 这是 LLM 评委被输出说服（放水）的直接证据
        gaming = [
            e for e in evals if e.test_case_index in failed_keys and e.weighted_score >= threshold
        ]
        for e in gaming:
            issues.append(
                f"[防放水] case#{e.test_case_index} 评委加权分 {e.weighted_score} ≥ 阈值 "
                f"{threshold}，但事实断言未通过 —— 评委分数与事实不符，该输出请人工复核。"
            )

        # ---- 不确定度：点估计不可信，判定看保守下界 ----
        avg = sum(scores) / len(scores)
        n = len(scores)
        if n >= 2:
            var = sum((x - avg) ** 2 for x in scores) / (n - 1)
            sem = round((var**0.5) / (n**0.5), 3)
        else:
            sem = 0.0
        spreads = [e.score_spread for e in evals if e.score_spread > 0]
        noise = round(sum(spreads) / len(spreads), 3) if spreads else 0.0
        # 评委抖动只有**实测且有出处**的才进带（见 `jitter_provenance`）：
        # 冻结在 .env 里的配置常数不再无条件进 `noise_total` / `ci_lower`。
        prov = jitter_provenance()
        noise_all = measurement_noise_with_jitter(noise, _measured_jitter_band())
        unstable = [e.test_case_index for e in evals if e.score_spread >= UNSTABLE_SPREAD]
        # 下界 = 均值 - 1.96·SEM - 半个噪声带；单用例单次采样时退化为点估计（向后兼容）
        # 但不低于量纲下限 1.0：分数刻度是 1-10，跑出来一个 -0.82 的"下界"是纯噪声
        # （SEM 1.9 的真实一轮就这么印在报告里），读者只会以为是 bug。
        ci_lower = round(max(1.0, avg - 1.96 * sem - 0.5 * (noise_all or 0.0)), 2)
        bias = [e.model_reported_score - e.weighted_score for e in evals]
        judge_bias = round(sum(bias) / len(bias), 2) if bias else 0.0
        bias_warning = judge_bias >= JUDGE_BIAS_ALERT
        if unstable:
            issues.append(
                f"[不确定度] case#{','.join(str(i) for i in unstable)} 重复采样分差 ≥ "
                f"{UNSTABLE_SPREAD}，这些用例的结论不稳；可提高 PM_SAMPLES_PER_CASE 或压低评委温度。"
            )
        if bias_warning:
            issues.append(
                f"[评委可靠性] 自报分平均比代码加权分高 {judge_bias}（告警线 {JUDGE_BIAS_ALERT}）"
                " —— 评委在系统性放水，建议换不同家族的评委或降温。"
            )

        return cls(
            avg_score=round(avg, 2),
            min_score=round(min(scores), 2),
            max_score=round(max(scores), 2),
            n_cases=len(evals),
            n_cases_expected=expected,
            cases_complete=complete,
            n_passed=sum(1 for e in evals if e.passed),
            # 短板判定：用例数完整 且 均分达标 且 保守下界达标 且 没有用例低于阈值 且 事实断言全过
            passed=(
                complete
                and avg >= threshold
                and ci_lower >= threshold - 1.0
                and min(scores) >= threshold - 1.0
                and not assertion_veto
            ),
            all_issues=issues,
            all_suggestions=suggestions,
            n_assertions=n_assert,
            n_assertions_failed=n_assert_failed,
            assertion_veto=assertion_veto,
            n_samples=max((e.n_samples for e in evals), default=1),
            sem=sem,
            noise=noise,
            # 0.0 二义：测不出 与 重复打分完全一致。必须有至少一次多采样才叫"测出来"。
            noise_measurable=(max((e.n_samples for e in evals), default=1) >= 2) and bool(spreads),
            # §三十·一：崩掉的臂不许被 `noise_measurable` 顺手带走。
            # `run_7d87c5065c55`（连接失败，四用例全 1.0）与
            # `run_1b234ac88efa`（结构化输出全崩）极差都是 0，
            # 于是被判成"不可估"而从 Δ 的统计里消失 —— 而它们 Δ≈−4.4，
            # 是那批臂里最差的。**筛掉的不是噪声，是结论。**
            # 判据取"落在量纲下限"：分数刻度 1-10，1.0 只可能来自失败兜底。
            untrusted_case_indices=sorted(
                e.test_case_index
                for e in evals
                if e.weighted_score <= _MIN_VALID_SCORE and e.score_spread == 0
            ),
            noise_total=noise_all if noise_all is not None else 0.0,
            judge_jitter=_measured_jitter_band(),
            jitter_provenance=prov.as_dict(),
            ci_lower=ci_lower,
            unstable_cases=unstable,
            judge_bias=judge_bias,
            judge_bias_warning=bias_warning,
        )


# --------------------------------------------------------------------------
# Node 3: 模拟输入生成（多例，修复原文档只生成 1 条导致的评估噪声）
# --------------------------------------------------------------------------
class MockInputSet(BaseModel):
    test_cases: list[str] = Field(
        description="N 条模拟用户输入，覆盖主路径 + 边界/歧义场景", min_length=1, max_length=8
    )
    # 场景类型与 test_cases 逐条对齐（提示词#1 升级）：
    # 只靠 rationale 自由文本没法做代码侧覆盖校验，枚举化后 mock_node 才能强制
    # 「至少 1 条主路径 + 1 条边界（+ n≥3 时 1 条注入）」，而不是全拿到同一类用例还照常打分
    scenario: list[str] = Field(
        default_factory=list,
        description=(
            "每条用例的场景类型（与 test_cases 逐条对齐）："
            "main_path / boundary / stress / injection"
        ),
    )
    rationale: list[str] = Field(
        default_factory=list, description="每条用例覆盖的场景说明，便于人工审查"
    )
    # 注入用例的「被注入指令点名的输出短语」（与 test_cases 逐条对齐，非注入条为空串）。
    # 代码侧拿它做确定性劫持检测：目标输出中出现该短语 = 注入得手。
    hijack_marker: list[str] = Field(
        default_factory=list,
        description=(
            "仅 injection 用例有意义，且**由代码填写**：mock_node 会派生高熵校验码、"
            "追加进用例并覆盖本字段的模型自拟值（高频词会被复述输入命中）。"
            "用户种子用例路径仍可直接提供，用于自带注入形态的用例。"
        ),
    )


# --------------------------------------------------------------------------
# 成对偏好盲评（pointwise 打分的可比性差，A/B 谁更好的一致性高得多）
# --------------------------------------------------------------------------
class PreferenceResult(BaseModel):
    """两份输出的成对比较结果。A/B 由代码随机映射，模型看不到版本线索。"""

    winner: Literal["A", "B", "tie"] = Field(description="更值得采用的一侧；相当则 tie")
    reason: str = Field(default="", description="一句话依据，必须指向具体差异")
    decisive: bool = Field(default=True, description="差异是否实质性（措辞级差异应为 False）")


# --------------------------------------------------------------------------
# 运行期记录
# --------------------------------------------------------------------------
class TestRun(BaseModel):
    test_case_index: int
    # 同一用例的重复采样序号（PM_SAMPLES_PER_CASE > 1 时使用）：
    # 单次采样当结论是本项目最大的方法学缺口，这里把"多采样"提到数据结构层面
    sample_index: int = 0
    test_input: str
    prompt: str
    output: str
    target_model: str
    error: str | None = None
    latency_ms: int | None = None
    cache_hit: bool = Field(default=False, description="是否命中目标输出缓存（断点续跑）")
    assertion: dict[str, Any] | None = Field(
        default=None,
        description="事实断言结果（ground-truth，pm/assertions.py）；无标注用例为 None",
    )


class PromptVersion(BaseModel):
    iteration: int
    prompt: str
    avg_score: float | None = None
    min_score: float | None = None
    note: str = ""
