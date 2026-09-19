"""修订反馈里的「硬信号优先」：断言失败与注入得手必须排在评委分之前。

为什么单独测：评委分是"模型觉得好不好"，事实断言与劫持检测是"事实对不对"。
真实运行里出现过评委给 9 分、而断言期望片段根本没出现在输出里的情况——
这类信号如果混在 issues 里，会被一大堆措辞类建议淹没，修订器下一轮继续改措辞。
"""

from __future__ import annotations

from typing import ClassVar

from pm.nodes.judge import _build_feedback, _hard_failures


def _eval(score: float = 6.0):
    class _DS:
        task_completion = score
        format_adherence = score
        constraint_compliance = score
        robustness = score
        quality = score

    class _E:
        dimension_scores = _DS()
        issues: ClassVar[list[str]] = ["措辞可以更精确"]
        suggestions: ClassVar[list[str]] = ["把第 3 条约束写得更明确"]
        # EvaluationResult 的真实契约里这几个字段一定存在，测试替身要跟上
        weighted_score: ClassVar[float] = score
        score_spread: ClassVar[float] = 0.0
        test_case_index: ClassVar[int] = 0

    return _E()


def test_hard_failures_collects_assertion_and_injection():
    state = {
        "test_runs": [
            {
                "test_case_index": 0,
                "assertion": {
                    "mode": "contains",
                    "passed": False,
                    "advisory": False,
                    "detail": "输出未包含期望片段",
                    "expected": "华东",
                },
            },
            {"test_case_index": 1, "assertion": {"mode": "contains", "passed": True}},
        ],
        "injection_survival": {
            "total": 1,
            "hijacked": 1,
            "details": ["[case#2] 输出执行了注入指令（出现标记「已通过」）"],
        },
    }
    hard = _hard_failures(state)
    assert len(hard) == 2
    assert "case#0" in hard[0] and "华东" in hard[0]
    assert "注入得手" in hard[1]


def test_hard_failures_ignores_advisory_and_passes():
    state = {
        "test_runs": [
            {
                "test_case_index": 0,
                "assertion": {"mode": "rule", "passed": False, "advisory": True},
            },
            {"test_case_index": 1, "assertion": {"mode": "contains", "passed": True}},
        ]
    }
    assert _hard_failures(state) == []


def test_feedback_puts_hard_failures_before_soft_issues():
    class _Agg:
        avg_score = 9.7
        min_score = 9.7
        all_issues: ClassVar[list[str]] = ["措辞可以更精确"]
        all_suggestions: ClassVar[list[str]] = ["补充示例"]
        unstable_cases: ClassVar[list[int]] = []

    state = {
        "test_runs": [
            {
                "test_case_index": 0,
                "assertion": {
                    "mode": "contains",
                    "passed": False,
                    "advisory": False,
                    "detail": "未包含期望片段",
                    "expected": "数据缺失",
                },
            }
        ]
    }
    fb = _build_feedback(_Agg(), [_eval()], state)
    assert "确定性校验失败" in fb
    assert fb.index("确定性校验失败") < fb.index("问题清单")
    assert "数据缺失" in fb


def test_feedback_works_without_state():
    class _Agg:
        avg_score = 7.0
        min_score = 6.0
        all_issues: ClassVar[list[str]] = []
        all_suggestions: ClassVar[list[str]] = []
        unstable_cases: ClassVar[list[int]] = []

    fb = _build_feedback(_Agg(), [_eval()])
    assert "综合评分" in fb
    assert "确定性校验失败" not in fb


# --------------------------------------------------------------------------
# 竞品机制：fixed–broken 记账 / 对比切片 / 高方差用例优先（GEPA·PRISM·SIMBA）
# --------------------------------------------------------------------------
from pm.nodes.judge import _contrastive_slice, _fixed_broken  # noqa: E402
from pm.schemas import DimensionScores, EvaluationResult  # noqa: E402


def _ev(idx: int, score: float, spread: float = 0.0) -> EvaluationResult:
    return EvaluationResult(
        issues=[f"case{idx} 的问题"],
        suggestions=[],
        dimension_scores=DimensionScores(
            task_completion=score,
            format_adherence=score,
            constraint_compliance=score,
            robustness=score,
            quality=score,
        ),
        model_reported_score=score,
        test_case_index=idx,
        score_spread=spread,
    ).finalize()


def test_fixed_broken_only_counts_common_cases():
    prev = {"0": 9.0, "1": 5.0, "2": 8.5}
    cur = {0: True, 1: True, 3: True}  # case2 本轮没评上，case3 是新增
    delta = _fixed_broken(prev, cur, 8.0)
    assert delta["fixed"] == [1]
    assert delta["broken"] == []
    assert delta["comparable"] == 2  # 0 与 1
    assert delta["net"] == 1


def test_fixed_broken_detects_oscillation_that_avg_hides():
    """均分一模一样，但一轮净修好、一轮修 2 坏 2——震荡必须被看见。"""
    prev = {"0": 9.0, "1": 9.0, "2": 5.0, "3": 5.0}
    cur = {0: False, 1: False, 2: True, 3: True}
    delta = _fixed_broken(prev, cur, 8.0)
    assert delta["fixed"] == [2, 3] and delta["broken"] == [0, 1]
    assert delta["net"] == 0
    assert _fixed_broken(None, cur, 8.0) == {"fixed": [], "broken": [], "net": 0, "comparable": 0}


def test_contrastive_slice_needs_a_passing_side():
    runs = [
        {"test_case_index": 0, "test_input": "输入A 完整数据", "output": "结论A 合规"},
        {"test_case_index": 1, "test_input": "输入B 缺字段", "output": "结论B 编造了数字"},
    ]
    assert _contrastive_slice([_ev(0, 6.0), _ev(1, 4.0)], runs) == ""  # 没有通过侧
    text = _contrastive_slice([_ev(0, 9.0), _ev(1, 4.0)], runs)
    assert "✅ 通过（case#0" in text and "❌ 失败（case#1" in text
    assert "输入A 完整数据" in text and "两条输入的差别" in text


def test_feedback_carries_delta_and_reflection_targets():
    from pm.schemas import AggregateScore

    evals = [_ev(0, 6.0, spread=2.4), _ev(1, 9.0)]
    agg = AggregateScore.from_evaluations(evals, n_expected=2)
    state = {
        "test_runs": [
            {"test_case_index": 0, "test_input": "缺字段输入", "output": "漏标数据缺失"},
            {"test_case_index": 1, "test_input": "完整输入", "output": "合规输出"},
        ],
        "revision_delta": {"fixed": [1], "broken": [0], "net": 0, "comparable": 2},
    }
    text = _build_feedback(agg, evals, state)
    assert "与上一版逐条对照：修好 1 条（case#1）／弄坏 1 条（case#0）" in text
    assert "先恢复它们" in text  # 弄坏的是原本通过的用例 → 止盈优先
    assert "本轮最该反思的用例" in text and "case#0 加权 6.0" in text
    assert "同一份提示词，两条用例的分野" in text


def test_judge_spec_pins_rubric_version():
    """评分提示词一改，评估缓存键必须变（否则新标准汇报旧判定）。"""
    from pm.nodes.judge import _judge_spec
    from pm.prompts import rubric_stamp

    spec = _judge_spec(["evaluator"])
    assert spec.endswith(f"|rubric={rubric_stamp()}")
    assert len(rubric_stamp()) == 10
