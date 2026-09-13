"""轮次证据提取器的判据语义回归。

工具也会骗人：如果 4b 判据只认「完全为空/非空」两个词，历史失败轮也会判"通过"，
迭代就变成自欺。这里把每条判据的判定语义钉死，避免工具悄悄放松。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "e2e_round_report",
    Path(__file__).resolve().parents[1] / "examples" / "e2e_round_report.py",
)
assert _SPEC and _SPEC.loader
_mod = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_mod)


def _state(**over) -> dict:
    base = {
        "run_id": "r",
        "status": "passed",
        "iteration": 1,
        "llm_calls": 10,
        "aggregate": {
            "avg_score": 9.0,
            "min_score": 8.5,
            "ci_lower": 8.6,
            "passed": True,
            "n_cases": 2,
            "n_cases_expected": 2,
        },
        "baseline_aggregate": {"avg_score": 7.0, "min_score": 5.0},
        "pairwise": {"verdict": "better", "votes": {"better": 3}},
        "prompt_versions": [{"iteration": 1, "prompt": "P", "avg_score": 9.0}],
        "test_runs": [],
    }
    base.update(over)
    return base


def test_over_generalization_flagged_when_amounts_dropped():
    st = _state(
        test_runs=[
            {
                "test_case_index": 0,
                "test_input": "华东 120 万，华南 98 万。请分析。",
                "output": "趋势结论：数据缺失（仅单期数据）",
            }
        ]
    )
    rep = _mod.analyze(st)
    assert rep["over_generalized"] and rep["checks"]["3. 无边界过度泛化"] is False


def test_over_generalization_not_flagged_when_amounts_echoed():
    st = _state(
        test_runs=[
            {
                "test_case_index": 0,
                "test_input": "华东 120 万，华南 98 万。请分析。",
                "output": "华东 120 万占比 55%；华北数据缺失",
            }
        ]
    )
    rep = _mod.analyze(st)
    assert rep["over_generalized"] == []
    assert rep["checks"]["3. 无边界过度泛化"] is True


def test_conflict_not_ok_when_report_does_not_flag_it():
    """冲突且报告没写出来 → 判据不过（不许瞒着读者）。"""
    st = _state(pairwise={"verdict": "worse", "votes": {"worse": 4}}, final_report="（无冲突段）")
    rep = _mod.analyze(st)
    assert rep["checks"]["4a. Δ 与盲评一致，或冲突已在报告中标注"] is False


def test_conflict_ok_when_explicitly_flagged_in_report():
    """冲突但报告已显式标注（含建议动作）→ 判据通过：判据要的是"不隐瞒"，不是"没冲突"。"""
    st = _state(
        pairwise={"verdict": "worse", "votes": {"worse": 4}},
        final_report="## ⚠️ 结论冲突（需人工裁定）\n- 冲突：均分说更好但盲评判基线胜",
    )
    rep = _mod.analyze(st)
    assert rep["checks"]["4a. Δ 与盲评一致，或冲突已在报告中标注"] is True


def test_no_conflict_when_signals_agree():
    st = _state(pairwise={"verdict": "better", "votes": {"better": 4}})
    rep = _mod.analyze(st)
    assert rep["checks"]["4a. Δ 与盲评一致，或冲突已在报告中标注"] is True


def test_per_field_clause_detection():
    """只有"缺失"字样、没有"已有数据照常输出"的提示词不算达标（历史失败轮的形态）。"""
    st = _state(
        prompt_versions=[
            {
                "iteration": 1,
                "prompt": "输入完全为空时输出[]；输入非空时各结论标注「数据缺失」",
                "avg_score": 9.0,
            }
        ]
    )
    assert _mod.analyze(st)["checks"]["4b. 缺失口径逐字段限定（非整体兜底）"] is False

    st2 = _state(
        prompt_versions=[
            {
                "iteration": 1,
                "prompt": "只对缺失的字段标注「数据缺失」，已有数据照常输出。",
                "avg_score": 9.0,
            }
        ]
    )
    assert _mod.analyze(st2)["checks"]["4b. 缺失口径逐字段限定（非整体兜底）"] is True


def test_hard_failure_excludes_advisory():
    st = _state(
        test_runs=[
            {
                "test_case_index": 0,
                "assertion": {"mode": "rule", "passed": False, "advisory": True},
            },
            {"test_case_index": 1, "assertion": {"mode": "contains", "passed": True}},
        ]
    )
    rep = _mod.analyze(st)
    assert rep["hard_failed"] == []
    assert rep["checks"]["1. 断言全通过"] is True


def test_hard_failure_blocks_when_contains_fails():
    st = _state(
        test_runs=[
            {
                "test_case_index": 0,
                "assertion": {
                    "mode": "contains",
                    "passed": False,
                    "advisory": False,
                    "expected": "华东",
                },
            },
        ]
    )
    rep = _mod.analyze(st)
    assert rep["checks"]["1. 断言全通过"] is False
    assert rep["hard_failed"][0]["expected"] == "华东"


def test_per_field_proxy_accepts_extraction_style_wording():
    """第 5 轮实测：抽取类任务的合法写法是「完全没有 X 时，X 字段填『未提供』」，
    判据不能只认「已有数据照常输出」，否则把合格交付物判成不合格。"""
    st = _state(
        prompt_versions=[
            {
                "iteration": 0,
                "prompt": (
                    "若对话中完全没有订单号，order_id 填「未提供」；"
                    "若完全没有问题类型，issue_type 填「未提供」。"
                ),
                "avg_score": 9.5,
            }
        ]
    )
    assert _mod.analyze(st)["checks"]["4b. 缺失口径逐字段限定（非整体兜底）"] is True


def test_per_field_proxy_still_rejects_blanket_wording():
    st = _state(
        prompt_versions=[
            {
                "iteration": 0,
                "prompt": "输入非空但无可识别数据时，在各结论位置标注「数据缺失」。",
                "avg_score": 5.0,
            }
        ]
    )
    assert _mod.analyze(st)["checks"]["4b. 缺失口径逐字段限定（非整体兜底）"] is False


def test_network_failures_are_flagged_and_block_conclusion():
    """第 6 轮真实事故：endpoint 连接抖动打掉全部盲评，该轮 status=failed 与低分
    属于基础设施问题，判据必须显式拦住，避免把它当成产品缺陷去改产品。"""
    st = _state(
        status="failed",
        errors=[
            "compare#0: [comparator] 最后错误：OpenAIConnectionError: Connection error.",
            "compare#0: [comparator] 最后错误：OpenAIConnectionError: Connection error.",
            "compare#1: [comparator] 最后错误：OpenAIConnectionError: Connection error.",
            "evaluate#2[evaluator]: ValidationError: 3 validation errors for EvaluationResult",
        ],
    )
    rep = _mod.analyze(st)
    assert rep["checks"]["5. 无网关/网络失败（否则本轮不可作为产品结论）"] is False
    # 同一行重复出现只留一条；不同调用点的失败各算一条
    assert len(rep["net_signatures"]) == 2


def test_no_network_failure_when_errors_are_schema_only():
    st = _state(errors=["evaluate#2[evaluator]: ValidationError: dimension_scores Field required"])
    rep = _mod.analyze(st)
    assert rep["checks"]["5. 无网关/网络失败（否则本轮不可作为产品结论）"] is True
