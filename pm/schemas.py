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
import os
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
        unstable = [e.test_case_index for e in evals if e.score_spread >= UNSTABLE_SPREAD]
        # 下界 = 均值 - 1.96·SEM - 半个采样噪声；单用例单次采样时退化为点估计（向后兼容）
        # 但不低于量纲下限 1.0：分数刻度是 1-10，跑出来一个 -0.82 的"下界"是纯噪声
        # （SEM 1.9 的真实一轮就这么印在报告里），读者只会以为是 bug。
        ci_lower = round(max(1.0, avg - 1.96 * sem - 0.5 * noise), 2)
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
