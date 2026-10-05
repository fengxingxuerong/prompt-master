"""`agg.noise` 必须区分"重复采样很稳"与"噪声测不出来"（2026-10-05 审 §二十一·六）。

## 缺陷（**先于本会话存在**，与 §二十一 的哨兵缺陷同源不同表现）

`AggregateScore.from_evaluations` 里这一行（`pm/schemas.py`）：

```python
spreads = [e.score_spread for e in evals if e.score_spread > 0]
noise = round(sum(spreads) / len(spreads), 3) if spreads else 0.0
```

`score_spread` 是该用例重复采样的极差（max-min）。把 `== 0` 的用例**过滤掉**，
于是 `noise` 收敛到"所有用例采样结果完全一致"时的 `0.0`。

但 `0.0` 这个读数是**二义的**，两种截然不同的情况共用它：

| 情况 | 真实含义 | 现在报成 |
|---|---|---|
| 用例重复采样，分数稳定落在一个值 | 重复性很好 | `0.0` ✅ |
| 采样了，但样本少 / 恰好撞一起，**测不出**波动 | 噪声**不可估** | `0.0` ❌ |

第二种被报成第一种，是**方向相反**的误报：读者据此以为这一轮的噪声带是可信的窄带，
而 `ci_lower = avg - 1.96·SEM - 0.5·noise_total` 正是靠 `noise_total` 收紧达标线。

实测（`PM_SAMPLES_PER_CASE=1` 是默认）：单用例、采样全同 ⇒ `noise=0.0`、`n_samples=1`，
报告印"采样噪声（平均极差）：0.0"。**没有任何一处告诉读者这个 0 是"稳"还是"没测到"。**

注意这与 §二十一 修的哨兵缺陷是**同一个病根**：
"采样次数不足以估计噪声"这件事在这套代码里**从来没有一个独立的表示**——
只能靠 `None`，而 `None` 又被合成噪声吃掉了。

## 修法

`AggregateScore` 增加 `noise_measurable: bool`（点名，不复用 `0.0` 糊弄）：
`n_samples >= 2` 且**至少有一个用例的极差 > 0** 才算测得到。
报告在不可测时说破，不让读者把"没测到"当成"很稳"。
"""

from __future__ import annotations

import pytest
from pm import schemas
from pm.schemas import AggregateScore, DimensionScores, EvaluationResult


def _mk(idx: int, score: float, spread: float, n_samples: int = 1) -> EvaluationResult:
    return EvaluationResult(
        dimension_scores=DimensionScores(
            task_completion=int(score),
            format_adherence=int(score),
            constraint_compliance=int(score),
            robustness=int(score),
            quality=int(score),
        ),
        model_reported_score=score,
        test_case_index=idx,
        score_spread=spread,
        n_samples=n_samples,
    ).finalize()


def _agg(*evals: EvaluationResult, n_expected: int) -> AggregateScore:
    return AggregateScore.from_evaluations(list(evals), n_expected=n_expected)


# ---------------------------------------------------------------------------
# 判据一：`noise == 0.0` 时必须能说出它到底是什么意思
# ---------------------------------------------------------------------------
def test_zero_noise_must_declare_whether_it_is_measurable() -> None:
    """默认单采样那一档：噪声测不出来，不许与"重复性很好"共用一个读数。"""
    agg = _agg(_mk(0, 8.0, 0.0), n_expected=1)
    assert agg.n_samples == 1
    assert agg.noise == 0.0
    assert agg.noise_measurable is False, "单采样测不出噪声，不能报成 0 宽的带"


def test_really_repeatable_stays_measurable() -> None:
    """采样多次且极差为 0 —— 那是"很稳"，与上一条必须区分开。"""
    agg = _agg(_mk(0, 8.0, 0.0, n_samples=3), n_expected=1)
    assert agg.n_samples == 3
    assert agg.noise == 0.0
    # 极差仍为 0：样本撞在一起 ⇒ 极差这个估计量在此无信息，仍算不可估
    assert agg.noise_measurable is False


def test_a_real_spread_is_measurable() -> None:
    agg = _agg(_mk(0, 8.0, 1.2, n_samples=3), n_expected=1)
    assert agg.noise == 1.2
    assert agg.noise_measurable is True


def test_mixed_run_needs_only_one_measured_case() -> None:
    """一部分用例撞在一起、一部分有波动：只要有波动就测得到（旧的过滤逻辑）。"""
    agg = _agg(_mk(0, 8.0, 1.2, n_samples=3), _mk(1, 9.0, 0.0, n_samples=3), n_expected=2)
    assert agg.noise == 1.2
    assert agg.noise_measurable is True


def test_a_spread_without_multi_sampling_is_still_unmeasurable() -> None:
    """只有 1 次采样却有极差 —— 那不是重复采样测出来的，不算测到了。

    保守方向：宁可说"不可估"，也不把一个来源不明的带宽当成实测精度。
    """
    agg = _agg(_mk(0, 8.0, 1.2, n_samples=1), n_expected=1)
    assert agg.noise == 1.2
    assert agg.noise_measurable is False, "单次采样不可能测出极差"


# ---------------------------------------------------------------------------
# 判据二：不可测时不能拿 0 去收窄达标线之外的任何"确定性"表述
# ---------------------------------------------------------------------------
def test_ci_lower_still_honours_a_measured_band() -> None:
    """可测时，`ci_lower` 仍要吸收噪声带（这条不许被本轮动到）。"""
    measured = _agg(_mk(0, 8.0, 1.2, n_samples=3), n_expected=1)
    unmeasured = _agg(_mk(0, 8.0, 0.0, n_samples=1), n_expected=1)
    assert measured.ci_lower < unmeasured.ci_lower, "带更宽 ⇒ 下界必须更低（保守方向）"


def test_json_carries_the_flag() -> None:
    """落盘 JSON 要带上这个标记（报告是从 dict 渲染的）。"""
    d = _agg(_mk(0, 8.0, 0.0), n_expected=1).model_dump()
    assert "noise_measurable" in d
    assert d["noise_measurable"] is False


# ---------------------------------------------------------------------------
# 判据三：报告必须说破，不许静默印一个 0
# ---------------------------------------------------------------------------
def test_report_says_so_when_noise_is_unmeasurable() -> None:
    from pm.report import render_report

    agg = _agg(_mk(0, 8.0, 0.0, n_samples=1), n_expected=1).model_dump()
    state = {"task": "t", "aggregate": agg, "versions": [{"avg_score": 8.0}]}
    md = render_report(state)[0]
    assert "采样噪声（平均极差）：0.0" in md, "原有那一行要保留（形状别变）"
    assert "噪声不可估" in md, "必须在报告里说破：0 是没测到，不是很稳"


def test_report_stays_quiet_when_noise_was_measured() -> None:
    from pm.report import render_report

    agg = _agg(_mk(0, 8.0, 1.2, n_samples=3), n_expected=1).model_dump()
    state = {"task": "t", "aggregate": agg, "versions": [{"avg_score": 8.0}]}
    md = render_report(state)[0]
    assert "噪声不可估" not in md, "测到了就不必啰嗦"


def test_measurement_noise_still_returns_none_for_unmeasurable_runs(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """与 §二十一 连起来看：不可测那一路必须一路透传 `None` 到余量判据。"""
    monkeypatch.setenv("PM_LOG_DIR", str(tmp_path))
    schemas._measured_jitter.cache_clear()
    monkeypatch.setattr(schemas, "JUDGE_JITTER", 0.0)
    # judge.py:993 的判据是 (n_samples or 1) >= 2；单采样 ⇒ None
    assert schemas.measurement_noise(None) is None
