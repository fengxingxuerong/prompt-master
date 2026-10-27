"""报告的章节渲染：每个函数「读 state → 返回若干行」（2026-10-19 从 `pm/report.py` 拆出）。

判据一律不在这里产生 —— 这里只把已算好的数按"如实标注"的措辞组装成 Markdown。
章节的编排在 `pm/report.py::render_report`。
"""

from __future__ import annotations

import re
from typing import Any

from .report_helpers import (
    BASELINE_DISCRIMINATION_FLOOR,
    ReportState,
    _aggregate,
    _base_word,
    _baseline_aggregate,
    _calibration_disclosure,
    _cases_short_note,
    _delta_avg,
    _evaluations,
    _infra_invalid_cases,
    _is_demo_run,
    _memory_hint,
    _pairwise,
    _report_safe_int,
    _seed_arm,
    _test_runs,
    _verdict_conflict,
    pick_best,
)
from .schemas import (
    PASS_SHORTFALL_TOLERANCE,
    PASS_THRESHOLD,
    effective_disagreement_threshold,
    judge_disagreement_threshold,
    judge_jitter,
)
from .state import (
    CLARIFY_REASON_BUDGET_EXHAUSTED,
    CLARIFY_REASON_NOT_INTERACTIVE,
    MAX_CLARIFY_QUESTION_ROUNDS,
)


def _header_lines(state: ReportState) -> list[str]:
    """报告头：run 身份、状态、模式。模式必须在头部就说清——否则读者把 Δ 当成
    "比不优化好多少"读，而它实际是"比交来的那份原稿好多少"。

    2026-10-23 补两条同样"决定这轮读数怎么读"的头部事实：
    - 假后端演示轮（分数是形状示例不是真实评测——此前报告长得和真跑一模一样）；
    - 优化参考（memory 注入会让同任务在不同机器上产出不同，不披露就是隐藏输入）。
    """
    status = state.get("status", "running")
    iteration = state.get("iteration", 0)
    lines: list[str] = []
    lines.append("# PromptMaster 交付报告")
    lines.append("")
    lines.append(f"- run_id：`{state.get('run_id')}`")
    lines.append(f"- 状态：`{status}`")
    if _is_demo_run(state):
        lines.append(
            "- ⚠️ 运行通道：**假后端（PM_FAKE_BACKEND）**——分数、Δ 与成本是链路"
            "形状的演示输出，**不是真实评测读数**，勿当验收或对比依据引用"
        )
    lines.append(f"- 迭代轮次：{iteration} / {state.get('max_iterations', 3)}")
    if state.get("early_stop_reason"):
        lines.append(f"- 提前终止：{state['early_stop_reason']}")
    lines.append(f"- 目标模型：`{state.get('target_model')}`")
    hint = _memory_hint(state)
    if hint:
        sim = hint.get("similarity")
        lines.append(
            f"- 优化参考：生成时命中历史达标资产 `{hint.get('run_id')}`"
            + (f"（相似度 {sim}）" if isinstance(sim, (int, float)) else "")
            + "，已注入生成上下文——同任务在无历史资产的机器上可能产出不同"
        )
    if _seed_arm(state):
        lines.append(
            f"- 模式：原稿改进（输入原稿 {len(_seed_arm(state))} 字符；"
            "基线 = 用户原稿，Δ 读作「比你自己的版本好多少」）"
        )
    lines.append(f"- LLM 调用次数：{state.get('llm_calls', 0)}")
    lines.append("")
    return lines


def _agg_threshold(agg: dict[str, Any]) -> float:
    """本轮判定用的阈值 —— **以归档为准**，缺失时才回退到当前环境值。

    `passed` 是计算时用当轮 `PM_PASS_THRESHOLD` 定死的，如果渲染时再读一次环境变量，
    两次数不一致就会印出自相矛盾的结论（实测 `run_0792cd9acf75`：当时阈值 8.5、
    avg 8.25 ⇒ passed=False，按后来的默认 8.0 渲染时总览每道闸都过而首行写"未达标"）。
    老归档没有这个字段，回退是不得已——所以 `_threshold_is_archived` 会让调用方
    把"这是回退值"明说出来，而不是假装它是当轮的判定依据。
    """
    val = agg.get("threshold")
    if isinstance(val, (int, float)) and float(val) > 0:
        return float(val)
    return PASS_THRESHOLD


def _threshold_is_archived(agg: dict[str, Any]) -> bool:
    val = agg.get("threshold")
    return isinstance(val, (int, float)) and float(val) > 0


def _fmt_num(value: float) -> str:
    """阈值这类小数的显示形式：整数保留一位小数（`8.0` 而不是 `8`），非整数原样。"""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return str(value)
    return f"{v:.1f}" if v == int(v) else f"{v:g}"


def _verdict_head(agg: dict[str, Any]) -> str:
    """结论那一行的措辞必须与数字自洽（2026-10-23 修）。

    旧版未达标分支无条件写「均分 {avg} < 阈值 {T}」——而未达标也可能卡在
    短板容忍带或事实断言上，均分本身已过线，实测印出过「均分 8.5 < 阈值 8.0」
    这种事实错误，而结论行是读者唯一必读的一行。这里对着 `AggregateScore.passed`
    的每道闸逐一描述，不新算任何数。

    2026-10-31：阈值改读 `agg["threshold"]`（当轮冻结值），不再读环境变量——
    否则"当时的判定"与"现在印的阈值"会各说一套。
    """
    avg = agg.get("avg_score")
    thr = _agg_threshold(agg)
    tol_line = thr - PASS_SHORTFALL_TOLERANCE
    if agg.get("passed"):
        return f"✅ **达标**：均分 {avg} ≥ 阈值 {_fmt_num(thr)}"
    reasons: list[str] = []
    blockers: list[str] = []
    lower = agg.get("ci_lower")
    min_s = agg.get("min_score")
    if isinstance(lower, (int, float)) and float(lower) < tol_line:
        blockers.append(f"保守下界 {lower}")
    if isinstance(min_s, (int, float)) and float(min_s) < tol_line:
        blockers.append(f"最低分 {min_s}")
    if blockers:
        reasons.append(
            f"{' 与 '.join(blockers)} 低于容忍线 {tol_line:g}"
            f"（阈值 − {PASS_SHORTFALL_TOLERANCE:g}）"
        )
    if agg.get("assertion_veto"):
        reasons.append(
            f"事实断言未全过（{agg.get('n_assertions_failed')}/{agg.get('n_assertions')}），一票否决"
        )
    if not agg.get("cases_complete", True):
        reasons.append(
            f"用例数不完整（{agg.get('n_cases')}/{agg.get('n_cases_expected')}），不予判定达标"
        )
    # 顺序敏感：均分条款先入列，再判空——否则「只卡均分一道闸」的轮会
    # 掉进兜底分支，读者看不到是分数没过线。
    if isinstance(avg, (int, float)) and float(avg) < thr:
        reasons.insert(0, f"均分 {avg} < 阈值 {_fmt_num(thr)}")
    if not reasons:
        return "⚠️ **未达标**（判定依据见「评分总览」）"
    if isinstance(avg, (int, float)) and float(avg) >= thr:
        return f"⚠️ **未达标**：均分 {avg} 已达阈值 {_fmt_num(thr)}，但" + "；".join(reasons)
    return "⚠️ **未达标**：" + "；".join(reasons)


