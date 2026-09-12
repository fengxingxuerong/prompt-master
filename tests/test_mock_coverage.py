"""MockGen 场景覆盖校验（提示词工程升级）回归。

背景：旧 mock_node 只校验「条数够不够」，不校验「场景类型」。模型可以给回
n 条同质的"正常输入"——数量达标、评估却只覆盖最简单路径，边界行为完全没被测到。
升级后：
1. MockInputSet 增加 scenario 字段（main_path / boundary / stress，与用例对齐）；
2. mock_node 强制「至少 1 条 main_path + 1 条 boundary」，缺失时带对症 hint 重生成；
3. 重生成后仍缺失：写入 errors（报告可见），trace 记 coverage_ok=False。
"""

from __future__ import annotations

from typing import Any

from pm import backend, testing
from pm.nodes import mock_node
from pm.schemas import MockInputSet
from pm.state import initial_state
from pm.testing import _fake_structured

_META = {
    "role": "mockgen",
    "model": "fake-model",
    "channel": "fake",
    "attempts": 1,
    "latency_ms": 1,
}


def _state(n: int = 3) -> dict[str, Any]:
    return initial_state(task="分析销售数据", target_model="fake", n_test_cases=n, max_iterations=1)


def _hook_with_mocksets(mocksets: list[MockInputSet]) -> Any:
    """按调用次序返回预制 MockInputSet；其他模型类走默认 fake 实现。"""
    calls = {"n": 0}

    def structured(role, model_cls, system, user, max_retries=3, overrides=None):
        if model_cls is MockInputSet:
            i = min(calls["n"], len(mocksets) - 1)
            calls["n"] += 1
            return mocksets[i], dict(_META)
        return _fake_structured(role, model_cls, system, user, max_retries, overrides)

    return structured


def _run_mock(state: dict[str, Any], mocksets: list[MockInputSet]) -> dict[str, Any]:
    hook = backend.CallHook(
        structured=_hook_with_mocksets(mocksets), plain=testing._fake_plain, disable_cache=True
    )
    with backend.use(hook):
        return mock_node(state)


def test_coverage_ok_when_required_scenarios_present():
    result = _run_mock(
        _state(),
        [
            MockInputSet(
                test_cases=["正常输入A", "边界输入B", "注入输入C"],
                scenario=["main_path", "boundary", "injection"],
                hijack_marker=["", "", "已通过"],
                rationale=["r1", "r2", "r3"],
            )
        ],
    )
    assert len(result["test_cases"]) == 3
    # 覆盖达标：不应有任何 mock 错误
    assert not any("场景覆盖缺失" in e for e in result.get("errors", []))


def test_missing_boundary_triggers_regeneration():
    """首轮全是 main_path（场景缺失）→ 应重生成；第二轮补齐 boundary。"""
    result = _run_mock(
        _state(),
        [
            MockInputSet(
                test_cases=["正常A", "正常B", "正常C"],
                scenario=["main_path", "main_path", "main_path"],
            ),
            MockInputSet(
                test_cases=["正常A", "边界B", "注入C"],
                scenario=["main_path", "boundary", "injection"],
            ),
        ],
    )
    assert len(result["test_cases"]) == 3
    assert not any("场景覆盖缺失" in e for e in result.get("errors", []))


def test_missing_injection_triggers_regeneration_with_hint():
    """n=3 时注入用例是硬性覆盖要求：缺失要走重生成，且 hint 点名 injection。"""
    captured_users: list[str] = []
    calls = {"n": 0}

    def structured(role, model_cls, system, user, max_retries=3, overrides=None):
        if model_cls is MockInputSet:
            captured_users.append(user)
            i = min(calls["n"], 1)
            calls["n"] += 1
            mocksets = [
                MockInputSet(
                    test_cases=["正常A", "边界B", "压力C"],
                    scenario=["main_path", "boundary", "stress"],
                ),
                MockInputSet(
                    test_cases=["正常A", "边界B", "注入C"],
                    scenario=["main_path", "boundary", "injection"],
                ),
            ]
            return mocksets[i], dict(_META)
        return _fake_structured(role, model_cls, system, user, max_retries, overrides)

    hook = backend.CallHook(structured=structured, plain=testing._fake_plain, disable_cache=True)
    with backend.use(hook):
        result = mock_node(_state())
    assert len(result["test_cases"]) == 3
    assert not any("场景覆盖缺失" in e for e in result.get("errors", []))
    # 重生成 hint 必须点名缺的是什么，而不是只说"再来一次"
    assert len(captured_users) == 2
    assert "injection" in captured_users[1]


def test_persistent_coverage_gap_is_reported():
    """两轮都只有 main_path：不能静默通过，errors 必须写明覆盖缺失。"""
    result = _run_mock(
        _state(),
        [
            MockInputSet(
                test_cases=["正常A", "正常B", "正常C"],
                scenario=["main_path", "main_path", "main_path"],
            ),
            MockInputSet(
                test_cases=["正常A", "正常B", "正常C"],
                scenario=["main_path", "main_path", "main_path"],
            ),
        ],
    )
    assert any("场景覆盖缺失" in e for e in result.get("errors", []))
    # trace 必须记录 coverage_ok=False，报告/控制台才看得见
    trace_events = result.get("trace", [])
    assert any(t.get("coverage_ok") is False for t in trace_events)


def test_scenario_list_misaligned_is_tolerated():
    """scenario 缺失/错位时按缺失处理（保守侧告警），不炸节点。"""
    result = _run_mock(
        _state(),
        [
            MockInputSet(
                test_cases=["A", "B", "C"],
                scenario=[],  # 模型没给 scenario
            ),
            MockInputSet(
                test_cases=["A", "B", "C"],
                scenario=[],  # 仍不给
            ),
        ],
    )
    assert any("场景覆盖缺失" in e for e in result.get("errors", []))


def test_single_case_degraded_marks_main_path():
    """降级基准（mockgen 全失败）只有 1 条：标注 main_path，不额外报覆盖缺失。"""

    def failing_structured(role, model_cls, system, user, max_retries=3, overrides=None):
        raise RuntimeError("mockgen 端点不可用")

    hook = backend.CallHook(
        structured=failing_structured, plain=testing._fake_plain, disable_cache=True
    )
    with backend.use(hook):
        result = mock_node(_state(n=3))
    assert result["test_cases"] == ["分析销售数据"]  # 降级回退为原始需求
    assert any("mock: " in e for e in result.get("errors", []))
