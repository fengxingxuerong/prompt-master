"""判定式评分（checklist 协议）：把评委的自由度从"五个 1-10 整数"压成"一串二值判定"。

为什么要换协议（不是把旧提示词写得更凶）：
`docs/evaluation.md` §八/§九 的实测结论是评委的复现性问题是**双峰**而不是噪声 ——
同一份输入，仲裁稳定落在「约束3/鲁棒3/完成度5 → 5.15」与「约束6/鲁棒6/完成度7 → 7.10」
两个模式之间，且自报分与加权分同步移动：每次调用是先选一套口径、再照着口径填数。
降温无效（`temperature=0` 没改善）、中位数无效（在双峰上是在两个口径之间投票）。
五个 1-10 整数给了它 10^5 种口径组合，写多少"锚定规则"都还是靠它自觉。

所以本模块做的事是把判断权从评委手里拿走一半：
1. 核对清单由**代码**从被测提示词的标签段里确定性抽取（`pm.quality.structure_items`），
   再注入几条与提示词无关的通用检查（核心交付物有没有真给、自相矛盾、部分缺失当整体无数据）；
2. 评委只回答"这条满足没有 + 一句证据"，不填分数；
3. 维度分、总分、以及 rubric 里那条"编造关键事实则总分封顶 6.0"由**代码**算 ——
   封顶从"评委记得就封、不记得就不封"变成硬约束；
4. 唯一保留的自由判断是 `quality_band`（内容深度确实无法二值化），从 1-10 收成 5 个具名档位。

诚实边界：这套协议消掉的是"口径漂移"，消不掉"事实判断错"。如果评委把一条确实满足的
约束判成不满足，判定式会给出**稳定地错**的分数 —— 复现性变好不等于更准，
两件事要分别用 `--repeat` 的自我极差和锚点的 bias/MAE 去量。
"""

from __future__ import annotations

import os
import re
from typing import Any

from .quality import structure_items
from .schemas import (
    PASS_THRESHOLD,
    ChecklistEvaluation,
    DimensionScores,
    EvaluationResult,
    compute_weighted_score,
)

MODE_IMPRESSION = "impression"
MODE_CHECKLIST = "checklist"

# 各桶的"违规条数 → 维度分"台阶。台阶位置不是拍脑袋的：它对齐 EVALUATOR_SYSTEM
# 自己的区间语义 —— 0 条未满足落在 9-10（逐条对照后找不到实质问题），
# 1 条落在 8-8.9（生产可用、允许个别不影响决策的轻微问题），
# 2 条就掉到 7 档（rubric 明写 7-7.9「单条不成立」，不是可用档），
# 3 条 5-6（勉强可用），4 条及以上 1-4（需重写）。
# 相比印象式这是**收紧**：判定式下"未满足"= 清单里真有一条没做到，
# 而人工锚点在两处缺失时给的确实是 7.0 以下（`real-sales-derived-stats` 人工 7.0）。
_STEPS: dict[int, float] = {0: 9.5, 1: 8.0, 2: 6.5, 3: 5.0}
_STEP_FLOOR = 3.0

# quality_band → 分。档位定义在 rubric 里逐条写死，避免"3 档还是 4 档"的口径漂移。
_BAND_POINTS: dict[int, float] = {1: 2.0, 2: 4.5, 3: 6.5, 4: 8.5, 5: 9.5}

# 编造/无依据关键事实的总分封顶值：与 rubric 里"加权总分不得高于 6.0"同口径。
_FABRICATION_CAP = 6.0
# 编造项以"清单里的一条"参与计数，这样它和提示词约束走同一套台阶，不额外开一条罚分路径。
_FABRICATION_ITEM = "事实性：输出中存在输入无法支撑、且评委未给出推导依据的数值或结论"
# 提示词自己贡献的条目少于这个数就走印象式：基线臂（原始需求直喂）没有标签段，
# 清单空桶会让 format/constraint 双双拿 9.5，基线被抬高之后 Δ 就测不出优化有没有变好。
_MIN_PROMPT_ITEMS = 2

_FORMAT_SECTION_RE = re.compile(r"格式|输出|骨架|结构|示例|排版")
_DELIVERABLE_SECTION_RE = re.compile(r"任务|交付|产出|步骤|流程")
# 破折号引导的条目在真实交付物里占多数，段名却常常就叫 [要求]/[说明]，落不进上面两类。

