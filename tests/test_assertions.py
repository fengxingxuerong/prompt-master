"""事实断言（ground-truth 指标）回归测试。

覆盖三层：
1. 断言原语（pm/assertions.py）：exact / contains / regex / custom / 容错路径；
2. 节点集成：seed_cases 直通 mock_node（省一次 LLM）、test_node 执行断言；
3. 聚合语义：断言失败一票否决达标 + 评委放水检测；端到端不判 passed。
"""

from __future__ import annotations

import pytest
from pm import testing
from pm.assertions import check_assertion, register_assertion
from pm.graph import build_app
from pm.nodes import mock_node
from pm.schemas import AggregateScore, DimensionScores, EvaluationResult
from pm.state import initial_state
from pydantic import ValidationError

CFG = {"configurable": {"thread_id": "gt"}, "recursion_limit": 60}


# --------------------------------------------------------------------------
# 1. 断言原语
# --------------------------------------------------------------------------
def test_exact_mode():
    assert check_assertion("结果 A", "结果 A", "exact").passed
    assert check_assertion("结果 A", "  结果 A  ", "exact").passed  # strip 后比较
    assert not check_assertion("结果 A", "结果 B", "exact").passed


def test_contains_mode():
    assert check_assertion("华东", "华东销售额 120 万", "contains").passed
    assert not check_assertion("华南", "华东销售额 120 万", "contains").passed


def test_regex_mode():
    assert check_assertion(r"销售额.{0,4}\d+", "华东销售额为 120 万", "regex").passed
    assert not check_assertion(r"\d{10}", "华东销售额 120 万", "regex").passed
    bad = check_assertion("(未闭合", "x", "regex")
    assert bad is not None and not bad.passed and "正则非法" in bad.detail


def test_custom_assertion_registered_and_missing():
    register_assertion("no_apology", lambda expected, out: "抱歉" not in out)
    assert check_assertion("", "没问题", "custom:no_apology").passed
    r = check_assertion("", "非常抱歉", "custom:no_apology")
    assert r is not None and not r.passed
    missing = check_assertion("", "x", "custom:never_registered")
    assert missing is not None and not missing.passed and "未注册" in missing.detail


def test_assertion_throws_in_custom_fn_is_failure_not_crash():
    def boom(expected: str, out: str) -> bool:
        raise RuntimeError("boom")

    register_assertion("boom", boom)
    r = check_assertion("", "x", "custom:boom")
    assert r is not None and not r.passed and "boom" in r.detail


def test_assertion_skipped_without_expected():
    """无标注 = 无断言：返回 None，调用方跳过而不是当失败。"""
    assert check_assertion("", "任意输出", "contains") is None
    assert check_assertion("   ", "任意输出", "exact") is None


def test_assertion_rejects_bad_name():
    with pytest.raises(ValueError):
        register_assertion("1bad-name", lambda e, o: True)


def test_unknown_mode_fails_closed():
    r = check_assertion("x", "x", "fuzzy")
    assert r is not None and not r.passed


# --------------------------------------------------------------------------
# 2. 节点集成：seed_cases 直通 mock_node
# --------------------------------------------------------------------------
def test_mock_node_uses_seed_cases_without_llm_call():
    state = initial_state(
        task="t",
        target_model="fake",
        n_test_cases=2,
        seed_cases=[
            {"input": "输入一", "expected": "期望一"},
            {"input": "输入二", "expected": ""},
        ],
    )
    state["prompt"] = "p"
    out = mock_node(state)  # type: ignore[arg-type]
    assert out["test_cases"] == ["输入一", "输入二"]
    assert "llm_calls" not in out, "使用用户用例不应产生 mockgen 调用计数"


# --------------------------------------------------------------------------
# 3. 聚合语义：一票否决 + 放水检测
# --------------------------------------------------------------------------
def _high_eval(idx: int) -> EvaluationResult:
    return EvaluationResult(
        dimension_scores=DimensionScores(
            task_completion=9,
            format_adherence=9,
            constraint_compliance=9,
            robustness=9,
            quality=9,
        ),
        model_reported_score=9.0,
        issues=[],
        suggestions=[],
        should_revise=False,
        test_case_index=idx,
        judge="evaluator",
    ).finalize()


def test_assertion_failure_vetoes_pass():
    """评委全给 9 分，但断言失败：不得判 passed，且写入放水告警。"""
    evals = [_high_eval(0), _high_eval(1)]
    assertions = {
        0: {"mode": "contains", "passed": False, "detail": "未包含", "score": 0.0},
        1: {"mode": "contains", "passed": True, "detail": "", "score": 1.0},
    }
    agg = AggregateScore.from_evaluations(evals, n_expected=2, assertions=assertions)
    assert agg.assertion_veto is True
    assert agg.n_assertions == 2
    assert agg.n_assertions_failed == 1
    assert agg.passed is False, "事实不符时评委分再高也不能判达标"
    assert any("事实断言" in i for i in agg.all_issues)
    assert any("[防放水]" in i and "case#0" in i for i in agg.all_issues), (
        "评委高分 + 断言失败 = 放水证据，必须标记"
    )


def test_all_assertions_passed_keeps_normal_verdict():
    evals = [_high_eval(0), _high_eval(1)]
    assertions = {
        0: {"mode": "exact", "passed": True, "detail": "", "score": 1.0},
        1: {"mode": "exact", "passed": True, "detail": "", "score": 1.0},
    }
    agg = AggregateScore.from_evaluations(evals, n_expected=2, assertions=assertions)
    assert agg.passed is True
    assert agg.assertion_veto is False
    assert not any("防放水" in i for i in agg.all_issues)


# --------------------------------------------------------------------------
# 4. 端到端：带 ground-truth 的运行不被判 passed
# --------------------------------------------------------------------------
def test_e2e_with_ground_truth_vetoes_pass():
    seeds = [
        # first case: expected marker is guaranteed absent from any fake output
        {"input": "用例一", "expected": "GROUND_TRUTH_MARKER_绝不含于假输出"},
        {"input": "用例二", "expected": ""},
    ]
    with testing.scope("progress"):
        final = build_app().invoke(
            initial_state(
                task="让 AI 分析销售数据",
                target_model="fake",
                n_test_cases=2,
                max_iterations=1,
                seed_cases=seeds,
                assertion_mode="contains",
            ),
            CFG,
        )
    agg = final["aggregate"]
    assert agg["n_assertions"] == 1, "只有带 expected 的用例执行断言"
    assert agg["assertion_veto"] is True
    assert final["status"] != "passed"
    assert "事实断言（ground-truth 校验）" in final["final_report"]


# --------------------------------------------------------------------------
# 5. API 请求模型
# --------------------------------------------------------------------------
def test_optimize_request_validates_assertion_mode():
    from pm.server import OptimizeRequest

    ok = OptimizeRequest(task="写个分析数据的提示词", assertion_mode="custom:my_check")
    assert ok.assertion_mode == "custom:my_check"
    with pytest.raises(ValidationError):
        OptimizeRequest(task="写个分析数据的提示词", assertion_mode="startswith")
    with pytest.raises(ValidationError):
        OptimizeRequest(
            task="写个分析数据的提示词",
            test_cases=[{"input": ""}],  # 空 input 非法
        )
