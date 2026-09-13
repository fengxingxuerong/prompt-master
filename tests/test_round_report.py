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


def test_conflict_flagged_when_delta_positive_but_pairwise_says_worse():
    st = _state(pairwise={"verdict": "worse", "votes": {"worse": 4}})
    rep = _mod.analyze(st)
    assert rep["checks"]["4a. Δ 与盲评一致"] is False


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
    assert _mod.analyze(st)["checks"]["4b. 提示词要求已有数据照常输出（只标缺失字段）"] is False

    st2 = _state(
        prompt_versions=[
            {
                "iteration": 1,
                "prompt": "只对缺失的字段标注「数据缺失」，已有数据照常输出。",
                "avg_score": 9.0,
            }
        ]
    )
    assert _mod.analyze(st2)["checks"]["4b. 提示词要求已有数据照常输出（只标缺失字段）"] is True


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
