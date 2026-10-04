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
4. 约束超载（constraint_overload）：[约束]/[关键约束]/[边界处理] 段内编号条目 > CONSTRAINT_LIMIT
   （2026-09-12 评审新增：OPTIMIZER_SYSTEM 一直要求"总数 ≤10 条"，但只靠模型自检清单执行，
   真实交付物实测 18 / 15 / 10 / 6 条，前两轮超标且 [关键约束] 与 [约束] 大面积语义重复。
   「元话语泄漏」上了硬闸，「约束超载」却靠自觉是双标——同一待遇：代码侧确定性校验 + 带 hint 重试）

用法：
- optimize_node / revise_node 生成后调用，不合格自动带 hint 重试一次
- 重试仍不合格 → 记录警告，并在 evaluate 时注入，让评估器重点核查
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .prompts import WRAPPER_TAGS

# 强特征：逐条必须能在对应模板里找到原文（tests/test_quality.py 有断言防漂移）
STRONG_META_MARKERS: list[str] = [
    "世界顶级的提示词工程师",  # OPTIMIZER_SYSTEM 自我定位
    "负责根据评估反馈修订提示词",  # REVISER_SYSTEM 自我定位
    "你是一位严格的 AI 输出质量评估专家",  # EVALUATOR_SYSTEM
    "生成一批（多条）模拟用户输入",  # MOCKGEN_SYSTEM
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

# 约束条目上限：与 OPTIMIZER_SYSTEM 硬性规则 #4 同一口径。
# 2026-09-13 实测从 10 收到 8：约束超载的轮次（[约束]×12）被评委点名"模型漏执行"，
# constraint_compliance 8.38→7.0、task_completion 9.25→7.75 —— 条目越多越执行不到位。
CONSTRAINT_LIMIT = 8

# 每轮修订的净增量上限：只写在 REVISER_USER 的 <size_budget> 散文里等于没有闸
# （模型自觉，超标无人核对 → 交付物逐轮膨胀）。这里给出唯一口径，
# 由 revise_node 做确定性核对并写进版本注记与报告。
SIZE_GROWTH_RATIO = 1.3

# 抑制型规则：让输出"少说/不说"的条款（停止处理、不输出结论、固定 token 兜底）。
# 为什么单列一类：这类规则不违反字面约束，却直接压低 task_completion 与 robustness
# （实测 8.75→7.88），且与"边界情况必须继续作答"的骨架要求相冲突。
_SUPPRESSIVE_RULE_RE = re.compile(
    r"(停止处理|终止处理|不再输出|不输出任何结论|不输出结论|仅输出固定 ?token|"
    r"固定输出[「\"']?数据缺失|拒绝(?:回答|输出))"
)

# 约束条目只在「约束语义」的标签段里数：[任务]/[输出格式] 里的编号是交付物清单
# （如"1. 趋势结论 2. 异常点"），把它们算进来会把合格提示词误杀。
_SECTION_HEADER_RE = re.compile(r"^\s*\[([^\[\]]{1,8})\]\s*$")
# 段名同义词要覆盖全：只认「约束|边界」时，交付物把段名写成 [要求] / [规则]
# 就能带着 20 条编号约束"合规"通过预算闸——绕过成本为零。
_CONSTRAINT_SECTION_RE = re.compile(r"约束|边界|规则|要求|限制|禁止|规范|须知|注意")
# 但这些段里的编号是交付物骨架而不是约束，即便段名撞上上面的词也不算（[输出要求]、
# [格式规范] 里列的是"第一部分/第二部分"，数进来就是误杀）。
_NON_CONSTRAINT_SECTION_RE = re.compile(r"任务|背景|角色|格式|输出|示例|骨架|流程|步骤|结构")
_NUMBERED_ITEM_RE = re.compile(r"^\s*\d+\s*[.、)）]\s*\S")
# 定界符配平：<输入>…<输入>（重复开标签）这类畸形会在真实调用里放大注入面 ——
# 目标模型看到两个开标签，分不清哪段才是"数据区"，上一轮评审在 Run A 的最终提示词里就抓到过。
_OPEN_TAG_RE = re.compile(r"<([A-Za-z_\u4e00-\u9fff][\w\u4e00-\u9fff]{0,20})>")
_CLOSE_TAG_RE = re.compile(r"</([A-Za-z_\u4e00-\u9fff][\w\u4e00-\u9fff]{0,20})>")

# 「兜底式缺失」措辞：把缺失当成全局开关（各结论位置一律标注数据缺失），
# 目标模型会据此把"部分字段缺失"泛化成"整体无数据"，连输入里明摆着的金额一起判缺失。
# 真实事故（2026-09-12/13 两轮 e2e）：「输入非空但未包含任何可识别的销售数据……在各结论位置
# 标注「数据缺失」」→ 含 120 万/98 万的用例被整段判缺失，事实断言直接失败。
# 精度教训（2026-09-13 第 2 轮实测）：**不要用裸的「一律/统一」当触发词**。
# 合法提示词里「相对日期一律视为无明确日期，标注『数据缺失』」是限定作用域的逐字段规则，
# 却会被 `一律.{0,12}缺失` 命中 → 闸误杀 → 强制重写 → 该轮均分从 5.78 掉到 4.76、提前终止。
# 现在只认「作用域是整体输出」的措辞（各结论位置/全部结论/整体标注/所有字段……），
# 且要求"缺失"字样紧跟其后：宁可漏判，也不误杀。
# 命中条件 = 「作用域词（指整体输出）」+ 12 字内出现**肯定式**的标注动词。
# 加否定守卫的原因（第 2 轮实测）：v1 里「若输入数据完整（所有字段均有值），**不得**标注
# 「数据缺失」」被前版正则命中——这是反向规则（数据完整时禁止标缺失），语义与兜底完全相反。
_BLANKET_SCOPE_RE = re.compile(
    r"(各结论位置|全部结论|所有结论|整体标注|一律标注|统一标注|所有字段|全部字段)"
)
_BLANKET_MARKING_RE = re.compile(
    r"(不得|禁止|不应|不可|不要|勿|避免)?\s*(?:标注|标记|标为|输出|填写|写为|写作)"
    r"\s*[「“\"']?(数据缺失|缺失)"
)
# 限定作用域的正面表述（出现任意一个即视为已区分两种缺失）
_PER_FIELD_RE = re.compile(r"(已有数据|其余|剩余|照常输出|照常列出|仍须输出|仍要输出)")


def blanket_missing_branch(prompt: str) -> bool:
    """交付物是否把「数据缺失」写成全局兜底（而不是按字段限定作用域）。

    只拦**明确的全局措辞**（如"各结论位置一律标注数据缺失"），不做语义猜测：
    单说"缺失时标注「数据缺失」"的合格提示词不会被误判。
    出现全局措辞时，无论是否同时写了逐字段规则都算问题——两者并存本身就是自相矛盾。
    """
    text = prompt or ""
    for m in _BLANKET_SCOPE_RE.finditer(text):
        mk = _BLANKET_MARKING_RE.search(text[m.end() : m.end() + 16])
        if mk and not mk.group(1):  # 有肯定式标注动词，且不是"不得标注"这类否定句
            return True
    return False


def delimiter_problems(prompt: str) -> list[str]:
    """检查交付物里的标签是否配平。

    只报两类**确定性结构错误**，不报"结尾未闭合"：
    提示词正文里提到 `<输入> 标签内的数据` 这种散文引用非常常见（优化器模板自己就这么写），
    把它判成"未闭合"会误杀合格交付物，误杀比重蹈更贵（会触发无谓重写）。
    真正会出事的是**同一个标签开了两次**（Run A 实测事故）——
    目标模型看到两个开标签，分不清哪段才是数据区，注入面被放大。
    """
    problems: list[str] = []
    stack: list[str] = []
    for line in (prompt or "").splitlines():
        for name in _OPEN_TAG_RE.findall(line):
            if name in stack:
                problems.append(f"<{name}> 重复开标签（上一个还没闭合）")
            else:
                stack.append(name)
        for name in _CLOSE_TAG_RE.findall(line):
            if name in stack:
                stack.pop()
            else:
                problems.append(f"</{name}> 没有对应的开标签")
    # 同一类问题只报一次，避免刷屏
    deduped: list[str] = []
    for p in problems:
        if p not in deduped:
            deduped.append(p)
    return deduped


def _is_constraint_section(name: str) -> bool:
    """段名是否属于「约束语义」段（受 ≤CONSTRAINT_LIMIT 预算管）。"""
    return bool(_CONSTRAINT_SECTION_RE.search(name)) and not _NON_CONSTRAINT_SECTION_RE.search(name)


def count_constraints(prompt: str) -> dict[str, Any]:
    """统计约束类标签段（[约束] / [规则] / [限制] / [边界处理] 等）内的编号条目数。

    返回 {"total": 总数, "sections": "段名1×a、段名2×b"}；无任何约束段时 total=0。
    无编号但确有约束段的（用破折号列条目）不做猜测——只数能确定性数出来的。
    """
    total = 0
    parts: list[tuple[str, int]] = []
    current: str | None = None
    current_count = 0
    for line in (prompt or "").splitlines():
        m = _SECTION_HEADER_RE.match(line)
        if m:
            if current is not None and current_count:
                parts.append((current, current_count))
            current = m.group(1)
            current_count = 0
            continue
        if (
            current is not None
            and _is_constraint_section(current)
            and _NUMBERED_ITEM_RE.match(line)
        ):
            current_count += 1
    if current is not None and current_count:
        parts.append((current, current_count))
    total = sum(n for _, n in parts)
    sections = "、".join(f"[{name}]×{n}" for name, n in parts)
    return {"total": total, "sections": sections}


# 破折号/圆点条目：`count_constraints` 刻意不数它们（数错会误杀 ≤8 约束预算闸），
# 但判定式评分要的是"逐条可核对的清单"，多一条少一条只是多问一个是非题，
# 代价远低于漏掉 [约束] 段里用 `- ` 写的那半数条目。
_BULLET_ITEM_RE = re.compile(r"^\s*[-•*]\s*\S")
# 标签与条目同行的写法：`[约束] 每条结论必须引用数值；缺失必须标注`，
# 以及标题同行式 `## 输出格式：Markdown 表格；结论不超过 3 条`。
# 组 1 = 方括号段名，组 2 = 标题段名（两者互斥），组 3 = 同行正文。
_INLINE_SECTION_RE = re.compile(
    r"^\s*(?:\[([^\[\]]{1,8})\]|#{1,3}\s*([^\[\]#]{1,12})[：:])\s*(\S.*)$"
)
# Markdown 标题式段落（`# 角色` / `## 约束`）：真实交付物里比方括号标签更常见。
# 只认 1-3 级标题，4 级以下通常是正文层次而不是段落名。
_HEADING_SECTION_RE = re.compile(r"^\s*#{1,3}\s*([^\[\]#]{1,12})\s*$")
# 同行条目的分隔符：只用分号与句号 —— 顿号/逗号会把"缺失 A、B 字段"这类
# 一个作用域并列多项的条目切成半截，切错比不切更坏（判据要对，不要多）。
_ITEM_SPLIT_RE = re.compile(r"[；;。]")


def structure_items(prompt: str) -> list[tuple[str, str]]:
    """把标签段里的条目摊平成 `(段名, 条目原文)` 列表。

    四种写法都收（各对应实测到的一类提示词）：
    1. `[约束]` 独占一行，下面跟 `1.` / `- ` 条目；
    2. `[约束] 条目一；条目二` —— 标签与条目同行、用分号/句号分隔。
       早期只认写法 1 与 3，结果评委校准里 7/11 条锚点"摊不出清单"被门禁挡掉，
       A/B 两边的样本数都不一样，测了等于没测；
    3. `# 约束` / `## 处理流程` 这类 Markdown 标题段落（真实交付物里比方括号更常见）；
    4. 上述任一段落下以 `1.` / `- ` 开头的条目行。

    **不做语义判断**：只按标签段与行首标点切，所以同一段里"1. 趋势结论 2. 异常点"
    （交付物）和"[约束] 1. 不得编造"（约束）会一起出来——分类是 `pm.scoring` 的事。
    """
    out: list[tuple[str, str]] = []
    current: str | None = None
    for line in (prompt or "").splitlines():
        bare = _SECTION_HEADER_RE.match(line) or _HEADING_SECTION_RE.match(line)
        if bare:
            current = bare.group(1)
            continue
        head = _INLINE_SECTION_RE.match(line)
        if head:
            current = head.group(1) or head.group(2) or ""
            for part in _ITEM_SPLIT_RE.split(head.group(3)):
                text = part.strip(" \t-•*、")
                if len(text) >= 4:
                    out.append((current, text))
            continue
        if current is None:
            continue
        if _NUMBERED_ITEM_RE.match(line) or _BULLET_ITEM_RE.match(line):
            text = re.sub(r"^\s*(?:\d+\s*[.、)）]|[-•*])\s*", "", line).strip()
            if text:
                out.append((current, text))
    return out


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
    code: str  # meta_leak | context_leak | too_short | constraint_overload | delimiter_unbalanced
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

    if blanket_missing_branch(prompt):
        issues.append(
            QualityIssue(
                "blanket_missing_branch",
                "「数据缺失」被写成全局兜底（如「各结论位置一律标注数据缺失」）："
                "目标模型会据此把「部分字段缺失」泛化成「整体无数据」，"
                "连输入里已有的数值一起判缺失（真实事故，事实断言因此失败）。"
                "必须拆成两条互斥规则：①输入完全为空/零字符 → 可跳过骨架、整体标注缺失；"
                "②输入非空但部分字段缺失 → 保留骨架、已有数据照常输出，只对缺失字段标注「数据缺失」",
            )
        )

    delim = delimiter_problems(prompt)
    if delim:
        issues.append(
            QualityIssue(
                "delimiter_unbalanced",
                "定界符不配平："
                + "；".join(delim[:3])
                + "——畸形的 <输入> 类标签会让目标模型分不清数据区边界，放大注入面；"
                "请保证每个开标签都有且只有一个对应的闭合标签",
            )
        )

    n = len(prompt.strip())
    if n < MIN_PROMPT_LENGTH:
        issues.append(
            QualityIssue(
                "too_short", f"提示词过短（{n} 字符 < {MIN_PROMPT_LENGTH}），疑似空泛或截断"
            )
        )

    counted = count_constraints(prompt)
    if counted["total"] > CONSTRAINT_LIMIT:
        issues.append(
            QualityIssue(
                "constraint_overload",
                f"约束超载：约束类段共 {counted['total']} 条编号条目"
                f"（{counted['sections']}），超过上限 {CONSTRAINT_LIMIT} 条。"
                "实测后果：条目越多执行越差（评委点名「模型漏执行」，"
                "constraint_compliance 与 task_completion 双双下滑）。"
                f"请删减到 {CONSTRAINT_LIMIT} 条以内：合并语义重复项、删除可有可无项，"
                "边界规则单独成段且不超过 4 条",
            )
        )

    suppressive = _SUPPRESSIVE_RULE_RE.findall(prompt or "")
    if suppressive:
        issues.append(
            QualityIssue(
                "suppressive_rule",
                "含抑制型规则（" + "、".join(dict.fromkeys(suppressive))[:60] + "）："
                "让目标模型「少说/不说」的条款会压低任务完成度与鲁棒性（实测 8.75→7.88），"
                "且与'边界情况仍要按骨架作答'相冲突。"
                "边界处理应写成「标注缺失 + 继续输出其余内容」，而不是停止处理或不输出结论",
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
