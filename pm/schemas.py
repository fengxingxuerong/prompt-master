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

import os
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationInfo, field_validator

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


class EvaluationResult(BaseModel):
    dimension_scores: DimensionScores
    model_reported_score: float = Field(
        ge=1, le=10, description="模型自报总分，仅用于监控评分偏差，不参与判定"
    )
    issues: list[str] = Field(default_factory=list, description="具体问题描述")
    suggestions: list[str] = Field(default_factory=list, description="可操作的改进建议")
    should_revise: bool = Field(description="是否需要修订")
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
        description="最终评定来源：evaluator / evaluator_b / merged / arbiter",
    )
    judge_scores: dict[str, float] = Field(
        default_factory=dict, description="各评委的加权分（评委名 → 加权分），用于追溯"
    )
    judge_disagreement: float | None = Field(
        default=None, description="双评委加权分差；超过阈值时触发仲裁"
    )
    cache_hit: bool = Field(default=False, description="本条评估是否命中本地缓存")

    @field_validator("judge", "judge_scores", "cache_hit", "sample_index", mode="before")
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
            }.get(info.field_name)
        return v

    def finalize(self) -> EvaluationResult:
        """模型输出后由代码调用，重算总分并判定。"""
        self.weighted_score = compute_weighted_score(self.dimension_scores)
        self.passed = self.weighted_score >= PASS_THRESHOLD
        return self


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
            amode = str((raw_a or {}).get("mode") or "-")
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
        unstable = [e.test_case_index for e in evals if e.score_spread >= UNSTABLE_SPREAD]
        # 下界 = 均值 - 1.96·SEM - 半个采样噪声；单用例单次采样时退化为点估计（向后兼容）
        ci_lower = round(avg - 1.96 * sem - 0.5 * noise, 2)
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
    rationale: list[str] = Field(
        default_factory=list, description="每条用例覆盖的场景说明，便于人工审查"
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
    assertion: dict | None = Field(
        default=None,
        description="事实断言结果（ground-truth，pm/assertions.py）；无标注用例为 None",
    )


class PromptVersion(BaseModel):
    iteration: int
    prompt: str
    avg_score: float | None = None
    min_score: float | None = None
    note: str = ""
