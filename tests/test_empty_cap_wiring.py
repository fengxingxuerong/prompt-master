"""空产出硬封顶的接线测试：封顶必须同源进聚合、进 state、进报告披露。

背景（§十七·十 照出，2026-10-05 串行测量 run `54b28925695a`）：封顶只改了
`evaluations` dict 的 weighted_score（落盘全 4.0），而聚合用的是**封顶前**
建的 EvaluationResult 对象（报告说 7.6），披露分支读的 `state["empty_cap"]`
从来没人写过——落盘、报告、披露三方对不上，且当时只凭读文件得出、零 pytest
复现（文档原话：「引用前请自行复现」）。本文件补上复现与钉死。
"""

from __future__ import annotations

from typing import Any

from pm.nodes import judge as J
from pm.report import _empty_deliverable_lines
from pm.schemas import DimensionScores, EvaluationResult

# 与 tests/test_empty_deliverable_alert.py 同款画像文本：
# _SKELETON 必然命中空产出画像（apply_empty_deliverable_cap 会压到 4.0）
_SKELETON = "今日无可用工作记录，请提供后再生成\n\n工作日报\n【今日完成】\n无\n【明日计划】\n无"
_GOOD = (
    "## 销售数据分析报告\n### 一、数据概览\n- 华东120万、华南98万、华北45万、西南12万\n"
    "### 二、趋势结论\n横截面数据无法判定时间趋势\n### 三、异常点\n西南低于均值50%阈值\n"
    "### 四、数据缺失说明\n无缺失\n### 五、建议\n排查西南区域渠道覆盖与客户开发情况"
)


def _eval(case_idx: int, score: float) -> dict[str, Any]:
    """构造一条高分评估（评委眼里五维全优）——封顶要拦的正是这种。"""
    return (
        EvaluationResult(
            dimension_scores=DimensionScores(
                task_completion=score,
                format_adherence=score,
                constraint_compliance=score,
                robustness=score,
                quality=score,
            ),
            model_reported_score=score,
            issues=[],
            suggestions=[],
            should_revise=False,
            test_case_index=case_idx,
        )
        .finalize()
        .model_dump()
    )


def _state() -> dict[str, Any]:
    """case#0 空壳输出 + case#1 实质报告：封顶只该命中前者。"""
    return {
        "task": "销售数据分析",
        "context": "",
        "prompt": "你是数据分析助手。<数据>{input}</数据>",
        "iteration": 0,
        "max_iterations": 3,
        "n_test_cases": 2,
        "prompt_versions": [
            {"iteration": 0, "prompt": "v0", "avg_score": 9.5, "status": "running"}
        ],
        "test_runs": [
            {
                "test_case_index": 0,
                "sample_index": 0,
                "test_input": "（无数据）",
                "prompt": "v0",
                "output": _SKELETON,
                "error": None,
            },
            {
                "test_case_index": 1,
                "sample_index": 0,
                "test_input": "华东120万、华南98万",
                "prompt": "v0",
                "output": _GOOD,
                "error": None,
            },
        ],
        "injection_survival": None,
        "unresolved_questions": [],
    }


def _fake_evaluate(monkeypatch):
    """屏蔽真实 LLM：case#0 评委裸分 9.5（空壳却被夸）、case#1 实打实 8.0。"""
    monkeypatch.setattr(
        J,
        "_evaluate_runs",
        lambda *a, **k: ([_eval(0, 9.5), _eval(1, 8.0)], [], 0, 0),
    )


# --------------------------------------------------------------------------
# 接线三处（§十七·十 的三条对不上）
# --------------------------------------------------------------------------
def test_cap_feeds_aggregate(monkeypatch):
    """封顶触发后，聚合 avg/min 必须基于封顶值——达标判定不得用封顶前裸分。"""
    _fake_evaluate(monkeypatch)
    out = J.evaluate_node(_state())
    agg = out["aggregate"]
    # 封顶前裸分：avg=(9.5+8.0)/2=8.75 ≥ 8.0 会误判达标；封顶后 avg=6.0 不可用档
    assert agg["avg_score"] == 6.0, f"聚合必须吃封顶：{agg['avg_score']}"
    assert agg["min_score"] == 4.0, f"封顶值要体现在 min：{agg['min_score']}"
    assert agg["passed"] is False, "空壳换来的达标是放水"


def test_cap_reaches_state(monkeypatch):
    """封顶触发后 state 必须带 empty_cap（此前只进 trace 事件，state 永远没有）。"""
    _fake_evaluate(monkeypatch)
    out = J.evaluate_node(_state())
    assert out.get("empty_cap") == 1


def test_report_discloses_cap(monkeypatch):
    """封顶触发时，交付报告披露行必须出现——键名契约：evaluate_node 的 patch
    键 empty_cap ↔ 渲染层 state["empty_cap"]（此前渲染读的键没人写过）。"""
    _fake_evaluate(monkeypatch)
    out = J.evaluate_node(_state())
    assert out.get("empty_cap") == 1
    # 渲染层拿完整 state 渲染（真实链路里 test_runs 从 test 节点起就一直在 state）；
    # 这里按真实延续方式拼装：evaluate 的 patch 键 + 既有 test_runs。
    render_state = {
        "evaluations": out["evaluations"],
        "test_runs": _state()["test_runs"],
        "empty_cap": out["empty_cap"],
    }
    lines = _empty_deliverable_lines(render_state)
    assert lines, "披露分支不能是死的"
    assert any("硬封顶" in ln and "1 条" in ln for ln in lines), lines


def test_no_cap_no_disclosure(monkeypatch):
    """没有空产出时不得误报披露：两条实质报告 + 正常分 → state 无 empty_cap、渲染层无披露行。"""
    monkeypatch.setattr(
        J,
        "_evaluate_runs",
        lambda *a, **k: ([_eval(0, 8.0), _eval(1, 8.0)], [], 0, 0),
    )
    normal = _state()
    normal["test_runs"] = [{**tr, "output": _GOOD} for tr in normal["test_runs"]]
    out = J.evaluate_node(normal)
    assert not out.get("empty_cap")
    render_state = {
        "evaluations": out["evaluations"],
        "test_runs": normal["test_runs"],
        "empty_cap": out.get("empty_cap"),
    }
    assert _empty_deliverable_lines(render_state) == []