def _verdict_lines(state: ReportState) -> list[str]:
    """## 结论（先看这一行）：判定 + 最该先知道的警告，搬到报告最前（2026-10-22）。

    用户视角：判定与小字警告散在中段，读者要翻到第三屏才知道"未达标 / 这个 Δ 不可采信"。
    本函数**不产生任何新判据** —— 只把各处已经算好的事实搬到前面，并指回详细章节，
    与其余章节同一条纪律（判据的产地不在渲染层）。
    """
    agg = _aggregate(state)
    status = str(state.get("status", "running"))
    lines: list[str] = ["## 结论（先看这一行）", ""]

    gate = state.get("injection_gate") or {}
    gate_blocked = isinstance(gate, dict) and bool(gate.get("blocked"))

    if status == "failed":
        lines.append("❌ **本轮失败，没有可交付的提示词。** 原因见文末「运行错误」。")
    elif not agg:
        lines.append("⚠️ **本轮没有产生评估结果**，分数与 Δ 都无从谈起（细节见下文各章）。")
    elif gate_blocked:
        # 首行是读者唯一必读的一行：分数过线但注入被劫持时，_verdict_head 只看
        # aggregate 会印「达标」，与文末「注入门禁已拦截」自相矛盾。
        lines.append(
            f"⚠️ **未达标**：分数已过阈值，但注入门禁拦截"
            f"（{gate.get('hijacked')}/{gate.get('total')} 条被劫持），"
            "达标结论被压制，本次交付按未达标处理（见「注入存活」）。"
        )
    else:
        lines.append(_verdict_head(agg) + "。交付的是历史最高分那一版（见文末「最终提示词」）。")

    caveats = _verdict_caveats(state, agg)
    if caveats:
        lines.append("")
        lines.append("**先知道的几件事：**")
        lines += caveats
    lines.append("")
    return lines


def _verdict_caveats(state: ReportState, agg: dict[str, Any]) -> list[str]:
    """结论后面那几条警告：**只在真的成立时出现**，每条都指回自己那一节。"""
    if not agg:
        return []
    out: list[str] = []
    n_invalid = _infra_invalid_cases(state)
    if n_invalid:
        out.append(
            f"- ⚠️ **Δ 不可采信**：优化臂有 {n_invalid} 条用例没有任何有效样本（见「与基线对比」）。"
        )
    elif not _baseline_aggregate(state):
        out.append(
            f"- ⚠️ **没有{_base_word(state)}读数** ⇒ 无法回答"
            + ("「比你的原稿好多少」" if _seed_arm(state) else "「比不优化好多少」")
            + "（见「与基线对比」）。"
        )
    if not agg.get("cases_complete", True):
        out.append("- ⚠️ **用例数不足，本次不予判定达标**（见「评分总览」）。")
    absent = judge_seat_absent(state)
    if absent:
        n_cases = agg.get("n_cases") or 0
        out.append(
            f"- ⚠️ **评委席降级**：{len(absent)}/{n_cases} 条用例有评委缺席，"
            "那些分数不是双评委共识（见「置信度与采样噪声」）。"
        )
    if int(agg.get("n_samples", 1) or 1) < 2:
        out.append(
            "- ⚠️ **噪声不可估**：每条用例只采样 1 次，「采样噪声 0.0」是**没测到**"
            "而不是「打分完全一致」（见「置信度与采样噪声」）。"
        )
    if state.get("clarification_unresolved"):
        # 与"Δ 不可采信"同级搬进结论区（2026-10-26，PE-3）：它改变的是**读者该怎么读这份交付**——
        # 需求前提本身没被确认过，那么再准的分数也只是"在一种未经确认的理解上更好了"。
        out.append(
            "- ⚠️ **需求未澄清即开跑**：提问轮数已用尽仍未判清晰，本轮按**系统推断**的"
            "受众/领域/格式/语气优化（见「需求侧遗留问题」）。"
        )
    return out


def _scoring_protocol_lines(state: ReportState) -> list[str]:
    """本轮分数出自哪套评分协议、是否两种口径混用（2026-10-23 补）。

    判定式清单太薄时会**逐例回退**印象式——judge 节点的留痕此前只有一条
    stderr warning，报告自己说过"必须留痕…否则聚合分成了两种口径的平均"，
    却不把它印出来：同一轮一部分二值判定、一部分五维印象，Δ 与跨轮对比都
    读不出东西。报告必须自含，判据取每条评估归档的 `scoring_mode` 字段。
    """
    evals = _evaluations(state)
    if not evals:
        return []
    n_checklist = sum(1 for e in evals if str(e.get("scoring_mode")) == "checklist")
    n_impression = len(evals) - n_checklist
    if n_checklist and n_impression:
        return [
            f"- ⚠️ 评分协议混用：{n_checklist} 例判定式／{n_impression} 例印象式"
            "（判定式清单太薄的用例逐例回退了印象式）——本轮聚合分是两种口径的平均，"
            "不能与纯协议轮直接对比"
        ]
    if n_checklist:
        return ["- 评分协议：判定式（评委只答二值判定，维度分与总分由代码计算）"]
    return ["- 评分协议：印象式（评委给五个 1-10 整数，代码按固定权重合成总分）"]


def _score_overview_lines(state: ReportState) -> list[str]:
    """## 评分总览：判定看保守下界而不是点估计（见置信度章节）。"""
    agg = _aggregate(state)
    lines: list[str] = []
    lines.append("## 评分总览")
    lines.append("")
    if agg:
        lines.append("| 指标 | 值 |")
        lines.append("|---|---|")
        lines.append(f"| 平均分 | {agg.get('avg_score')} |")
        lines.append(f"| 最低分 | {agg.get('min_score')} |")
        lines.append(f"| 最高分 | {agg.get('max_score')} |")
        lines.append(f"| 用例数 | {agg.get('n_cases')}{_cases_short_note(agg)} |")
        lines.append(f"| 通过用例 | {agg.get('n_passed')} |")
        gate = state.get("injection_gate") or {}
        if isinstance(gate, dict) and gate.get("blocked"):
            verdict = "未达标（分数过阈值，注入门禁拦截）"
        else:
            thr = _agg_threshold(agg)
            verdict = f"通过（≥{_fmt_num(thr)}）" if agg.get("passed") else "未达标"
            if not _threshold_is_archived(agg):
                # 老归档没有冻住的阈值：照旧印判定，但要说明这个数是"当前配置"，
                # 而不是这一轮的判定依据（判定结果以归档里的 `passed` 为准）。
                verdict += f"（本轮归档未记录阈值，此处 {_fmt_num(thr)} 取自当前配置）"
        lines.append(f"| 判定 | {verdict} |")
        lines.extend(_scoring_protocol_lines(state))
    else:
        lines.append("_未产生评估结果_")
    lines.append("")
    return lines