_NUM_RE = re.compile(r"\d+(?:\.\d+)?%?")
# 千分位要先抹掉：`\d+(?:[.,]\d+)?` 会把 "1,200,000" 切成 "1,200" + ",000"，
# 于是"输出写 1,200,000、输入也写 1,200,000"反而回查不到同源（漏报），
# 更糟的是同一个金额会被拆成两个陌生 token 报给评委去解释。
_THOUSANDS_RE = re.compile(r"(?<=\d),(?=\d{3}(?!\d))")


def _num_tokens(text: str) -> set[str]:
    """抽数字 token（千分位归一、去掉百分号）。输入侧与输出侧同一套口径才可比。"""
    normalized = _THOUSANDS_RE.sub("", text or "")
    return {m.rstrip("%") for m in _NUM_RE.findall(normalized)}


def _points(items: list[str], violated: set[str]) -> float:
    """桶内违规条数 → 维度分。空桶给 9.5：没有可查的条目就不罚，也不奖。"""
    if not items:
        return 9.5
    n = sum(1 for it in items if it in violated)
    return _STEPS.get(n, _STEP_FLOOR)


def scoring_mode() -> str:
    """当前评分协议。缺省 `impression` = 旧行为逐字不变（本仓库的一贯口径：不填就不动）。"""
    raw = (os.getenv("PM_SCORING_MODE") or "").strip().lower()
    return MODE_CHECKLIST if raw == MODE_CHECKLIST else MODE_IMPRESSION


def unsourced_numbers(test_input: str, output: str) -> list[str]:
    """输出里在输入中找不到原样来源的数字（= 待解释的候选）。

    收录口径：带小数点或百分比的一律收（"22.4%"这类比率最常被编造），
    纯整数只收 **≥100** 的 —— 1~99 的整数绝大多数是序号与条目计数，全报会把评委
    的解释预算耗在噪音上；而实测的编造形态（"Q1 总销售额 275 万"）都是大数。
    这是**机械线索**不是判定：派生值（求和、占比、环比、均值）完全合法，
    所以评委必须为每个候选给出去处，给不出才算编造、漏报同样算。
    """
    src = _num_tokens(test_input)
    out: list[str] = []
    for token in _NUM_RE.findall(_THOUSANDS_RE.sub("", output or "")):
        norm = token.rstrip("%")
        if norm in src or norm in out:
            continue
        decimal_or_pct = "." in norm or token.endswith("%")
        try:
            big = float(norm) >= 100.0
        except ValueError:
            big = False
        if len(norm) > 1 and (decimal_or_pct or big):
            out.append(norm)
    return out


def build_checklist(prompt: str, task: str = "") -> list[dict[str, str]]:
    """从被测提示词摊平核对清单，并注入与提示词无关的通用检查。

    注入项（`_INJECTED`）的意义：清单条目随提示词变，两版提示词的分数因此不可直接比；
    有这几条固定项，基线臂与最终臂至少在**同一批问题**上可比。
    返回 `[{"item": 原文, "bucket": format|constraint|deliverable|robustness}]`。
    """
    items: list[dict[str, str]] = []
    seen: set[str] = set()
    for section, text in structure_items(prompt or ""):
        bucket = _bucket_for(section, text)
        item = f"[{section}] {text}"
        if item in seen:
            continue
        seen.add(item)
        items.append({"item": item, "bucket": bucket})
    for item, bucket in _INJECTED:
        if item not in seen:
            items.append({"item": item, "bucket": bucket})
    return items


def checklist_usable(checklist: list[dict[str, str]]) -> bool:
    """清单够不够格走判定式：只剩注入项撑着 = 根本没在核这条提示词，宁可退回印象式。"""
    return sum(1 for r in checklist if not r["item"].startswith("通用：")) >= _MIN_PROMPT_ITEMS


def _bucket_for(section: str, text: str) -> str:
    if _FORMAT_SECTION_RE.search(section):
        return "format"
    if _DELIVERABLE_SECTION_RE.search(section) and not re.search(r"不得|禁止|必须|应", text):
        return "deliverable"
    return "constraint"


# 通用检查项。三条都对应实测到的人工/评委分歧点，而不是"看起来该问的问题"：
# ① 核心交付物被一句不可核对的话糊过去（A 给 8.85、人工 7.0）；
# ② 「输入非空但字段不足」被泛化成「整体无数据」（2026-09-12/13 真实事故，均分 5.78→4.76）；
# ③ 同一份输出里自相矛盾（争议锚点 disp-1：一边列「未提供订单编号」一边拿它当查询输入）。
_INJECTED: tuple[tuple[str, str], ...] = (
    (
        "通用：原始需求点名要的那一件交付物是否真的给出（而不是推给「其他/信息不足」）",
        "deliverable",
    ),
    (
        "通用：输入非空但字段不足时，是否只对缺失字段标注缺失、已有数据照常输出"
        "（而不是把整段判成无数据）",
        "robustness",
    ),
    ("通用：输出内部是否存在互相矛盾的说法（含与前文已声明缺失的内容冲突）", "robustness"),
)


