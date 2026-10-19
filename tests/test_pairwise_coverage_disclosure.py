"""成对盲评的**分母**必须说清：只跑通 2/4 例的「多数胜出」是在骗人。

起因（2026-10-18，run `845bb861ce8f` 的 R4 重跑）：报告印「优化版胜 0 / 基线胜 3 /
持平 1（共 4 例）」，读起来像四例全跑到、基线赢了三票。而同一轮的 pointwise 侧
3/4 例评委席降级 —— 盲评走的是 `comparator` 单席位，与 pointwise 席位互不印证，
读报告的人却会把这两件事当成同一枚硬币的两面。

真正要钉的是另一条：`compare_node` 里两序都失败或两侧输出都为空时直接 `continue`，
被跳过的用例**不进 details** ⇒ `n_compared` 天然小于用例数，而渲染层只印 `n_compared`、
从不印"本来该有几例"。于是「1 例出票判 worse」也能印出「基线多数胜出」。
判据口径：胜方票数必须达到**应比较例数**的多数，才配叫"多数"。
"""

from __future__ import annotations

from typing import Any

import pytest
from pm import backend
from pm.nodes import compare_node
from pm.report import _pairwise_lines
from pm.schemas import PreferenceResult
from pm.state import initial_state


def _state(n: int = 4) -> dict[str, Any]:
    st = initial_state(task="t", target_model="fake", n_test_cases=n)
    st["run_id"] = "coverage"
    st["test_cases"] = [f"i{i}" for i in range(n)]
    st["test_runs"] = [
        {
            "test_case_index": i,
            "sample_index": 0,
            "test_input": f"i{i}",
            "prompt": "p",
            "output": f"OPTIMIZED {i}",
        }
        for i in range(n)
    ]
    st["baseline_runs"] = [
        {
            "test_case_index": i,
            "sample_index": 0,
            "test_input": f"i{i}",
            "prompt": "t",
            "output": f"BASELINE {i}",
        }
        for i in range(n)
    ]
    return st  # type: ignore[return-value]


def _comparator(fail: set[int]) -> Any:
    """`fail` 里的用例序号：comparator 调用抛错 ⇒ 该例两序全灭、被跳过不计票。"""

    def _call(role, model_cls, system, user, max_retries=3, overrides=None):
        block = user.split("<TEST_INPUT>")[1].split("</TEST_INPUT>")[0].strip()
        idx = int(block.lstrip("i"))
        if idx in fail:
            raise RuntimeError("comparator 挂了")
        a_block = user.split("<CANDIDATE_A>")[1].split("</CANDIDATE_A>")[0]
        # 始终选基线那一侧（无论它坐在 A 还是 B）⇒ 两序一致，排除位置偏置干扰
        winner = "A" if "BASELINE" in a_block else "B"
        return (
            PreferenceResult(winner=winner, reason="更完整", decisive=True),
            {"model": "f", "channel": "fake", "attempts": 1, "latency_ms": 1},
        )

    return _call


def test_full_coverage_reports_no_shortfall(monkeypatch: pytest.MonkeyPatch) -> None:
    """四例全部出票时，`n_compared == n_expected`，不该有覆盖率告警。"""
    monkeypatch.setenv("PM_PAIRWISE", "1")
    with backend.use(backend.CallHook(structured=_comparator(set()), disable_cache=True)):
        pw = compare_node(_state(4))["pairwise"]
    assert pw["n_compared"] == 4
    assert pw["n_expected"] == 4
    assert pw["n_skipped"] == 0
    assert not any("覆盖" in ln for ln in _pairwise_lines({"pairwise": pw}))


def test_partial_coverage_is_counted_and_disclosed(monkeypatch: pytest.MonkeyPatch) -> None:
    """两例 comparator 全灭时，分母（应比较 4 例）必须跟 2 例一起写出来。"""
    monkeypatch.setenv("PM_PAIRWISE", "1")
    with backend.use(backend.CallHook(structured=_comparator({1, 3}), disable_cache=True)):
        pw = compare_node(_state(4))["pairwise"]
    assert pw["n_compared"] == 2
    assert pw["n_expected"] == 4
    assert pw["n_skipped"] == 2
    text = "\n".join(_pairwise_lines({"pairwise": pw}))
    assert "覆盖" in text


def test_one_vote_out_of_four_is_not_a_majority(monkeypatch: pytest.MonkeyPatch) -> None:
    """只有 1/4 例出票时，结论文案不许出现「多数」——1 票不是多数。"""
    monkeypatch.setenv("PM_PAIRWISE", "1")
    with backend.use(backend.CallHook(structured=_comparator({0, 1, 2}), disable_cache=True)):
        pw = compare_node(_state(4))["pairwise"]
    assert pw["verdict"] == "worse"
    text = "\n".join(_pairwise_lines({"pairwise": pw}))
    assert "多数" not in text


def test_three_of_four_still_reads_as_majority(monkeypatch: pytest.MonkeyPatch) -> None:
    """反过来：3/4 例出票且全判 worse，确实构成多数 ⇒ 措辞不许被上一刀误伤。"""
    monkeypatch.setenv("PM_PAIRWISE", "1")
    with backend.use(backend.CallHook(structured=_comparator({3}), disable_cache=True)):
        pw = compare_node(_state(4))["pairwise"]
    assert pw["n_compared"] == 3
    text = "\n".join(_pairwise_lines({"pairwise": pw}))
    assert "多数" in text


def test_empty_both_sides_counts_as_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    """两侧输出都为空 ⇒ 该例本就不该比较，但同样要计入 `n_skipped` 而不是凭空消失。"""

    def _call(role, model_cls, system, user, max_retries=3, overrides=None):
        return (
            PreferenceResult(winner="A", reason="更完整", decisive=True),
            {"model": "f", "channel": "fake", "attempts": 1, "latency_ms": 1},
        )

    monkeypatch.setenv("PM_PAIRWISE", "1")
    st = _state(2)
    st["test_runs"][1]["output"] = "   "
    st["baseline_runs"][1]["output"] = ""
    with backend.use(backend.CallHook(structured=_call, disable_cache=True)):
        pw = compare_node(st)["pairwise"]
    assert pw["n_compared"] == 1
    assert pw["n_expected"] == 2
    assert pw["n_skipped"] == 1
