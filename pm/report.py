"""交付报告渲染：从 `pm/nodes.py` 搬出来的纯函数。

为什么单独成模块：报告是这套系统对外的唯一界面，"如实标注"全靠它。原先它和节点实现
挤在同一个 1500 行文件里 —— 改措辞要在大文件里翻，改节点又容易误伤渲染。
这里只做字符串组装，不读写图状态；写回由 `nodes.report_node` 负责。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .schemas import (
    PASS_THRESHOLD,
    effective_disagreement_threshold,
    judge_disagreement_threshold,
    judge_jitter,
)

# 用例区分度自检阈值：基线均分达到该值以上时，判定「用例对优化不敏感」。
# 基线 = 原始需求直喂 target：它都拿到接近满分，说明这批用例太简单，
# 任何合理的提示词都能过——优化版即使 9 分也证明不了相对价值（评不出差异）。
BASELINE_DISCRIMINATION_FLOOR = 8.0

# 报告只“读”状态，不依赖 State 的完整字段定义（也不该依赖）：
# 用 Mapping 接单，方便单测直接传 dict，又避开 TypedDict 的窄接受。
ReportState = Mapping[str, Any]


def _report_safe_int(value: Any) -> int:
    """尽力取非负整数，取不到就是 0（state 里的字段类型不可信）。

    为什么需要：state 可能来自旧版 checkpoint 或外部构造，`int(None)`／`int("abc")`
    会让**报告渲染**整段崩掉 —— 报告是唯一给用户看的东西，绝不能因为一个脏字段
    就整个出不来（测试实证：total="abc" 时原写法抛 ValueError）。
    """
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _verdict_conflict(d_avg: float | None, pw: dict[str, Any]) -> str | None:
    """均分口径（Δ）与成对盲评结论是否打架。

    真实运行里出现过：均分 8.67 > 基线 7.54（Δ=+1.13），盲评却是优化版 0 胜 / 基线 4 胜。
    两段结论并排展示、不提示冲突，等于让读者自己猜——与"证明变好了"的设计目标直接矛盾。
    这里只**报告**冲突，不改判定：自动裁定一个自己都说不清的结果比如实说明更危险。
    """
    if d_avg is None or not pw:
        return None
    verdict = str(pw.get("verdict") or "")
    if d_avg > 0 and verdict == "worse":
        return (
            f"均分口径说优化版更好（Δ={d_avg:+.2f}），"
            "但成对盲评多数判**基线胜出**——相对偏好与绝对评分给出相反结论。"
        )
    if d_avg <= 0 and verdict == "better":
        return (
            f"均分口径说没有正向提升（Δ={d_avg:+.2f}），"
            "但成对盲评多数判**优化版胜出**——相对偏好与绝对评分给出相反结论。"
        )
    return None


def _infra_invalid_cases(state: Mapping[str, Any]) -> int:
    """本臂有多少条用例**没有任何有效样本**（目标模型空输出 / 调用失败）。

    为什么必须单独报：这类用例在评分层被记 1.0（否则用例会凭空消失），于是
    "优化版比基线差 4.81 分"可能测的只是**两次调用之间端点抖动的相位差**——
    2026-09-18 的 sales_mockgen 一轮就是这样：优化臂 8 次 empty_output、
    基线臂全成功，Δ 完全没有质量含义。不写出来就会被当成结论读。
    """
    by_case: dict[int, bool] = {}
    for r in state.get("test_runs") or []:
        if not isinstance(r, dict):
            continue
        i = int(r.get("test_case_index", 0) or 0)
        ok = not r.get("error") and bool(str(r.get("output") or "").strip())
        by_case[i] = by_case.get(i, False) or ok
    return sum(1 for ok in by_case.values() if not ok)


def _cases_short_note(agg: dict[str, Any]) -> str:
    """用例数不足时在报告里显式标注（C1）：避免“分数很高”被当成“结论可靠”。"""
    if agg.get("cases_complete", True):
        return ""
    return f"（不足：期望 {agg.get('n_cases_expected')} 条，本次不予判定达标）"


def pick_best(state: ReportState) -> tuple[dict[str, Any], str]:
    """挑出交付版本并给出说明。

    原文档只说"返回当前最佳版本 + 未解决的问题"，却没实现"最佳"的选取逻辑。
    这里明确：达标即取当前版；未达标则回溯所有版本取 avg_score 最高者，
    并如实标注未达标与遗留问题 —— 不粉饰结果。
    """
    versions: list[dict[str, Any]] = state.get("prompt_versions") or []
    status = state.get("status", "running")
    scored = [v for v in versions if v.get("avg_score") is not None]
    if status == "passed" and versions:
        return versions[-1], "最终版本（已达标）"
    if scored:
        best = max(scored, key=lambda v: (v["avg_score"] or 0, v.get("min_score") or 0))
        return best, f"第 {best.get('iteration')} 版（未达标，取历史最高分）"
    if versions:
        return versions[-1], "最终版本（未获得评分）"
    return {"iteration": 0, "prompt": ""}, "无可用版本"


def render_report(state: ReportState) -> tuple[str, dict[str, Any]]:
    """渲染 Markdown 交付报告，返回 (报告文本, 选定的交付版本)。"""
    status = state.get("status", "running")
    iteration = state.get("iteration", 0)
    agg = state.get("aggregate") or {}
    versions: list[dict[str, Any]] = state.get("prompt_versions") or []
    best, best_note = pick_best(state)

    lines: list[str] = []
    lines.append("# PromptMaster 交付报告")
    lines.append("")
    lines.append(f"- run_id：`{state.get('run_id')}`")
    lines.append(f"- 状态：`{status}`")
    lines.append(f"- 迭代轮次：{iteration} / {state.get('max_iterations', 3)}")
    if state.get("early_stop_reason"):
        lines.append(f"- 提前终止：{state['early_stop_reason']}")
    lines.append(f"- 目标模型：`{state.get('target_model')}`")
    lines.append(f"- LLM 调用次数：{state.get('llm_calls', 0)}")
    lines.append("")

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
        verdict = f"通过（≥{PASS_THRESHOLD}）" if agg.get("passed") else "未达标"
        lines.append(f"| 判定 | {verdict} |")
    else:
        lines.append("_未产生评估结果_")
    lines.append("")

    # ---- 不确定度：点估计之外还要看采样噪声与下界 ----
    if agg:
        lines.append("## 置信度与采样噪声")
        lines.append("")
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
            f"- 均分保守下界：**{agg.get('ci_lower')}**（判定看这个而不是看点估计 {agg.get('avg_score')}）"
        )
        unstable = agg.get("unstable_cases") or []
        if unstable:
            lines.append(f"- ⚠️ 结论不稳的用例：{', '.join('#' + str(i) for i in unstable)}")
        bias = agg.get("judge_bias", 0.0)
        lines.append(
            f"- 评委自报分与代码加权分的平均偏差：{bias}"
            + (
                "（⚠️ 系统性偏高，评委在放水，建议换评委家族或降温）"
                if agg.get("judge_bias_warning")
                else ""
            )
        )
        # 这台"放水探测器"其实大部分是恒零的：提示词把权重给了模型、演算示例直接示范
        # 怎么求加权，而 model_reported_score 又排在 dimension_scores 之后生成——
        # 实测 109 次原始评委调用里评委 A 有 80%、仲裁 88% 是**逐字复述**加权结果。
        # 所以先把"相等占比"印出来，否则上面那行 -0.17 会被读成"没放水"。
        evals = [e for e in (state.get("evaluations") or []) if isinstance(e, dict)]
        scored = [
            e
            for e in evals
            if isinstance(e.get("model_reported_score"), (int, float))
            and isinstance(e.get("weighted_score"), (int, float))
        ]
        if scored:
            same = sum(
                1 for e in scored if abs(e["model_reported_score"] - e["weighted_score"]) < 1e-9
            )
            if same:
                lines.append(
                    f"  - ⚠️ 其中 {same}/{len(scored)} 条自报分与代码加权分**完全相等**："
                    "这部分不是独立印象，复述上面那行不构成「没放水」的证据。"
                    "（该探测器只在两数真的分开时才有意义）"
                )
        # 满分声明：五维全部 ≥9.5 却还留着 issue，是自相矛盾；一条 issue 都不给则是
        # "无可指摘"的强声明却没有可核对的证据。两种都是天花板行为，实测占 9.4%。
        ceil_with_issue: list[Any] = []
        ceil_no_issue: list[Any] = []
        for e in evals:
            ds = e.get("dimension_scores") or {}
            dims = [v for v in ds.values() if isinstance(v, (int, float))]
            if len(dims) == 5 and min(dims) >= 9.5:
                (ceil_with_issue if (e.get("issues") or []) else ceil_no_issue).append(
                    e.get("test_case_index")
                )
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
        # 评委配置体检：同源评委 = 同一分布采样两次，交叉验证不提供独立证据
        jh = state.get("judge_health") or {}
        if jh.get("homogeneous"):
            lines.append(f"- ⚠️ **评委同源**：{jh.get('note', '')}")
        elif jh.get("models"):
            lines.append(f"- 评委模型：{'、'.join(sorted(set(jh['models'].values())))}")
        # 分数出自谁：双评委分差超阈值时采信的是**第三方单评委**，两位原评委的分只留在
        # judge_scores 里。真跑实测占 9/26 轮（两个已完成 run，冻结口径；见 llm_e2e_matrix 发现 13），不是边角情况，但报告此前从不说明，
        # 读者会以为表里每个数都是两位评委的共识（n_arbitrated 只进过 trace）。
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
        # 逐条得失：均分看不出的"修 4 坏 3"必须让读者看见（震荡的直接证据）
        delta = state.get("revision_delta") or {}
        if isinstance(delta, dict) and (delta.get("fixed") or delta.get("broken")):
            lines.append(
                f"- 与上一版逐条对照：修好 {len(delta['fixed'])} 条"
                f"（{'、'.join('#' + str(i) for i in delta['fixed'])}）"
                f"／弄坏 {len(delta['broken'])} 条"
                f"（{'、'.join('#' + str(i) for i in delta['broken'])}）"
                f"，净值 {delta.get('net', 0)}"
            )
        lines.append("")

    # ---- 基线对比：没有参照点，“分数很高”本身不构成结论 ----
    base = state.get("baseline_aggregate") or {}
    pw = state.get("pairwise") or {}
    d_avg: float | None = None
    if base:
        d_avg = round(
            float(agg.get("avg_score", 0.0) or 0.0) - float(base.get("avg_score", 0.0) or 0.0), 2
        )
        d_min = round(
            float(agg.get("min_score", 0.0) or 0.0) - float(base.get("min_score", 0.0) or 0.0), 2
        )
        lines.append("## 与基线对比（原始需求直喂 target，同口径采样与评分）")
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
            lines.append(
                f"> ⚠️ **用例区分度不足**：基线均分已达 {base_avg}（≥ {BASELINE_DISCRIMINATION_FLOOR}），"
                "说明当前测试用例对优化不敏感——连原始需求都能拿高分，"
                "本报告的达标结论不能证明优化版相对更好。"
                "建议换用更难的用例（参考 `case_templates/` 的边界与注入场景）或提高采样口径后重跑。"
            )
            lines.append("")
    elif not pw:
        lines.append("## 与基线对比")
        lines.append("")
        lines.append("_未跑基线（PM_BASELINE=0 或基线失败）——因此无法回答“比不优化好多少”。_")
        lines.append("")

    if pw:
        votes = pw.get("votes") or {}
        verdict_text = {
            "better": "优化版多数胜出",
            "worse": "**基线多数胜出（优化未带来优势）**",
            "tie": "两份持平",
            "no_signal": "无有效信号（成对比较全部失败）",
        }.get(str(pw.get("verdict")), str(pw.get("verdict")))
        lines.append("## 成对盲评（优化版 vs 基线）")
        lines.append("")
        lines.append(f"- 结论：{verdict_text}")
        lines.append(
            f"- 投票：优化版胜 {votes.get('better', 0)} / 基线胜 {votes.get('worse', 0)} "
            f"/ 持平 {votes.get('tie', 0)}（共 {pw.get('n_compared', 0)} 例，每例双向评：正序 + 交换 A/B）"
            + (
                f"\n- ⚠️ 位置偏置：{pw['position_flips']}/{pw.get('n_compared', 0)} 例两序结论相反"
                "，已一律记为持平（这类用例的胜负由座位决定，不计入结论）"
                if pw.get("position_flips")
                else ""
            )
        )
        if pw.get("conflict"):
            lines.append(f"- ⚠️ 结论冲突：{pw['conflict']}")
        for d in (pw.get("details") or [])[:6]:
            lines.append(
                f"  - case#{d.get('test_case_index')}：{d.get('reason') or '（未给依据）'}"
            )
        lines.append("")

    # ---- 结论冲突仲裁：两个信号打架时必须说出来，不能让读者自己猜 ----
    conflict = _verdict_conflict(d_avg, pw)
    if conflict:
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
        lines.append(
            "  2. 提高 `PM_SAMPLES_PER_CASE` 后重跑（单采样的噪声足以让两个信号分道扬镳）；"
        )
        lines.append("  3. 人工比对上面对盲评列出的逐例依据，再决定是否采用本版提示词。")
        # 冲突自动归因（确定性风格统计，compare_node 计算并存入 pairwise.attribution）
        attr = pw.get("attribution") or {}
        if attr.get("hypothesis"):
            b, c = attr.get("base") or {}, attr.get("cur") or {}
            lines.append("")
            lines.append("- 风格归因（自动统计，供裁定参考）：")
            lines.append(
                f"  - 保守枚举标记（数据缺失/未提供等）：基线 {b.get('conservative_markers', 0)} 处"
                f" vs 优化版 {c.get('conservative_markers', 0)} 处"
            )
            lines.append(
                f"  - 具体数值引用：基线 {b.get('data_points', 0)} 处"
                f" vs 优化版 {c.get('data_points', 0)} 处"
            )
            lines.append(f"  - 归因假设：{attr['hypothesis']}")
        lines.append("")
        lines.append("> 本系统不自动裁定冲突：错误的自动裁定比「如实说不知道」更危险。")
        lines.append("")

    if len(versions) > 1:
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

    unresolved = state.get("unresolved_questions", []) or []
    if unresolved:
        lines.append("## 需求侧遗留问题")
        lines.append("")
        for q in unresolved:
            lines.append(f"- {q}")
        lines.append("")

    if agg and agg.get("all_issues"):
        lines.append("## 未解决的输出问题")
        lines.append("")
        for i in agg["all_issues"][:20]:
            lines.append(f"- {i}")
        lines.append("")

    if state.get("errors"):
        lines.append("## 运行期错误")
        lines.append("")
        for e in state["errors"][:20]:
            lines.append(f"- `{e}`")
        lines.append("")

    # ---- 模型用量与耗时（按角色台账）：9 次调用花了多少 token、哪个角色最贵，一眼可见 ----
    usage = state.get("llm_usage") or {}
    if usage:
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

    # 事实断言（ground-truth）结果：确定性校验的逐条对错，评委分之外的硬证据
    assert_runs = [
        (r, r["assertion"])
        for r in state.get("test_runs", [])
        if isinstance(r.get("assertion"), dict)
    ]
    if assert_runs:
        lines.append("## 事实断言（ground-truth 校验）")
        lines.append("")
        lines.append("| 用例 | 模式 | 结果 | 说明 | 期望片段 |")
        lines.append("|---|---|---|---|---|")
        for r, a in assert_runs:
            exp_cell = str(a.get("expected") or "")
            if len(exp_cell) > 120:
                exp_cell = exp_cell[:120] + "…"
            if a.get("advisory"):
                mark = "⚠️ 语义判定" if a.get("advisory_kind") == "semantic" else "⚠️ 仅提醒"
            else:
                mark = "✅ 通过" if a.get("passed") else "❌ 未通过"
            lines.append(
                f"| case#{r.get('test_case_index', '?')} | {a.get('mode', '-')} "
                f"| {mark} | {a.get('detail', '')} | {exp_cell} |"
            )
        if any(str(a.get("mode")) == "rule" for _, a in assert_runs):
            lines.append("")
            lines.append(
                "> `rule` 行由**评委**逐条核验（带主观噪声），默认不计入否决；"
                "要把它当硬约束请设 `PM_RULE_VETO=1`。"
            )
        lines.append("")

    # 注入存活（鲁棒性专项）：确定性劫持检测，评委分之外的另一条硬证据
    surv = state.get("injection_survival")
    gate = state.get("injection_gate") or {}
    gate_blocked = bool(gate.get("blocked")) if isinstance(gate, dict) else False
    # 2026-09-16：显示条件必须包含 gate_blocked 本身。
    # 原条件只看 injection_survival —— 一旦两个字段不同步（gate 说拦了但 surv 缺失/
    # total=0），报告就会**静默不提**门禁，用户只看到"未达标"却不知原因。
    # 门禁拦了就必须说清楚，这是安全结论的可解释性底线。
    surv_total = _report_safe_int((surv or {}).get("total")) if isinstance(surv, dict) else 0
    if surv_total > 0 or gate_blocked:
        hijacked = _report_safe_int((surv or {}).get("hijacked")) if isinstance(surv, dict) else 0
        if gate_blocked and isinstance(gate, dict):
            # 以门禁记录的为准（它是实际拦截时快照下来的）
            hijacked = _report_safe_int(gate.get("hijacked")) or hijacked
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
                f"- 同一批用例打基线（原始需求直喂）：被劫持 "
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

    # 代码侧质量门警告（元话语/上下文泄漏，评估时已注入核查）
    q_issues = state.get("prompt_quality_issues", []) or []
    if q_issues:
        lines.append("## 提示词质量警告（代码侧规则检测）")
        lines.append("")
        for qi in q_issues:
            lines.append(f"- v{qi.get('iteration', '?')}：{qi.get('issues', '')}")
        lines.append("")

    lines.append(f"## 最终提示词（{best_note}）")
    lines.append("")
    lines.append("```text")
    lines.append(best.get("prompt", ""))
    lines.append("```")
    lines.append("")

    return "\n".join(lines), best