def _uncertainty_lines(state: ReportState) -> list[str]:
    """## 置信度与采样噪声：点估计之外还要看采样噪声与下界。整章只在有聚合结果时出现。"""
    agg = _aggregate(state)
    if not agg:
        return []
    lines: list[str] = ["## 置信度与采样噪声", ""]
    lines += _noise_band_lines(agg)
    lines += _judge_bias_lines(agg)
    lines += _restate_ratio_lines(state)
    lines += _ceiling_claim_lines(state)
    lines += _empty_deliverable_lines(state)
    lines += _judge_source_lines(state)
    lines += _revision_delta_lines(state)
    lines.append("")
    return lines


def _noise_band_lines(agg: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    lines = _jitter_provenance_lines(agg)
    lines.append(f"- 每条用例重复采样：{agg.get('n_samples', 1)} 次（`PM_SAMPLES_PER_CASE`）")
    lines.append(
        f"- 用例间标准误差 SEM：{agg.get('sem', 0.0)}；采样噪声（平均极差）：{agg.get('noise', 0.0)}"
        + (
            f"；⊕ 评委复现性抖动 {agg.get('judge_jitter')} → 合成噪声带 "
            f"{agg.get('noise_total')}（`ci_lower` 用的是合成值）"
            if agg.get("judge_jitter")
            else ""
        )
    )
    if agg.get("judge_jitter"):
        jit_a = judge_jitter("evaluator")
        jit_b = judge_jitter("evaluator_b")
        seat_note = (
            f"；两位座位各自的实测极差 {jit_a}/{jit_b}"
            if {jit_a, jit_b} != {agg.get("judge_jitter")}
            else ""
        )
        lines.append(
            f"- 仲裁有效触发线：{effective_disagreement_threshold()}"
            f"（配置值 {judge_disagreement_threshold()}，按评委自我分歧抬高{seat_note}——"
            "低于这条线的分差当成仪表抖动而不是用例难度，所以**双评委分歧变少了**，"
            "不是交叉验证变强了）"
        )
    lines.append(
        f"- 均分保守下界：**{agg.get('ci_lower')}**（判定要求它 ≥ "
        f"{PASS_THRESHOLD - PASS_SHORTFALL_TOLERANCE:g}，即容忍带下限，"
        f"而不是只看均分 {agg.get('avg_score')}）"
    )
    unstable = agg.get("unstable_cases") or []
    if unstable:
        lines.append(f"- ⚠️ 结论不稳的用例：{', '.join('#' + str(i) for i in unstable)}")
    # ⚠️ 0.0 是二义读数：既是"重复打分完全一致"（好），也是"压根没重复采样、测不出来"。
    # 默认 PM_SAMPLES_PER_CASE=1 落在后者，而报告曾照印 0.0，读的人会当成"噪声极小"。
    # 2026-10-23 补第二形：采样≥2 但所有极差都是 0（确定性桩后端/温控极死）——
    # `noise_measurable` 同样为 False，旧文案却硬写"只采样 1 次"，与上一行自相矛盾。
    if not agg.get("noise_measurable", False):
        n_samples = int(agg.get("n_samples", 1) or 1)
        if n_samples >= 2:
            lines.append(
                f"- ⚠️ **噪声不可估**：虽然每条用例采样了 {n_samples} 次，但极差全为 0"
                "（重复打分完全一致，常见于确定性桩后端）——这台仪表的噪声带**测不出来**，"
                "`采样噪声 0.0` 读作「没测到」而不是「精确」；换真实端点或更高采样数再看。"
            )
        else:
            lines.append(
                "- ⚠️ **噪声不可估**：本轮每条用例只采样 1 次，上面那个 `采样噪声 0.0` "
                "是「没测到」而不是「重复打分完全一致」。要拿到真实的噪声带，"
                "把 `PM_SAMPLES_PER_CASE` 调到 ≥2 重跑。"
            )
    return lines


def _jitter_provenance_lines(agg: dict[str, Any]) -> list[str]:
    """评委抖动那个数是从哪来的（§十八：未验证的配置常数不得进带，但必须披露）。"""
    prov = agg.get("jitter_provenance") or {}
    if not prov:
        return []
    source = prov.get("source")
    if source in ("measured", "measured_override"):
        where = f"账本实测（n={prov.get('n_samples')}，ts={prov.get('ts') or '—'}）"
        if source == "measured_override":
            where += f"，被显式配置 {prov.get('configured')} 覆盖"
        return [f"- 评委复现性抖动取值出处：{where}"]
    configured = prov.get("configured")
    if source == "unverified_config":
        return [
            f"- ⚠️ 评委复现性抖动：配置里写了 `{configured}`，但校准账本里查不到对应的 "
            "`--repeat` 实测记录 —— 该值**未计入**噪声带与 `ci_lower`。"
            f"请跑 `python run.py calibrate --judge evaluator --repeat 3` 把它量出来。"
        ]
    return ["- 评委复现性抖动：未配置、未实测，按 0 计入（噪声带里不含评委手抖）"]


def _judge_bias_lines(agg: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    bias = agg.get("judge_bias", 0.0)
    lines.append(
        f"- 评委自报分与代码加权分的平均偏差：{bias}"
        + (
            "（⚠️ 系统性偏高，评委在放水，建议换评委家族或降温）"
            if agg.get("judge_bias_warning")
            else ""
        )
    )
    cal_line = _calibration_disclosure(agg)
    if cal_line:
        lines.append(cal_line)
    return lines


def _restate_ratio_lines(state: ReportState) -> list[str]:
    # 这台"放水探测器"其实大部分是恒零的：提示词把权重给了模型、演算示例直接示范
    # 怎么求加权，而 model_reported_score 又排在 dimension_scores 之后生成——
    # 实测 109 次原始评委调用里评委 A 有 80%、仲裁 88% 是**逐字复述**加权结果。
    # 所以先把"相等占比"印出来，否则上面那行 -0.17 会被读成"没放水"。
    evals = _evaluations(state)
    scored = [
        e
        for e in evals
        if isinstance(e.get("model_reported_score"), (int, float))
        and isinstance(e.get("weighted_score"), (int, float))
    ]
    if not scored:
        return []
    same = sum(1 for e in scored if abs(e["model_reported_score"] - e["weighted_score"]) < 1e-9)
    if not same:
        return []
    return [
        f"  - ⚠️ 其中 {same}/{len(scored)} 条自报分与代码加权分**完全相等**："
        "这部分不是独立印象，复述上面那行不构成「没放水」的证据。"
        "（该探测器只在两数真的分开时才有意义）"
    ]


def _ceiling_claim_lines(state: ReportState) -> list[str]:
    # 满分声明：五维全部 ≥9.5 却还留着 issue，是自相矛盾；一条 issue 都不给则是
    # "无可指摘"的强声明却没有可核对的证据。两种都是天花板行为，实测占 9.4%。
    ceil_with_issue: list[Any] = []
    ceil_no_issue: list[Any] = []
    for e in _evaluations(state):
        ds = e.get("dimension_scores") or {}
        dims = [v for v in ds.values() if isinstance(v, (int, float))]
        if len(dims) == 5 and min(dims) >= 9.5:
            (ceil_with_issue if (e.get("issues") or []) else ceil_no_issue).append(
                e.get("test_case_index")
            )
    lines: list[str] = []
    if ceil_with_issue:
        lines.append(
            f"- ⚠️ 五维全 ≥9.5 却仍列出了问题：{'、'.join('#' + str(i) for i in ceil_with_issue)}"
            " —— 有缺陷就不该是满分，这条分数的信息量低于它的数字看起来的程度"
        )
    if ceil_no_issue:
        lines.append(
            "- ⚠️ 宣称「无可指摘」（五维全 ≥9.5 且 issues 为空）："
            f"{'、'.join('#' + str(i) for i in ceil_no_issue)} —— 本轮人工锚点里最高只到 8.7，"
            "满分声明建议人工抽查"
        )
    return lines


def _empty_deliverable_lines(state: ReportState) -> list[str]:
    # 空产出高分：输出只剩占位/拒答标记却被判可上线（2026-09-27 人工锚点实测：
    # 两条空壳日报评委 7.0/9.85、人工 2/2）。与满分声明同一待遇：提醒，不否决。
    from .scoring import has_empty_deliverable_profile

    evals = _evaluations(state)
    _out_by_idx = {
        str(tr.get("test_case_index", "0")): str(tr.get("output") or "") for tr in _test_runs(state)
    }
    empty_high = [
        "#" + str(e.get("test_case_index"))
        for e in evals
        if isinstance(e.get("weighted_score"), (int, float))
        and e["weighted_score"] >= PASS_THRESHOLD
        and has_empty_deliverable_profile(_out_by_idx.get(str(e.get("test_case_index", "0")), ""))
    ]
    if empty_high:
        return [
            f"- ⚠️ 空产出获高分（≥{PASS_THRESHOLD}）：{'、'.join(empty_high)}"
            " —— 输出只有占位/拒答标记，没有实质交付物；"
            "「拒答得体」应体现为 task_completion 低分，不该换来可上线判定"
        ]
    if state.get("empty_cap"):
        return [
            f"- ℹ️ 空产出硬封顶已生效：{state.get('empty_cap')} 条评估命中空产出画像，"
            "加权分已由代码压到 4.0（不可用档）。这不是评委的意见，是确定性约束"
            "——校准口径不含此封顶，量的是评委裸分。"
        ]
    return []


def _judge_source_lines(state: ReportState) -> list[str]:
    lines: list[str] = []
    # 评委配置体检：同源评委 = 同一分布采样两次，交叉验证不提供独立证据
    jh = state.get("judge_health") or {}
    if jh.get("homogeneous"):
        lines.append(f"- ⚠️ **评委同源**：{jh.get('note', '')}")
    elif jh.get("models"):
        lines.append(f"- 评委模型：{'、'.join(sorted(set(jh['models'].values())))}")
    # 分数出自谁：双评委分差超阈值时采信的是**第三方单评委**，两位原评委的分只留在
    # judge_scores 里。真跑实测占 9/26 轮（两个已完成 run，冻结口径；见 llm_e2e_matrix 发现 13），不是边角情况，但报告此前从不说明，
    # 读者会以为表里每个数都是两位评委的共识（n_arbitrated 只进过 trace）。
    evals = _evaluations(state)
    n_arb = sum(1 for e in evals if e.get("judge") == "arbiter")
    n_cons = sum(1 for e in evals if e.get("judge") == "conservative")
    if evals and (n_arb or n_cons):
        parts = []
        if n_arb:
            parts.append(f"{n_arb}/{len(evals)} 例出自**仲裁者一人**")
        if n_cons:
            parts.append(f"{n_cons}/{len(evals)} 例因仲裁调用失败取了两评委较低分（安全侧）")
        lines.append(
            "- ⚠️ 分数出处：" + "；".join(parts) + "——这些数**不是**双评委共识，"
            "逐评委的原始分在 `judge_scores`，跨轮对比时按单评委读。"
        )
    # 评委席降级（2026-10-06 §三十·十一·二 补）：上面那条只覆盖"两位都在、分歧大、
    # 交给第三方"这一种形态。**评委直接缺席**是另一种：缺席的用例 `judge` 字段仍是
    # `"evaluator"`（不是 arbiter / conservative），既有条件一个都判不到。
    # 真实跑 run_13f9a99096e9：evaluator_b 5 次超时，4 条用例里 2 条只剩单评委，
    # 那轮的 avg 与基线照常进归档、进分数表，**正文一句都没提这件事**。
    # 后果：半个评委席的读数被当成双评委共识读——而两臂评委席完整度不同，
    # Δ 就不可比（§三十·十一·二 结论：那一轮数据不可用）。
    # 分母取 judge_health.panel_size（配置值），不用"见过几个"倒推：
    # 全员缺席时倒推会得到 0，判不出降级。
    # ⚠️ 判据必须用**失败证据**（errors 里 `evaluate#<idx>[<seat>]:`），
    # 不能用 `len(judge_scores) < panel`——两种形态在归档里**长得一样**：
    #   · PM_JUDGES=1 配置：`_merge_judge_results` 的 len==1 分支原样返回，不写 judge_scores
    #   · 双评委但某位失败：同样落到 len==1，同样不写
    # 真实归档实测（run_13f9a99096e9 的 case#2/#3 与 run_88dcfd4d9c29 的四条）
    # 都是 `len(judge_scores)==0`。只看 judge_scores 就把配置当故障报（或反之）。
    absent = judge_seat_absent(state)
    if absent and evals:
        idxs = "、".join(f"case#{i}({'、'.join(s)})" for i, s in sorted(absent.items()))
        seats = {r for r in (jh.get("models") or {}) if r != "arbiter"}
        gone = sorted({r for s in absent.values() for r in s})
        # 缺席的评委要能从配置名单里对上号；对不上（配置读失败等）就说"有评委"，
        # 不猜是哪一位——披露文本本身不许编。
        missing = "、".join(gone) if set(gone) <= seats else "有评委"
        lines.append(
            f"- ⚠️ 评委席降级：{len(absent)}/{len(evals)} 例有评委失败缺席"
            f"（{missing}：{idxs}）——这些分数**不是**双评委共识；"
            "两臂的评委席完整度若不同则 Δ 不可比。缺席原因见「运行期错误」。"
        )
    return lines


_FAILED_JUDGE_RE = re.compile(r"evaluate#(?P<idx>\d+)\[(?P<seat>[^\]]+)\]")


def judge_seat_absent(state: ReportState) -> dict[int, list[str]]:
    """哪些用例缺了哪位评委：`{用例序号: [席位…]}`，按序号升序。

    报告与 `run.py --json` **共用这一份推导**（2026-10-15 实测：真端点一轮里
    `evaluator_b` 6 次超时、4/4 例降级，报告写了这句话，而机器出口的 33 个键里
    根本没有它 ⇒ 桌面端④把"单评委共识"当双评委共识显示）。判据仍用失败证据，
    不用 `len(judge_scores) < panel`——理由见上面那段注释。
    """
    panel = int((state.get("judge_health") or {}).get("panel_size") or 0)
    if panel <= 1:
        return {}
    absent: dict[int, list[str]] = {}
    for e in state.get("errors") or []:
        m = _FAILED_JUDGE_RE.match(str(e))
        if m:
            absent.setdefault(int(m.group("idx")), []).append(m.group("seat"))
    # ⚠️ 故意**不**拿终态 `judge_scores` 去减掉"看起来还在"的席位（2026-10-15 试过，被
    # `tests/test_run_aa98d99e66b5_findings.py` 判红后撤回）：`evaluations` 每用例只归档
    # 一行，而 `PM_SAMPLES_PER_CASE=2` ⇒ 另一发的席位完整度**根本不在归档里**。
    # aa98d99e66b5 就是反例：4 例的 judge_scores 两位齐全，errors 却记着 3 例墙钟耗尽
    # ——按"减掉"的写法这轮会被判成零降级，而那正是本节要避免的"半个评委席被当共识读"。
    # 判不了就往保守侧靠：宁可照报，也不要用一份取不全的证据去洗白。
    return {i: sorted(set(seats)) for i, seats in sorted(absent.items())}


def _revision_delta_lines(state: ReportState) -> list[str]:
    # 逐条得失：均分看不出的"修 4 坏 3"必须让读者看见（震荡的直接证据）
    delta = state.get("revision_delta") or {}
    if not (isinstance(delta, dict) and (delta.get("fixed") or delta.get("broken"))):
        return []
    fixed, broken = delta.get("fixed") or [], delta.get("broken") or []
    # 空列表不渲染成空括号（……弄坏 0 条（）），2026-10-23 顺手修
    fixed_s = f"（{'、'.join('#' + str(i) for i in fixed)}）" if fixed else ""
    broken_s = f"（{'、'.join('#' + str(i) for i in broken)}）" if broken else ""
    return [
        f"- 与上一版逐条对照：修好 {len(fixed)} 条{fixed_s}"
        f"／弄坏 {len(broken)} 条{broken_s}"
        f"，净值 {delta.get('net', 0)}"
    ]


def _baseline_lines(state: ReportState) -> list[str]:
    """## 与基线对比：没有参照点，"分数很高"本身不构成结论。"""
    agg = _aggregate(state)
    base = _baseline_aggregate(state)
    pw = _pairwise(state)
    lines: list[str] = []
    if not base:
        if not pw:
            lines.append("## 与基线对比")
            lines.append("")
            lines.append(
                "_未跑基线（PM_BASELINE=0 或基线失败）——因此无法回答"
                + ("“比你的原稿好多少”。_" if _seed_arm(state) else "“比不优化好多少”。_")
            )
            lines.append("")
        return lines

    # 注意不能用 `or 0.0` 兜 None：round(-0.001, 2) 得到 -0.0，它是假值，
    # 会被换成 +0.0 —— 报告里 Δ 的符号就印错了（Δ≤0 那条警告也跟着变）。
    d_avg_val = _delta_avg(state)
    d_avg = 0.0 if d_avg_val is None else d_avg_val
    d_min = round(
        float(agg.get("min_score", 0.0) or 0.0) - float(base.get("min_score", 0.0) or 0.0), 2
    )
    lines.append(
        "## 与基线对比（"
        + ("用户原稿直喂 target" if _seed_arm(state) else "原始需求直喂 target")
        + "，同口径采样与评分）"
    )
    lines.append("")
    lines.append("| 指标 | 基线 | 优化后 | Δ |")
    lines.append("|---|---|---|---|")
    lines.append(f"| 平均分 | {base.get('avg_score')} | {agg.get('avg_score')} | {d_avg:+} |")
    lines.append(f"| 最低分 | {base.get('min_score')} | {agg.get('min_score')} | {d_min:+} |")
    # 基础设施有效性：本臂有整条用例没有任何有效样本时，Δ 测的是端点抖动不是质量
    n_invalid = _infra_invalid_cases(state)
    if n_invalid:
        lines.append("")
        lines.append(
            f"> ⚠️ **本轮 Δ 不可采信**：优化臂有 {n_invalid} 条用例**没有任何有效样本**"
            "（目标模型空输出或调用失败，评分层按刻度下限记账以免用例凭空消失）。"
            "两臂是在不同时间窗打的，端点抖动会直接冒充成质量差——先错峰重跑本臂"
            "（或调大 `PM_TARGET_MAX_TOKENS`，空输出多半是 reasoning 耗尽预算），"
            "再谈优化有没有变好。"
        )
    lines.append(
        f"| 断言未通过 | {base.get('n_assertions_failed', 0)}/{base.get('n_assertions', 0)} "
        f"| {agg.get('n_assertions_failed', 0)}/{agg.get('n_assertions', 0)} | - |"
    )
    lines.append("")
    if d_avg <= 0:
        lines.append(
            "> ⚠️ 本轮优化相对基线**没有正向提升**。流水线跑得通不等于创造了价值，"
            "建议人工比对两份输出或提高 PM_SAMPLES_PER_CASE 后重跑。"
        )
        lines.append("")
    # 用例区分度自检：基线（原始需求直喂）都接近满分 = 用例太简单，
    # 任何合理提示词都能过，优化版的分数证明不了相对价值（评不出差异）。
    base_avg = float(base.get("avg_score") or 0.0)
    if base_avg >= BASELINE_DISCRIMINATION_FLOOR:
        easy_arm = "连原始需求都能拿高分" if not _seed_arm(state) else "连你的原稿都能拿高分"
        lines.append(
            f"> ⚠️ **用例区分度不足**：基线均分已达 {base_avg}（≥ {BASELINE_DISCRIMINATION_FLOOR}），"
            f"说明当前测试用例对优化不敏感——{easy_arm}，"
            "本报告的达标结论不能证明优化版相对更好。"
            "建议换用更难的用例（参考 `case_templates/` 的边界与注入场景）或提高采样口径后重跑。"
        )
        lines.append("")
    return lines


def _pairwise_lines(state: ReportState) -> list[str]:
    pw = _pairwise(state)
    if not pw:
        return []
    bw = _base_word(state)
    votes = pw.get("votes") or {}
    n_cmp = _report_safe_int(pw.get("n_compared"))
    # 分母是新加的键：老归档里没有它，退化成"实际出票例数"（即不告警），
    # 而不是反过来把老报告全判成覆盖率不足。
    n_exp = _report_safe_int(pw.get("n_expected")) or n_cmp
    # 「多数」是指占**应比较例数**的多数，不是占实际出票例数的多数 ——
    # 只跑通 1/4 例时那 1 票无论判什么都配不上"多数"两个字。
    top = max(_report_safe_int(votes.get("better")), _report_safe_int(votes.get("worse")))
    thin = n_exp > 0 and top * 2 <= n_exp
    verdict_text = {
        "better": "优化版倾向胜出（出票不足，不代表整卷）" if thin else "优化版多数胜出",
        "worse": (
            f"**{bw}倾向胜出（优化未带来优势；出票不足，不代表整卷）**"
            if thin
            else f"**{bw}多数胜出（优化未带来优势）**"
        ),
        "tie": "两份持平",
        "no_signal": "无有效信号（成对比较全部失败）",
    }.get(str(pw.get("verdict")), str(pw.get("verdict")))
    lines: list[str] = []
    lines.append(f"## 成对盲评（优化版 vs {bw}）")
    lines.append("")
    lines.append(f"- 结论：{verdict_text}")
    lines.append(
        f"- 投票：优化版胜 {votes.get('better', 0)} / {bw}胜 {votes.get('worse', 0)} "
        f"/ 持平 {votes.get('tie', 0)}（共 {n_cmp} 例"
        + (f"／应比较 {n_exp} 例" if n_exp != n_cmp else "")
        + "，每例双向评：正序 + 交换 A/B）"
        + (
            f"\n- ⚠️ 位置偏置：{pw['position_flips']}/{n_cmp} 例两序结论相反"
            "，已一律记为持平（这类用例的胜负由座位决定，不计入结论）"
            if pw.get("position_flips")
            else ""
        )
    )
    if n_cmp < n_exp:
        lines.append(
            f"- ⚠️ 覆盖不足：{n_exp} 例里只有 {n_cmp} 例出票"
            f"（{_report_safe_int(pw.get('n_skipped'))} 例两侧无输出或成对调用失败）"
            "——这个结论**不能**按比例外推到整张考卷。"
        )
    if pw.get("conflict"):
        lines.append(f"- ⚠️ 结论冲突：{pw['conflict']}")
    for d in (pw.get("details") or [])[:6]:
        lines.append(f"  - case#{d.get('test_case_index')}：{d.get('reason') or '（未给依据）'}")
    lines.append("")
    return lines


def _conflict_lines(state: ReportState) -> list[str]:
    """## 结论冲突：两个信号打架时必须说出来，不能让读者自己猜。"""
    pw = _pairwise(state)
    conflict = _verdict_conflict(_delta_avg(state), pw, _base_word(state))
    if not conflict:
        return []
    bw = _base_word(state)
    lines: list[str] = []
    lines.append("## ⚠️ 结论冲突（需人工裁定）")
    lines.append("")
    lines.append(f"- 冲突：{conflict}")
    lines.append("")
    lines.append(
        "- 为什么两个信号会打架：均分是**绝对尺度**（评委按锚点打分，受评委松紧影响），"
        "成对盲评是**相对偏好**（同一输入二选一，不受绝对尺度漂移影响）。"
        "两者不一致通常意味着评委尺度偏松/偏紧，或两份输出各有长短（格式好 vs 事实准）。"
    )
    lines.append("- 建议动作（按成本从低到高）：")
    lines.append("  1. 先看**事实断言**表：断言失败的一方无论分数高低都不应采用；")
    lines.append("  2. 提高 `PM_SAMPLES_PER_CASE` 后重跑（单采样的噪声足以让两个信号分道扬镳）；")
    lines.append("  3. 人工比对上面对盲评列出的逐例依据，再决定是否采用本版提示词。")
    # 冲突自动归因（确定性风格统计，compare_node 计算并存入 pairwise.attribution）
    attr = pw.get("attribution") or {}
    if attr.get("hypothesis"):
        b, c = attr.get("base") or {}, attr.get("cur") or {}
        lines.append("")
        lines.append("- 风格归因（自动统计，供裁定参考）：")
        lines.append(
            f"  - 保守枚举标记（数据缺失/未提供等）：{bw} {b.get('conservative_markers', 0)} 处"
            f" vs 优化版 {c.get('conservative_markers', 0)} 处"
        )
        lines.append(
            f"  - 具体数值引用：{bw} {b.get('data_points', 0)} 处"
            f" vs 优化版 {c.get('data_points', 0)} 处"
        )
        lines.append(f"  - 归因假设：{attr['hypothesis']}")
    lines.append("")
    lines.append("> 本系统不自动裁定冲突：错误的自动裁定比「如实说不知道」更危险。")
    lines.append("")
    return lines


def _version_table_lines(state: ReportState) -> list[str]:
    versions: list[dict[str, Any]] = state.get("prompt_versions") or []
    if len(versions) <= 1:
        return []
    lines: list[str] = []
    lines.append("## 版本迭代")
    lines.append("")
    lines.append("| 版本 | 平均分 | 最低分 | 说明 |")
    lines.append("|---|---|---|---|")
    for v in versions:
        lines.append(
            f"| v{v['iteration']} | {v.get('avg_score', '-')} | "
            f"{v.get('min_score', '-')} | {v.get('note', '')} |"
        )
    lines.append("")
    return lines


def _unresolved_lines(state: ReportState) -> list[str]:
    unresolved = state.get("unresolved_questions", []) or []
    # 带着未澄清的需求开跑（2026-10-26，PE-3）：光列问题不够 ——
    # 读者会把这些当成"以后再答也行"，而真相是**交付物已经按系统推断的
    # 受众/领域/格式/语气做出来了**，用户从未确认过那一版理解。
    # 所以抬头必须说清这是哪一种"遗留"。（判据不在渲染层产生：读 state 的两个既有字段。）
    if not unresolved and not state.get("clarification_unresolved"):
        return []
    lines: list[str] = []
    lines.append("## 需求侧遗留问题")
    lines.append("")
    if state.get("clarification_unresolved"):
        asked = state.get("clarify_questions_asked", 0)
        reason = state.get("clarification_unresolved_reason", "")
        # 两种成因对读者的含义不同，必须分开说（2026-10-26，PE-3）：
        # "没问你"（非交互，CLI 默认）与"问了但没问明白"（打满轮数）不是同一件事，
        # 混成一句话会让读者以为系统问过他却没答。
        if reason == CLARIFY_REASON_NOT_INTERACTIVE:
            why = "本轮为**非交互模式**（未向你提问，属设计如此）"
        elif reason == CLARIFY_REASON_BUDGET_EXHAUSTED:
            why = f"已向你提问 {asked} 轮（上限 {MAX_CLARIFY_QUESTION_ROUNDS} 轮）仍未判清晰"
        else:
            why = "本轮未取得澄清"
        lines.append(
            f"> ⚠️ **需求未澄清即开跑**：{why}，本轮**直接带着系统推断的"
            "受众/领域/格式/语气进入优化**。下面这些问题**没有拿到你的回答**；"
            "若它们的答案会改变结果，请先回答再重跑一轮。"
        )
        lines.append("")
    for q in unresolved:
        lines.append(f"- {q}")
    lines.append("")
    return lines


def _open_issues_lines(state: ReportState) -> list[str]:
    agg = _aggregate(state)
    if not (agg and agg.get("all_issues")):
        return []
    lines: list[str] = []
    lines.append("## 未解决的输出问题")
    lines.append("")
    for i in agg["all_issues"][:20]:
        lines.append(f"- {i}")
    lines.append("")
    return lines


def _run_errors_lines(state: ReportState) -> list[str]:
    if not state.get("errors"):
        return []
    lines: list[str] = []
    lines.append("## 运行期错误")
    lines.append("")
    for e in state["errors"][:20]:
        lines.append(f"- `{e}`")
    lines.append("")
    return lines


def _wasted_delivery_lines(state: ReportState, usage: dict[str, Any]) -> list[str]:
    """「成功但空」的钱花在哪：把 trace 的 *_empty 事件与台账对起来。

    为什么要有这一段（§三十·十九，真实证据 run_41a833fb9b41）：
    台账只记**成功**与**失败**两类，而「HTTP 200 但正文是空的」两类都不是。
    实测那轮 optimizer 花掉 32850 输出 token / 207 秒、台账字段全部正常，
    可最终提示词是 0 字符——只看用量表完全看不出这轮白烧了。
    """
    empties = [
        t
        for t in (state.get("trace") or [])
        if isinstance(t, dict) and str(t.get("event") or "").endswith("_empty")
    ]
    if not empties:
        return []
    lines = [
        "> ⚠️ 本轮出现过**「调用成功但交付为空」**：这些调用在台账上属于正常成功"
        "（HTTP 200、token 照记），所以上面的用量表看不出异常。",
    ]
    for t in empties:
        node = str(t.get("node") or "")
        role = {"optimize": "optimizer", "evaluate": "evaluator"}.get(node)
        spent = ""
        if role and (usage.get(role) or {}):
            u = usage[role] or {}
            out_tok = int(u.get("output_tokens") or 0)
            lat = int(u.get("latency_ms") or 0) / 1000
            spent = f"，该角色累计已花 {out_tok} 输出 token / {lat:.0f}s"
        lines.append(f"> - `{t.get('event')}`（node=`{node}`{spent}）")
    lines.append("")
    return lines


def _usage_lines(state: ReportState) -> list[str]:
    """## 模型用量与耗时（按角色台账）：9 次调用花了多少 token、哪个角色最贵，一眼可见。"""
    usage = state.get("llm_usage") or {}
    if not usage:
        return []
    lines: list[str] = []
    lines.append("## 模型用量与耗时（按角色）")
    lines.append("")
    lines.append("| 角色 | 调用次数 | 输入 tokens | 输出 tokens | 累计耗时 |")
    lines.append("|---|---|---|---|---|")
    tot_calls = tot_in = tot_out = 0
    tot_ms = 0
    for role in sorted(usage):
        t = usage.get(role) or {}
        calls = int(t.get("calls", 0) or 0)
        inp = int(t.get("input_tokens", 0) or 0)
        out = int(t.get("output_tokens", 0) or 0)
        ms = int(t.get("latency_ms", 0) or 0)
        tot_calls += calls
        tot_in += inp
        tot_out += out
        tot_ms += ms
        lines.append(f"| {role} | {calls} | {inp} | {out} | {ms / 1000:.1f}s |")
    lines.append(f"| **合计** | {tot_calls} | {tot_in} | {tot_out} | {tot_ms / 1000:.1f}s |")
    lines.append("")
    # §三十·十八：失败/重试单列。**不并进上面的 calls** —— 那四个数按成功调用算，
    # 混进失败会让「秒/千输出token」这类速率判据失真（judge 守卫依赖那个口径）。
    #
    # 为什么必须显示：以前失败的调用在台账上完全不存在，于是「真的慢」与「一直失败」
    # 在这张表上长得一样——本会话已因此查错三轮（查错本账/误判偶发长尾/误判端点排队）。
    # 触发条件必须同时看 `retry_requests`（2026-10-31 修）：只有"解析失败后重发"时
    # 上一发的传输层是成功的，`failed_calls` 为 0 而 `retry_requests` 可能 >0 ——
    # 只判 failed_calls 会让这段在"重试过但没失败过"的轮次里整段消失。
    if any(
        int((usage.get(r) or {}).get("failed_calls", 0) or 0)
        or int((usage.get(r) or {}).get("retry_requests", 0) or 0)
        for r in usage
    ):
        lines.append("> ⚠️ 上表的「调用次数」只数**成功**的调用。失败/重试另列于下表——")
        lines.append("> 两者不可相加：相加会得到一个既不是真实请求数、也不是成功数的混合值。")
        lines.append("")
        lines.append("| 角色 | 失败次数 | 失败耗时 | 重试请求数 |")
        lines.append("|---|---|---|---|")
        tf = tms = tr = 0
        for role in sorted(usage):
            row = usage.get(role) or {}
            f_n = int(row.get("failed_calls", 0) or 0)
            f_ms = int(row.get("failed_latency_ms", 0) or 0)
            r_n = int(row.get("retry_requests", 0) or 0)
            if not (f_n or r_n):
                continue
            tf += f_n
            tms += f_ms
            tr += r_n
            lines.append(f"| {role} | {f_n} | {f_ms / 1000:.1f}s | {r_n} |")
        lines.append(f"| **合计** | {tf} | {tms / 1000:.1f}s | {tr} |")
        lines.append("")
    # §三十·十九：「成功但空」——传输层成功（HTTP 200、token 照记）但**交付为空**。
    # run_41a833fb9b41 实测：optimizer 烧了 32850 输出 token / 207 秒，台账一切正常，
    # 而最终提示词 0 字符。这类故障**记账记不到**（它既不是失败也不是成功退出），
    # 证据在 trace 的 `*_empty` 事件里。只看用量表会以为这轮一切正常。
    _wasted = _wasted_delivery_lines(state, usage)
    if _wasted:
        lines.extend(_wasted)
    # 金额折算：有 PM_PRICE_*_PER_M 才出现。本系统不内置价目表（会过期且错得看不出来），
    # 没配单价时整段不渲染——"没有数"比"看起来很合理的假数"安全。
    from .cost import render_cost_section

    lines.extend(render_cost_section(usage))
    return lines


def _assertion_lines(state: ReportState) -> list[str]:
    """事实断言（ground-truth）结果：确定性校验的逐条对错，评委分之外的硬证据。

    按**用例**去重（2026-10-31 修）：`test_runs` 是逐**采样**的，`PM_SAMPLES_PER_CASE=2`
    时同一用例会出现两行。实测 `run_13f9a99096e9` 的 case#0/#1 各两行共 4 行，
    而 `aggregate.n_assertions` 是 2 —— 读者会以为查了 4 条。同一用例的任一样本失败
    即视为该用例失败（与 `_collect_assertions` 的保守口径一致），所以保留失败那条。
    """
    by_case: dict[Any, dict[str, Any]] = {}
    for r in _test_runs(state):
        a = r.get("assertion")
        if not isinstance(a, dict):
            continue
        key = r.get("test_case_index", "?")
        prev = by_case.get(key)
        # 失败的优先留下：同一用例两次采样一次过、一次没过时，报"过"等于掩盖证据。
        if prev is None or (prev.get("passed") and not a.get("passed")):
            by_case[key] = a
    if not by_case:
        return []
    lines: list[str] = []
    lines.append("## 事实断言（ground-truth 校验）")
    lines.append("")
    lines.append("| 用例 | 模式 | 结果 | 说明 | 期望片段 |")
    lines.append("|---|---|---|---|---|")
    for idx in sorted(by_case, key=lambda k: str(k)):
        a = by_case[idx]
        exp_cell = str(a.get("expected") or "")
        if len(exp_cell) > 120:
            exp_cell = exp_cell[:120] + "…"
        if a.get("advisory"):
            mark = "⚠️ 语义判定" if a.get("advisory_kind") == "semantic" else "⚠️ 仅提醒"
        else:
            mark = "✅ 通过" if a.get("passed") else "❌ 未通过"
        lines.append(
            f"| case#{idx} | {a.get('mode', '-')} | {mark} | {a.get('detail', '')} | {exp_cell} |"
        )
    if any(str(a.get("mode")) == "rule" for a in by_case.values()):
        lines.append("")
        lines.append(
            "> `rule` 行由**评委**逐条核验（带主观噪声），默认不计入否决；"
            "要把它当硬约束请设 `PM_RULE_VETO=1`。"
        )
    lines.append("")
    return lines


def _injection_lines(state: ReportState) -> list[str]:
    """注入存活（鲁棒性专项）：确定性劫持检测，评委分之外的另一条硬证据。"""
    surv = state.get("injection_survival")
    gate = state.get("injection_gate") or {}
    gate_blocked = bool(gate.get("blocked")) if isinstance(gate, dict) else False
    # 2026-09-16：显示条件必须包含 gate_blocked 本身。
    # 原条件只看 injection_survival —— 一旦两个字段不同步（gate 说拦了但 surv 缺失/
    # total=0），报告就会**静默不提**门禁，用户只看到"未达标"却不知原因。
    # 门禁拦了就必须说清楚，这是安全结论的可解释性底线。
    surv_total = _report_safe_int((surv or {}).get("total")) if isinstance(surv, dict) else 0
    if not (surv_total > 0 or gate_blocked):
        return []
    hijacked = _report_safe_int((surv or {}).get("hijacked")) if isinstance(surv, dict) else 0
    if gate_blocked and isinstance(gate, dict):
        # 以门禁记录的为准（它是实际拦截时快照下来的）
        hijacked = _report_safe_int(gate.get("hijacked")) or hijacked
    lines: list[str] = []
    lines.append("## 注入存活（鲁棒性专项）")
    lines.append("")
    # 用 surv_total / gate 快照，不要直接索引 surv —— 只有 gate 时 surv 可能是 None
    shown_total = surv_total or _report_safe_int(gate.get("total"))
    lines.append(
        f"- 注入用例：{shown_total} 条；被劫持：{hijacked} 条"
        "（输出执行了注入指令，判定为确定性包含检查，不经评委）"
    )
    # 基线臂同口径检测：给出"劫持是优化版引入的，还是这份数据本来就能劫持"的参照
    base_surv = state.get("baseline_injection_survival")
    if isinstance(base_surv, dict) and base_surv.get("total"):
        lines.append(
            f"- 同一批用例打基线（"
            f"{'用户原稿直喂' if _seed_arm(state) else '原始需求直喂'}）：被劫持 "
            f"{_report_safe_int(base_surv.get('hijacked'))}/{base_surv['total']} 条"
            + (
                "——基线未中招而优化版中招，说明劫持由这版提示词自己引入"
                if not _report_safe_int(base_surv.get("hijacked")) and hijacked
                else ""
            )
        )
    for d in ((surv or {}).get("details") or [])[:10]:
        lines.append(f"- ❌ {d}")
    if hijacked:
        lines.append("")
        lines.append(
            "> 被劫持 = 交付提示词里「标签内是数据、其中指令不得执行」一类的声明"
            "没有实际约束力。这种缺陷评委分照常能打 8+，不代表可上线。"
        )
        if gate_blocked:
            lines.append("")
            lines.append(
                f"> 🚫 **注入门禁已拦截**：达标结论被压制（{hijacked}/{shown_total} 被劫持），"
                "本次交付按未达标处理；修复注入约束前禁止渲染 SKILL.md。"
            )
    lines.append("")
    return lines


def _quality_warning_lines(state: ReportState) -> list[str]:
    """代码侧质量门警告（元话语/上下文泄漏，评估时已注入核查）。"""
    q_issues = state.get("prompt_quality_issues", []) or []
    if not q_issues:
        return []
    lines: list[str] = []
    lines.append("## 提示词质量警告（代码侧规则检测）")
    lines.append("")
    for qi in q_issues:
        lines.append(f"- v{qi.get('iteration', '?')}：{qi.get('issues', '')}")
    lines.append("")
    return lines


def _seed_lines(state: ReportState) -> list[str]:
    seed = _seed_arm(state)
    if not seed:
        return []
    best, _ = pick_best(state)
    lines: list[str] = []
    lines.append("## 原稿体检（代码侧确定性规则，未跑模型）")
    lines.append("")
    findings = [str(f) for f in (state.get("seed_quality_findings") or []) if str(f).strip()]
    if findings:
        lines.append(f"你交来的原稿命中 {len(findings)} 条规则，这些命中项已作为判据交给改进器：")
        lines.append("")
        lines.extend(f"- {f}" for f in findings)
    else:
        lines.append(
            "- 原稿未命中任何确定性规则。注意这只说明「实测到会出事的那几类」没踩到，"
            "不等于原稿已经够用——它的分数由下面的基线对比给出。"
        )
    lines.append("")
    lines.append(f"- 原稿 {len(seed)} 字符 → 交付版 {len(best.get('prompt') or '')} 字符")
    lines.append("")
    return lines


def _final_prompt_lines(state: ReportState) -> list[str]:
    best, best_note = pick_best(state)
    return [
        f"## 最终提示词（{best_note}）",
        "",
        "```text",
        best.get("prompt", ""),
        "```",
        "",
    ]
