"""失败的臂不许被"噪声不可估"这条过滤**静默删掉**（§三十·一）。

## 缺陷是怎么被发现的

按 §十八·六 的新口径重算归档读数（本轮，`recompute_ledger.py`）时，
`run.py history` 对 8 臂任务报 Δ=+1.34 CI[−0.39,+3.06]（不显著），
而"只留噪声可估的臂"报 Δ=+2.15 CI[+1.42,+2.89]（**显著**）。
8×1.34−7×2.15 ≈ −4.33 —— 被筛掉的那条 Δ≈−4.4。

把它翻出来看：`status=failed`，`errors` 全是 `Connection error.`，
四条用例的 `score_spread` 全 0、`weighted_score` 全 1.0、avg 恰好 1.0。

于是 `from_evaluations` 的
`noise_measurable = (n_samples >= 2) and bool(spreads)`（pm/schemas.py:1078）
把这条**连接失败的臂**判成"噪声不可估"，而它其实是**最不该被丢掉的观测**：
Δ=−4.39，是那 7 条里最差的。

## 为什么这是最坏的一种错

"噪声不可估"这条过滤**本来是好的**（§二十 用来挡住单采样假装测出噪声）。
它的毛病不在判据，而在**判据只说了带测不测得出来、没说这条数据本身可不可信**，
于是任何"全 0 分数"的臂都会被它顺带删掉 —— 包括连接失败、包括结构化输出
彻底崩掉、包括评委集体给了 1 分。

这些恰恰是**信号最强**的臂。筛掉的不是噪声，是结论。

## 判据

1. 至少有一条用例**失败/不可信**的臂，绝不许被当成"不可估"而消失；
2. 这类臂必须能被单独识别出来，供统计侧选择"排除并说明理由"，
   而不是被 `noise_measurable` 顺手带走；
3. `noise_measurable` 仍然只回答"带测不测得出来"，不扩张成"这条数据好不好"
   —— 两个问题必须能被分开问。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pm.schemas import AggregateScore, EvaluationResult

RUNS = Path(__file__).resolve().parents[1] / "logs"


def _mk(idx: int, score: float, spread: float, n_samples: int = 1) -> EvaluationResult:
    return EvaluationResult(
        weighted_score=score,
        judge_scores={"evaluator": score},
        model_reported_score=score,
        dimension_scores={
            "quality": score,
            "task_completion": score,
            "format_adherence": score,
            "constraint_compliance": score,
            "robustness": score,
        },
        test_case_index=idx,
        score_spread=spread,
        n_samples=n_samples,
    ).finalize()


def _agg(*evals: EvaluationResult, n_expected: int) -> AggregateScore:
    return AggregateScore.from_evaluations(list(evals), n_expected=n_expected)


# ---------------------------------------------------------------------------
# 判据一：失败臂不许消失
# ---------------------------------------------------------------------------
def test_all_ones_from_a_failed_run_is_not_merely_unmeasurable() -> None:
    """复现 `run_7d87c5065c55`：连接失败 → 四条用例全 1.0、极差全 0。

    它**看起来**和"重复打分完全一致"一模一样 —— 这正是判据失效的地方。
    """
    agg = _agg(
        _mk(0, 1.0, 0.0, n_samples=2),
        _mk(1, 1.0, 0.0, n_samples=2),
        _mk(2, 1.0, 0.0, n_samples=2),
        _mk(3, 1.0, 0.0, n_samples=2),
        n_expected=4,
    )
    assert agg.noise == 0.0
    # 关键：它必须能被识别成"这条臂不可信"，而不只是"带测不出来"
    assert agg.untrusted_case_indices == [0, 1, 2, 3], (
        "连接失败导致的全 1 分必须被标成不可信，否则统计侧会把它和'重复性很好'混为一谈然后顺手丢掉"
    )


def test_mixed_arm_keeps_the_good_cases_measurable_and_flags_the_bad() -> None:
    """混合臂：好用例有真实波动，坏用例是 1.0。两个问题分别回答。"""
    agg = _agg(
        _mk(0, 8.5, 1.2, n_samples=3),
        _mk(1, 1.0, 0.0, n_samples=3),
        n_expected=2,
    )
    assert agg.noise_measurable is True, "有真波动的臂，带照样测得出来"
    assert agg.untrusted_case_indices == [1], "而坏用例要单独点名"


def test_a_genuinely_stable_good_run_is_trusted() -> None:
    """对照组：分数正常、只是重复打分完全一致 —— 不该被误伤成不可信。"""
    agg = _agg(_mk(0, 8.0, 0.0, n_samples=3), n_expected=1)
    assert agg.untrusted_case_indices == []


# ---------------------------------------------------------------------------
# 判据二：真实归档里那两条被删掉的臂，必须能被这个标记抓到
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "run_id", ["7d87c5065c55", "1b234ac88efa"], ids=["连接失败", "结构化输出全崩"]
)
def test_the_real_discarded_arms_are_flagged_untrusted(run_id: str) -> None:
    """钉住**实际发生过**的那两条臂，不让结论只活在文档里。

    这两条就是 §十七·五 那句"显著"的来源 —— 被"噪声不可估"这条
    好意的过滤删掉了，且删掉的正是最差的两条。
    """
    p = RUNS / f"run_{run_id}.json"
    if not p.exists():
        pytest.skip("归档不在本机")
    d = json.loads(p.read_text(encoding="utf-8"))
    agg = d["aggregate"]
    evals = d["evaluations"]
    untrusted = sorted(
        {
            e["test_case_index"]
            for e in evals
            if float(e.get("weighted_score") or 0) <= 1.0 and float(e.get("score_spread") or 0) == 0
        }
    )
    assert agg["avg_score"] <= 3.0, "这两条臂确实是崩掉的（avg 远低于正常量级）"
    assert agg["passed"] is False
    # 判据是"落在下限且极差为 0"，所以**只能**要求下限那些被认出来，
    # 不能要求全部用例都被认出来 —— `1b234ac88efa` 的 case#2 评了 6.7，
    # 那是一次真实测量（它该被认成好数据，尽管那一轮整体崩了）。
    floor = [e["test_case_index"] for e in evals if float(e.get("weighted_score") or 0) <= 1.0]
    assert floor, f"{run_id} 至少要有落到量纲下限的用例，否则这条就不该标不可信"
    assert set(floor) <= set(untrusted), f"下限用例 {floor} 未被全标成不可信；只认出 {untrusted}"