def score_checklist(
    ev: ChecklistEvaluation,
    checklist: list[dict[str, str]],
    *,
    unsourced_candidates: list[str] | None = None,
) -> tuple[DimensionScores, dict[str, Any]]:
    """二值判定 → 维度分（纯代码）。返回 (维度分, 明细)。

    **未回答按违规处理**：评委跳过一条问题不该是免费的，否则"少答"会稳定抬高分数，
    而少答正是印象式里那个"取证取到一半就 commit 分数"的形态。
    """
    buckets: dict[str, list[str]] = {
        "format": [],
        "constraint": [],
        "deliverable": [],
        "robustness": [],
    }
    verdicts = {_norm(v.item): v for v in ev.verdicts}
    violated: list[str] = []
    unanswered: list[str] = []
    for row in checklist:
        bucket = row["bucket"] if row["bucket"] in buckets else "constraint"
        buckets[bucket].append(row["item"])
        v = verdicts.get(_norm(row["item"]))
        if v is None:
            unanswered.append(row["item"])
            violated.append(row["item"])
            continue
        if not v.satisfied:
            violated.append(row["item"])

    # 事实性：代码点名的无来源数字里，评委没解释（或解释是占位语）的那几个按编造处理。
    explained = {
        _norm(c.claim)
        for c in ev.unsourced
        if not _is_placeholder(c.basis) and (c.basis or "").strip()
    }
    fab = [t for t in (unsourced_candidates or []) if not any(t in e for e in explained)]
    violated_set = set(violated)
    if fab:
        # rubric 的锚定规则：编造类违规 constraint 与 robustness 同档落 3-4，
        # 且总分封顶 6.0。印象式下这条靠评委记得，判定式下由代码落。
        violated_set.add(_FABRICATION_ITEM)
        buckets["constraint"].append(_FABRICATION_ITEM)
        buckets["robustness"].append(_FABRICATION_ITEM)

    dims = DimensionScores(
        task_completion=_points(buckets["deliverable"], violated_set),
        format_adherence=_points(buckets["format"], violated_set),
        constraint_compliance=_points(buckets["constraint"], violated_set),
        robustness=_points(buckets["robustness"], violated_set),
        quality=_BAND_POINTS.get(int(ev.quality_band), 6.5),
    )
    caps: list[float] = [_FABRICATION_CAP] if fab else []
    details: dict[str, Any] = {
        "violations": violated,
        "unanswered": unanswered,
        "unsourced_unexplained": fab,
        "caps": caps,
        "n_items": len(checklist),
        "n_items_declared": int(ev.n_items_checked),
        # 声明条数与清单条数不符只留痕不罚分：罚分会把"数错数"和"没做到"混成同一件事
        "items_mismatch": int(ev.n_items_checked) != len(checklist),
        "quality_band": int(ev.quality_band),
        "bucket_sizes": {k: len(v) for k, v in buckets.items()},
    }
    return dims, details


def apply_caps(dims: DimensionScores, details: dict[str, Any]) -> tuple[float, bool]:
    """加权分 + 代码侧封顶 → (最终分, 是否通过)。

    绕过 `finalize()` 是有意的：`finalize()` 只会按维度分算加权，而"编造关键事实则总分
    不得高于 6.0"这条必须有一个能真正执行它的地方 —— 否则它仍然只是一句写在提示词里的话。
    """
    weighted = compute_weighted_score(dims)
    for cap in details.get("caps") or []:
        weighted = min(weighted, float(cap))
    weighted = round(weighted, 2)
    return weighted, weighted >= PASS_THRESHOLD


