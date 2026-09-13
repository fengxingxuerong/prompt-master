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

    fb = _build_feedback(_Agg(), [_eval()])
    assert "综合评分" in fb
    assert "确定性校验失败" not in fb
