"""报告渲染的共享取值与判定（2026-10-19 从 `pm/report.py` 拆出）。

这一层只做"从 state 里安全取值"和"该不该说"的判定，不拼字符串，也不依赖渲染段 ——
依赖方向是 `report_sections → report_helpers`、`pm/report.py → 两者`，单向，无循环。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .schemas import (
    PASS_THRESHOLD,
)

BASELINE_DISCRIMINATION_FLOOR = 8.0


ReportState = Mapping[str, Any]


def _seed_arm(state: ReportState) -> str:
    """原稿改进模式下的用户原稿；没交原稿时为空串（基线臂=原始需求直喂）。"""
    return str(state.get("seed_prompt") or "").strip()


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


def _verdict_conflict(
    d_avg: float | None, pw: dict[str, Any], base_word: str = "基线"
) -> str | None:
    """均分口径（Δ）与成对盲评结论是否打架。

    真实运行里出现过：均分 8.67 > 基线 7.54（Δ=+1.13），盲评却是优化版 0 胜 / 基线 4 胜。
    两段结论并排展示、不提示冲突，等于让读者自己猜——与"证明变好了"的设计目标直接矛盾。
    这里只**报告**冲突，不改判定：自动裁定一个自己都说不清的结果比如实说明更危险。

    `base_word` 是对手臂的称呼：原稿改进模式下它不是"未优化的基线"而是**用户自己的版本**，
    沿用"基线"会让读者把"基线胜出"读成"不优化更好"。默认值保持非原稿模式逐字不变。
    """
    if d_avg is None or not pw:
        return None
    verdict = str(pw.get("verdict") or "")
    if d_avg > 0 and verdict == "worse":
        return (
            f"均分口径说优化版更好（Δ={d_avg:+.2f}），"
            f"但成对盲评多数判**{base_word}胜出**——相对偏好与绝对评分给出相反结论。"
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
    for r in _test_runs(state):
        i = _report_safe_int(r.get("test_case_index", 0) or 0)
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


def _human_source_note(cal: dict[str, Any]) -> str:
    """锚点侧的同源披露：合并 bias 里有多少"人工分"其实是 AI 代判的。

    评委同源（A/B 同模型）早有检测和警示，锚点侧的同源此前只在提交说明里承认过一句。
    代判模型 GLM-5.3 与评委 B glm-5.2 同族 ⇒ 那部分读数量的是**家族一致性**，
    不写出来，读者会把"+3.09"整句当成"与人的偏差"。
    """
    split = cal.get("by_human_source") or {}
    ai = split.get("ai_proxy") or {}
    owner = split.get("owner") or {}
    ai_n = int(ai.get("n_anchors") or 0)
    owner_n = int(owner.get("n_anchors") or 0)
    if not ai_n or ai_n < owner_n:
        return ""
    bias = owner.get("bias")
    strict = (
        f"；严格人工那一半（{owner_n} 条）bias {float(bias):+.2f}"
        if isinstance(bias, (int, float))
        else ""
    )
    return f"（其中 {ai_n} 条的人工分是 AI 按所有者判例代判、与评委 B 同族{strict}）"


def _calibration_disclosure(agg: ReportState) -> str | None:
    """校准披露行：把账本里量得的评委系统性偏差折进报告的读数语境。

    2026-09-29 落地，依据 docs/evaluation.md §十五：48 锚点人工基线把「评委 A
    系统性偏松 bias +3.64、8.0 判定漏放 35/45」坐实——绝对分不带这把尺子的
    已知误差就会被误读，8.2 不是"接近满分"，折算后约 4.6。与空产出封顶同一
    哲学的另一半：**只披露、不修复**——点估计、判定行、保守下界全部照旧，
    读者拿误差棒自己折算。

    偏差在告警线（±1.0，calibration.BIAS_ALERT）内时降为弱措辞，否则未来换了
    校准合格的评委，报告还在喊狼来了。

    2026-10-04 换型纪元：聚合按**本轮实际解析到的评委模型**过滤（账本轮记录
    自带 model 字段）——换型后旧模型的 +3.09 不再混进新仪表的披露行。模型
    解析失败时退回不过滤（旧行为）：披露行有比没有好，配置错误另有体检管。
    """
    from . import llm as _llm
    from .calibration import BIAS_ALERT, latest_pooled
    from .prompts import checklist_rubric_stamp, rubric_stamp
    from .scoring import scoring_mode

    try:
        current_model = _llm.build_config("evaluator").model
        current_rubric = (
            checklist_rubric_stamp() if scoring_mode() == "checklist" else rubric_stamp()
        )
    except Exception:  # noqa: BLE001 - 与 _judge_health 同款：配置读取失败不影响主流程
        current_model = None
        current_rubric = None

    cal = latest_pooled(model=current_model, rubric=current_rubric)
    if not cal:
        return None
    bias = cal.get("bias")
    if not isinstance(bias, (int, float)):
        return None
    ci = cal.get("bias_ci95")
    ci_note = (
        f"（95% CI {ci[0]:+.2f}~{ci[1]:+.2f}）" if isinstance(ci, list) and len(ci) == 2 else ""
    )
    src_parts = [str(cal.get(k)) for k in ("n_anchors", "n_rounds") if isinstance(cal.get(k), int)]
    src = f"{src_parts[0]} 条人工锚点、{src_parts[1]} 轮校准的" if len(src_parts) == 2 else ""
    src += _human_source_note(cal)
    avg = agg.get("avg_score")
    if abs(bias) < BIAS_ALERT:
        return (
            f"- 评委校准披露：{src}聚合平均偏差 {bias:+.2f}{ci_note}，在告警线"
            f"（±{BIAS_ALERT}）内，绝对分可按面值理解"
        )
    direction = "偏松" if bias > 0 else "偏严"
    kappa = (cal.get("decision") or {}).get("kappa")
    kappa_note = f"，κ={kappa}" if isinstance(kappa, (int, float)) else ""
    fold = (
        f"；本报告均分 {avg} 按此折算约 **{max(0.0, float(avg) - float(bias)):.1f}**"
        if isinstance(avg, (int, float))
        else ""
    )
    return (
        f"- ⚠️ 评委校准披露：{src}聚合平均偏差 **{bias:+.2f}**{ci_note}——评委系统性{direction}"
        f"已实测坐实{fold}。{PASS_THRESHOLD} 达标判定在裸读数下不可采信{kappa_note}，"
        "达标结论建议人工抽查复核（量具刻画见 docs/evaluation.md §十五）"
    )


def _base_word(state: ReportState) -> str:
    """对手臂的称呼：原稿模式下"基线"其实是用户自己写的版本，
    沿用"基线"会让"基线胜出"被读成"不优化更好"。
    """
    return "你的原稿" if _seed_arm(state) else "基线"


def _aggregate(state: ReportState) -> dict[str, Any]:
    agg: dict[str, Any] = state.get("aggregate") or {}
    return agg


def _baseline_aggregate(state: ReportState) -> dict[str, Any]:
    base: dict[str, Any] = state.get("baseline_aggregate") or {}
    return base


def _pairwise(state: ReportState) -> dict[str, Any]:
    pw: dict[str, Any] = state.get("pairwise") or {}
    return pw


def _evaluations(state: ReportState) -> list[dict[str, Any]]:
    return [e for e in (state.get("evaluations") or []) if isinstance(e, dict)]


def _test_runs(state: ReportState) -> list[dict[str, Any]]:
    """逐条执行记录，只认 dict 条目、`test_runs` 整体缺失或为 None 时给空表。

    为什么要在读取点统一收口：state 可能来自旧版 checkpoint 或外部构造，
    实测 `test_runs=["x"]` 会让报告渲染抛 AttributeError、`test_runs=None` 抛
    TypeError——**报告是唯一给用户看的东西**，不能因为一条脏记录整个出不来
    （同文件 `_report_safe_int` 讲的是同一件事）。原先三处读取各写各的：
    `_infra_invalid_cases` 防了非 dict，另外两处没防。
    """
    return [r for r in (state.get("test_runs") or []) if isinstance(r, dict)]


def _delta_avg(state: ReportState) -> float | None:
    """均分口径的 Δ：没跑基线时是 None（冲突检测就靠这个 None 保持沉默）。"""
    base = _baseline_aggregate(state)
    if not base:
        return None
    agg = _aggregate(state)
    return round(
        float(agg.get("avg_score", 0.0) or 0.0) - float(base.get("avg_score", 0.0) or 0.0), 2
    )
