"""
提示词质量规则校验（代码侧确定性规则，不依赖 LLM）。

背景：真实 e2e 发现优化器可能"复读自身 system prompt"（元话语泄漏），
把优化任务的元说明当成了交付物；而评估器只评 target 模型输出，
评不出 prompt 本身的问题 —— 8.86 的高分掩盖了劣质交付物。

这里用确定性规则在代码侧拦截，堵住这个盲区。

规则：
1. 元话语泄漏（meta_leak）
   - 强特征：优化器/修订器/评估器 system 里的**长句**（领域提示词几乎不可能原样包含），命中即判
   - 弱特征：`输出契约`、`硬性规则` 这类**领域里也会正当出现**的词 —— 单独命中不判，
     需 ≥2 个同时命中（修复 C3：旧版用「输出契约」这种通用术语单串命中，
     把一段合格的「API 接口评审」提示词误杀成泄漏，还反过来让评估器故意压分）
2. 任务上下文泄漏（context_leak）：命中模板里真实存在的注入标签 / 未渲染占位符
   （标签集与占位符集**从 pm.prompts 自动提取**，不再手写清单 ——
   旧版手写的 `<<task>>` 与真实占位符 `<<task_description>>` 对不上，属于检测不到的死码，见 A8）
3. 长度下限：过短（< MIN_PROMPT_LENGTH 字符）视为空泛 / 截断

用法：
- optimize_node / revise_node 生成后调用，不合格自动带 hint 重试一次
- 重试仍不合格 → 记录警告，并在 evaluate 时注入，让评估器重点核查
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .prompts import WRAPPER_TAGS

# 强特征：逐条必须能在对应模板里找到原文（tests/test_quality.py 有断言防漂移）
STRONG_META_MARKERS: list[str] = [
    "世界顶级的提示词工程师",  # OPTIMIZER_SYSTEM 自我定位
    "负责根据评估反馈修订提示词",  # REVISER_SYSTEM 自我定位
    "你是一位严格的 AI 输出质量评估专家",  # EVALUATOR_SYSTEM
    "为给定提示词生成一批",  # MOCKGEN_SYSTEM
    "<输出前自检",  # OPTIMIZER_SYSTEM 的内部自检段
    "<修订流程（按序执行）",  # REVISER_SYSTEM
    "<评估流程（按序执行）",  # EVALUATOR_SYSTEM
    "请输出优化后的提示词",  # OPTIMIZER_USER 收尾指令
    "请输出修订后的完整提示词",  # REVISER_USER 收尾指令
]

# 弱特征：领域提示词里也可能正当出现，需组合命中（≥ WEAK_COMBO_MIN 个）才判泄漏
WEAK_META_MARKERS: list[str] = [
    "输出契约",
    "硬性规则",
    "输出前自检",
    "被广泛用于生产环境",
    "约束预算",
    "先取证、后打分",
]
WEAK_COMBO_MIN = 2

# 交付物长度下限（字符）：低于此值视为空泛 / 截断 / 未生成
MIN_PROMPT_LENGTH = 100

# 除模板外的其它注入包装器（由 nodes.py / scheduler 运行时拼进上下文，不在 prompts.py 模板里）
EXTRA_LEAK_MARKERS: list[str] = [
    "<推断上下文",
    "<用户补充回答>",
    "<quality_warning>",
    "<count_warning>",
    "<json_schema>",
    "<previous_error>",
]

# 注入标签与未渲染占位符：直接从 prompts.py 推导（同一份清单也用于渲染前的转义，
# 不会再出现“清单里写着 <<task>>、模板里其实是 <<task_description>>”的死码）
LEAK_TAGS: list[str] = list(WRAPPER_TAGS) + EXTRA_LEAK_MARKERS

# 质量警告注入到评估器时的提示模板
EVALUATOR_WARNING_BLOCK = """
<prompt_quality_warnings>
代码侧已检测到被测提示词存在以下问题（与目标模型输出无关，是提示词本身的问题）。
请重点核查：该提示词是否直接面向原始任务、是否自包含、是否包含对优化过程或元任务的自我引用。
此类问题属于「任务完成度 / 约束遵守」维度的重大缺陷，应显著扣分：
{warnings}
</prompt_quality_warnings>
"""

# 重试 hint（拼接到 user 段末尾）
RETRY_HINT_TEMPLATE = """
<quality_warning>
你的上一次输出存在问题：{details}
请重新输出：一个直接面向用户原始任务、自包含、可直接作为目标模型 system prompt 使用的提示词。
不要包含任何对本次优化对话的引用、不要输出优化说明、不要复述任务、不要包含元标签。
</quality_warning>
"""


@dataclass
class QualityIssue:
    code: str  # meta_leak | context_leak | too_short
    detail: str

    def __str__(self) -> str:
        return self.detail


@dataclass
class QualityReport:
    issues: list[QualityIssue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.issues

    def describe(self) -> str:
        return "; ".join(i.detail for i in self.issues)


def check_prompt_quality(prompt: str) -> QualityReport:
    """对生成的提示词做确定性规则检查，返回问题列表（空 = 合格）。"""
    issues: list[QualityIssue] = []
    if not prompt or not prompt.strip():
        issues.append(QualityIssue("too_short", "提示词为空"))
        return QualityReport(issues)

    strong = [m for m in STRONG_META_MARKERS if m in prompt]
    weak = [m for m in WEAK_META_MARKERS if m in prompt]
    if strong:
        issues.append(
            QualityIssue("meta_leak", f"疑似元话语泄漏：命中优化流程特征句「{strong[0]}」")
        )
    elif len(weak) >= WEAK_COMBO_MIN:
        issues.append(
            QualityIssue(
                "meta_leak",
                f"疑似元话语泄漏：同时命中 {len(weak)} 个优化器特征词（{', '.join(weak)}）",
            )
        )

    leaked = [t for t in LEAK_TAGS if t in prompt]
    if leaked:
        issues.append(
            QualityIssue(
                "context_leak",
                f"疑似任务上下文泄漏：命中注入标签/占位符「{', '.join(leaked[:3])}」",
            )
        )

    n = len(prompt.strip())
    if n < MIN_PROMPT_LENGTH:
        issues.append(
            QualityIssue(
                "too_short", f"提示词过短（{n} 字符 < {MIN_PROMPT_LENGTH}），疑似空泛或截断"
            )
        )

    # 去重（同一类问题只保留一条，避免重复扣分表述）
    seen: set[str] = set()
    deduped: list[QualityIssue] = []
    for i in issues:
        if i.code not in seen:
            seen.add(i.code)
            deduped.append(i)
    return QualityReport(deduped)


def retry_hint(report: QualityReport) -> str:
    return RETRY_HINT_TEMPLATE.format(details=report.describe())


def evaluator_warning_block(reports: list[str]) -> str:
    if not reports:
        return ""
    bullets = "\n".join(f"- {r}" for r in reports)
    return EVALUATOR_WARNING_BLOCK.format(warnings=bullets)
