"""`_is_empty_output_eval` 只认「空输出」，不认「超时」——废样本照样进均值。

真实事故（2026-10-06 §三十·十一，`run_88dcfd4d9c29` 单评委轮）：
case#3 两次采样得 **7.6 与 1.0**。读输出全文（311 字）——三章节齐全、
逐条标注「数据缺失」、明确拒绝预测股市，**完全合规**。那次 1.0 是
**请求超时**（墙钟预算 600s 用尽 / Request timed out）后的兜底分，
不是质量读数。

后果：折叠取中位数 [(1.0+7.6)/2 = 4.3] → `min_score=4.3` → `ci_lower` 从
本该有的水平掉到 **3.56**，C1 直接不达标。**一条传输失败绑架了整条臂的达标判定。**

而 `_collapse_samples` 的注释写着历史事故「矩阵 A：基线 8 条里 4 条空输出，
Δ=+6.02 含此偏差」——**空输出这一种形态治好了，超时这一种没治**。

修法方向：把「无效样本」的判定从"认一个字面量"扩成"认失败类别"。
但 `issues` 里混着评委评语（也有可能是别的形态），所以判据要落在
**结构化证据**上而不是文本匹配上。

先钉住现状（复现红），再修。
"""

from __future__ import annotations

import pytest
from pm.nodes.judge import _collapse_samples, _is_empty_output_eval

GOOD = "## 趋势结论\n- 数据缺失：未收到任何区域销售额\n\n## 异常点列表\n数据缺失\n\n## 数据完整性说明\n数据缺失\n"


def _eval(idx: int, score: float, issues: list[str]) -> dict:
    return {
        "test_case_index": idx,
        "sample_index": 0,
        "weighted_score": score,
        "model_reported_score": score,
        "score_spread": 0.0,
        "dimension_scores": {
            "quality": score,
            "task_completion": score,
            "format_adherence": score,
            "constraint_compliance": score,
            "robustness": score,
        },
        "issues": issues,
        "suggestions": [],
        "judge": "evaluator",
        "judge_scores": {},
        "passed": False,
    }


# ---------------------------------------------------------------------------
# 现状：只有「空输出」被判无效
# ---------------------------------------------------------------------------
def test_empty_output_is_still_recognised() -> None:
    """原有能力不许丢——这是治好的那半边。"""
    assert _is_empty_output_eval(_eval(0, 1.0, ["empty_output"])) is True


def test_normal_low_score_is_valid() -> None:
    """真实低分必须仍被当作有效样本。"""
    assert _is_empty_output_eval(_eval(0, 1.0, ["输出确实很差，没有按要求分章节"])) is False


# ---------------------------------------------------------------------------
# 缺口：超时导致的废样本
# ---------------------------------------------------------------------------
def test_timeout_is_recognised_as_invalid_sample() -> None:
    """超时不是低分，是没拿到分。"""
    ev = _eval(
        0,
        1.0,
        [
            "[evaluator] 墙钟预算 600s 用尽（实际 302s，已发 1 次真实请求）。"
            "最后一次错误：Request timed out."
        ],
    )
    assert _is_empty_output_eval(ev) is True, (
        "超时样本被当成有效读数——case#3 的 1.0 就是这样把 min_score 拖到 4.3、"
        "ci_lower 拖到 3.56 的（§三十·十一 实测）"
    )


@pytest.mark.parametrize(
    "issue",
    [
        "Request timed out.",
        "墙钟预算 600s 用尽（实际 302s）",
        "evaluate#3[evaluator_b]: [evaluator_b] 墙钟预算 600s 用尽。最后一次错误：Request timed out.",
        "结构化输出失败，已重试 3 次。最后错误：ValueError: 未在输出中找到合法 JSON 对象。",
    ],
)
def test_failure_shapes_are_recognised(issue: str) -> None:
    """失败形态不止一种，判据不能只认一个字面量。"""
    assert _is_empty_output_eval(_eval(0, 1.0, [issue])) is True


def test_collapse_drops_the_failed_sample() -> None:
    """核心回归：超时的那次采样不许进中位数。"""
    group = [
        _eval(3, 7.6, ["输出合规，三章节齐全"]),
        _eval(3, 1.0, ["Request timed out."]),
    ]
    group[1]["sample_index"] = 1
    out = _collapse_samples(group)
    assert len(out) == 1
    assert out[0]["weighted_score"] == 7.6, (
        f"折叠后是 {out[0]['weighted_score']}，期望 7.6——废样本污染了中位数"
    )
    assert out[0]["score_spread"] == 0.0, "只剩一个有效样本时极差应为 0"


def test_all_samples_failed_keeps_the_case() -> None:
    """全失败时用例不能凭空消失（否则臂会少一条，n_cases 对不上）。"""
    group = [
        _eval(3, 1.0, ["Request timed out."]),
        _eval(3, 1.0, ["Request timed out."]),
    ]
    out = _collapse_samples(group)
    assert len(out) == 1, "全失败时要保留（记为不可信），而不是让用例消失"


def test_real_timeout_run_is_not_polluted() -> None:
    """拿真实归档回归：case#3 折叠后应是 7.6，不是 4.3。"""
    import json
    from pathlib import Path

    p = Path(__file__).resolve().parents[1] / "logs" / "run_88dcfd4d9c29.json"
    if not p.exists():
        pytest.skip("归档不在本机")
    d = json.loads(p.read_text(encoding="utf-8"))
    e3 = next(x for x in d["evaluations"] if x["test_case_index"] == 3)
    assert e3["sample_scores"] == [7.6, 1.0], "归档变了，需要重新判断这个用例"
    # 输出全文合规，说明 7.6 才是真实读数
    out = next(str(r.get("output") or "") for r in d["test_runs"] if r.get("test_case_index") == 3)
    assert "数据缺失" in out and "趋势结论" in out
    assert "范围外请求" in out and "股市行情预测超出" in out, "它拒绝了域外请求，这正是要考的能力"

    fixed = [
        _eval(3, 7.6, ["输出合规"]),
        _eval(3, 1.0, ["Request timed out."]),
    ]
    assert _collapse_samples(fixed)[0]["weighted_score"] == 7.6
    assert e3["weighted_score"] == 4.3, "归档是修之前的读数（(1.0+7.6)/2）"