def evaluate_with_checklist(
    role: str,
    user_prompt: str,
    checklist: list[dict[str, str]],
    numbers: list[str],
) -> tuple[Any, dict[str, Any]]:
    """走判定式协议打一次评委调用，把二值判定折算成 `EvaluationResult`。

    返回 (结果, meta)，meta 里带结构化输出走的通道 —— 主管道要按它记 trace。

    放在这里（而不是各写一份在主管道和校准脚本里）是为了两套入口跑的是**同一个协议实现**：
    A/B 测的必须是将来会上线的那条路径。
    """
    from . import llm  # 局部导入：避免 llm ↔ scoring 的潜在环
    from .prompts import EVALUATOR_SYSTEM_CHECKLIST

    ev_model, meta = llm.structured_call(
        role, ChecklistEvaluation, EVALUATOR_SYSTEM_CHECKLIST, user_prompt
    )
    dims, detail = score_checklist(ev_model, checklist, unsourced_candidates=numbers)
    weighted, passed = apply_caps(dims, detail)
    from .schemas import EvaluationResult

    ev = EvaluationResult(
        dimension_scores=dims,
        model_reported_score=weighted,
        issues=ev_model.issues,
        suggestions=ev_model.suggestions,
        should_revise=not passed,
        judge=role,
        scoring_mode=MODE_CHECKLIST,
        checklist_detail=detail,
        weighted_score=weighted,
        passed=passed,
    )
    return ev, meta


def merge_checklist_results(
    ev_a: EvaluationResult, ev_b: EvaluationResult, checklist: list[dict[str, str]]
) -> EvaluationResult:
    """判定式下双评委的合并：**违规取并集**，不是维度分取平均。

    为什么必须换：印象式取平均是"两位各打一个数、折中"；判定式的输出本质是一组布尔判定，
    取平均会把"A 漏判一条、B 抓到了"稀释成 8.75 继续放行 —— 而防放水这套机制要的就是
    "任一评委抓到就算抓到"（与 `judge._merge_rule_checks` 同一取向）。
    质量维度取两位的较低值：它是唯一还留给评委的自由判断，保守方向在这里成立。
    """
    da: dict[str, Any] = dict(ev_a.checklist_detail or {})
    db: dict[str, Any] = dict(ev_b.checklist_detail or {})
    violated = set(da.get("violations") or []) | set(db.get("violations") or [])
    unanswered = sorted(set(da.get("unanswered") or []) | set(db.get("unanswered") or []))
    unsourced = sorted(
        set(da.get("unsourced_unexplained") or []) | set(db.get("unsourced_unexplained") or [])
    )
    buckets: dict[str, list[str]] = {
        "format": [],
        "constraint": [],
        "deliverable": [],
        "robustness": [],
    }
    for row in checklist:
        buckets[row["bucket"] if row["bucket"] in buckets else "constraint"].append(row["item"])
    if unsourced:
        # 与 score_checklist 同一处置：编造同时落进 constraint 与 robustness，并封顶 6.0
        violated.add(_FABRICATION_ITEM)
        buckets["constraint"].append(_FABRICATION_ITEM)
        buckets["robustness"].append(_FABRICATION_ITEM)
    dims = DimensionScores(
        task_completion=_points(buckets["deliverable"], violated),
        format_adherence=_points(buckets["format"], violated),
        constraint_compliance=_points(buckets["constraint"], violated),
        robustness=_points(buckets["robustness"], violated),
        quality=min(ev_a.dimension_scores.quality, ev_b.dimension_scores.quality),
    )
    detail: dict[str, Any] = {
        "violations": sorted(violated),
        "unanswered": unanswered,
        "unsourced_unexplained": unsourced,
        "caps": [_FABRICATION_CAP] if unsourced else [],
        "n_items": len(checklist),
        "merged_from": [ev_a.judge, ev_b.judge],
    }
    weighted, passed = apply_caps(dims, detail)
    return EvaluationResult(
        dimension_scores=dims,
        model_reported_score=weighted,
        issues=_dedupe_texts(list(ev_a.issues) + list(ev_b.issues)),
        suggestions=_dedupe_texts(list(ev_a.suggestions) + list(ev_b.suggestions)),
        should_revise=not passed,
        judge="merged",
        scoring_mode=MODE_CHECKLIST,
        checklist_detail=detail,
        weighted_score=weighted,
        passed=passed,
    )


def _dedupe_texts(texts: list[str]) -> list[str]:
    seen: dict[str, None] = {}
    for t in texts:
        key = _norm(t)
        if key and key not in seen:
            seen[key] = None
    return [t for t in texts if _norm(t) in seen]


def _is_placeholder(basis: str) -> bool:
    """「无来源/不清楚/大概是」这类解释等于没解释。"""
    b = basis.strip()
    return b in {"", "无", "不知道", "不确定", "无法判断", "未提供", "n/a", "N/A", "数据缺失"}


def _norm(text: str) -> str:
    """条目文本归一化：评委照抄时会改空格、加句号，回配必须对这些不敏感。"""
    return re.sub(r"\s+", "", (text or "").strip().rstrip("。.，,"))
