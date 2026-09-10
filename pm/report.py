"""交付报告渲染：从 `pm/nodes.py` 搬出来的纯函数。

为什么单独成模块：报告是这套系统对外的唯一界面，"如实标注"全靠它。原先它和节点实现
挤在同一个 1500 行文件里 —— 改措辞要在大文件里翻，改节点又容易误伤渲染。
这里只做字符串组装，不读写图状态；写回由 `nodes.report_node` 负责。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .schemas import PASS_THRESHOLD

# 用例区分度自检阈值：基线均分达到该值以上时，判定「用例对优化不敏感」。
# 基线 = 原始需求直喂 target：它都拿到接近满分，说明这批用例太简单，
# 任何合理的提示词都能过——优化版即使 9 分也证明不了相对价值（评不出差异）。
BASELINE_DISCRIMINATION_FLOOR = 8.0

# 报告只“读”状态，不依赖 State 的完整字段定义（也不该依赖）：
# 用 Mapping 接单，方便单测直接传 dict，又避开 TypedDict 的窄接受。
ReportState = Mapping[str, Any]


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
    versions: list[dict] = state.get("prompt_versions") or []
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
    versions: list[dict] = state.get("prompt_versions") or []
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
        lines.append("")

    # ---- 基线对比：没有参照点，“分数很高”本身不构成结论 ----
    base = state.get("baseline_aggregate") or {}
    pw = state.get("pairwise") or {}
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
            f"/ 持平 {votes.get('tie', 0)}（共 {pw.get('n_compared', 0)} 例，A/B 已随机映射）"
        )
        if pw.get("conflict"):
            lines.append(f"- ⚠️ 结论冲突：{pw['conflict']}")
        for d in (pw.get("details") or [])[:6]:
            lines.append(
                f"  - case#{d.get('test_case_index')}：{d.get('reason') or '（未给依据）'}"
            )
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
