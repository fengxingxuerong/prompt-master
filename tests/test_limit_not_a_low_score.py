"""429 / 配额类失败没被判成无效样本——修复漏洞的守卫（2026-10-06 §三十·十四）

§三十·十一 修了「超时」，但那五个字面量没覆盖**限流/配额**这一整类。
真实跑 `run_71ee15abd123` 照出来了：

  case#0 sample_scores=[8.1, 1.0] → weighted=4.55
  case#2 sample_scores=[3.85, 1.0] → weighted=2.42（judge="system"，五维全 1.0）

两次 429 的原文：
  `evaluate#2[evaluator]: Error code: 429 - {'error': {'message':
   'inference exceeds tpm/rpm limit', 'type': 'rate_limit_error',
   'code': 'insufficient_quota'}}`

**注意 case#2 的形态**：五维全 1.0、`judge="system"`、
issues 是「全部评委评估失败：…429…」——这是 `_evaluate_one` 的
`if not results:` 兜底分支（judge.py:505），**根本没经过评委**。
而 case#0 更隐蔽：那条 issues 里只有评委评语（说明那次采样成功评了），
但 errors 里另有一次 429 ⇒ 那个 1.0 是**另一条**失败路径留下的，
两条证据不在同一个字段里。

⇒ 判据必须同时吃两个通道：`issues`（采样级）与 `errors`（用例级）。
只吃 issues 的话，case#0 那种漏；只吃 errors 的话，
折叠阶段（`_collapse_samples`）拿不到 errors，case#2 那种也漏。

先钉现状（复现红），再修。
"""

from __future__ import annotations

import pytest
from pm.nodes.judge import _collapse_samples, _is_empty_output_eval

LIMIT_ERR = (
    "Error code: 429 - {'error': {'message': 'inference exceeds tpm/rpm limit', "
    "'type': 'rate_limit_error', 'code': 'insufficient_quota'}}"
)


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
# 原有能力不许丢
# ---------------------------------------------------------------------------
def test_empty_output_still_recognised() -> None:
    assert _is_empty_output_eval(_eval(0, 1.0, ["empty_output"])) is True


def test_timeout_still_recognised() -> None:
    assert _is_empty_output_eval(_eval(0, 1.0, ["Request timed out."])) is True


def test_real_low_score_stays_valid() -> None:
    assert _is_empty_output_eval(_eval(0, 1.0, ["输出确实很差，没有按要求分章节"])) is False


# ---------------------------------------------------------------------------
# 漏网：429 / 配额
# ---------------------------------------------------------------------------
def test_rate_limit_is_recognised() -> None:
    assert _is_empty_output_eval(_eval(0, 1.0, [f"全部评委评估失败：{LIMIT_ERR}"])) is True


@pytest.mark.parametrize(
    "issue",
    [
        "Error code: 429",
        "insufficient_quota",
        "rate_limit_error",
        "Error code: 503 - {'error': {'message': 'service overloaded'}}",
        "Connection error.",
    ],
)
def test_more_failure_shapes(issue: str) -> None:
    assert _is_empty_output_eval(_eval(0, 1.0, [issue])) is True


def test_system_fallback_evaluation_is_invalid() -> None:
    """`judge="system"` 形态：没经过评委，五维全 1.0。

    这是 `_evaluate_one` 的 `if not results:` 兜底（judge.py:505）。
    它是**结构信号**，比文本匹配可靠：真有评委给出五维全 1.0 是可能的，
    但同时 judge="system" 且 issues 说"全部评委评估失败"——两义又出现了，
    所以只认"全 1.0 **且** system"这个组合。
    """
    ev = _eval(0, 1.0, ["全部评委评估失败：whatever"])
    ev["judge"] = "system"
    ev["dimension_scores"] = dict.fromkeys(
        ["quality", "task_completion", "format_adherence", "constraint_compliance", "robustness"],
        1.0,
    )
    assert _is_empty_output_eval(ev) is True


def test_real_429_run_is_not_polluted() -> None:
    """拿真实归档回归：case#2 折叠后必须不再是 2.42。"""
    import json
    from pathlib import Path

    p = Path(__file__).resolve().parents[1] / "logs" / "run_71ee15abd123.json"
    if not p.exists():
        pytest.skip("归档不在本机")
    d = json.loads(p.read_text(encoding="utf-8"))
    e2 = next(x for x in d["evaluations"] if x["test_case_index"] == 2)
    assert e2["sample_scores"] == [3.85, 1.0], "归档变了，需要重新判断这个用例"
    assert e2["judge"] == "system"
    assert _is_empty_output_eval(e2) is True

    fixed = [
        _eval(2, 3.85, ["评委评语"]),
        _eval(2, 1.0, [f"全部评委评估失败：{LIMIT_ERR}"]),
    ]
    fixed[1]["judge"] = "system"
    fixed[1]["dimension_scores"] = dict.fromkeys(
        ["quality", "task_completion", "format_adherence", "constraint_compliance", "robustness"],
        1.0,
    )
    out = _collapse_samples(fixed)
    assert out[0]["weighted_score"] == 3.85, (
        f"折叠后 {out[0]['weighted_score']}，期望 3.85——429 废样本污染了中位数"
    )
    assert out[0]["score_spread"] == 0.0
