"""评估缓存键必须带采样序号——否则"重复采样"量不出噪声带。

2026-10-09 真端点实测（`run_d01e8a9168d4`，4 用例 × 2 采样）：归档里 `/test_runs` 的
4/4 用例两次采样的 target 输出**字节相同**（低温端点近乎确定性；`cache_hit` 全是
False，所以两发都是真调用）。于是第二次评估按内容寻址命中第一次的评估缓存——聚合日志
那句"缓存命中 4"正好等于用例数。两次评估是**同一个对象** ⇒ `score_spread` 按构造为 0
⇒ 报告印出 `噪声=0.00`，读起来像"重复打分完全一致"，实际是"第二次根本没打分"。

后果不止于一行数字：达标判定看的是均分保守下界 `ci_lower`，而它 = 均分 − 噪声项。
噪声被读成 0 ⇒ 下界虚高 ⇒ **Δ 与达标都失去可采信的置信区间**（owner 三轮真端点
"优化有效"0 例确证，其中一轮就是这个形态）。

修法与 `key_for_target` 的 `|s{sample}` 同一先例：采样序号进评估缓存键。
判据（这些是可查的事实，不是我的断言）：
- 同输出、不同 `sample_index` → 必须各评一次（`n_cache_hit=0`、极差非 0）；
- 同输出、同 `sample_index`（断点续跑）→ 仍必须命中，否则这次改动只是在烧钱。
"""

from __future__ import annotations

from pathlib import Path

import pm.cache as C
import pytest
from pm import backend
from pm.nodes import judge as J
from pm.schemas import DimensionScores, EvaluationResult

OUT = "## 趋势结论\n- 华东领先\n\n## 异常点\n无\n\n## 数据完整性说明\n完整\n"


def _mk_ev(score: float, idx: int = 0) -> EvaluationResult:
    d = round(score)
    return EvaluationResult(
        dimension_scores=DimensionScores(
            task_completion=d,
            format_adherence=d,
            constraint_compliance=d,
            robustness=d,
            quality=d,
        ),
        model_reported_score=score,
        should_revise=False,
        test_case_index=idx,
    ).finalize()


def _hook(scores: list[float]) -> backend.CallHook:
    """按调用次序给分；分数用尽还来调用 → StopIteration，让"多打了一次"当场可见。"""
    it = iter(scores)

    def structured(role, model_cls, system, user, max_retries=3, overrides=None):
        return _mk_ev(next(it)), {"model": "f", "channel": "fake", "attempts": 1, "latency_ms": 1}

    # disable_cache=False：这条用例要验的就是缓存，夹具默认是关的（见 tests/conftest 的
    # no_shared_disk_cache），所以缓存目录也要在本用例里显式指向 tmp。
    return backend.CallHook(structured=structured, plain=None, disable_cache=False)


def _run(sample: int, output: str = OUT) -> dict:
    return {"test_case_index": 0, "sample_index": sample, "test_input": "x", "output": output}


def _kw(prompt: str = "p") -> dict:
    return {
        "task": "t",
        "context": "",
        "prompt": prompt,
        "judges": ["evaluator"],
        "judge_spec": "judges=evaluator",
        "q_warns": [],
    }


@pytest.fixture
def live_cache(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """开缓存 + 把缓存目录封进 tmp_path + 重置单例（三条都要，少一条就打到运营 logs/）。"""
    cache_dir = tmp_path / "cache"
    monkeypatch.setenv("PM_EVAL_CACHE", "1")
    monkeypatch.setenv("PM_CACHE_DIR", str(cache_dir))
    monkeypatch.setattr(C, "_eval_cache", None)
    yield cache_dir
    monkeypatch.setattr(C, "_eval_cache", None)


# ---------------------------------------------------------------------------
# 1. 键本身：采样序号必须参与散列，且默认值不改动既有键位（续跑不失效）
# ---------------------------------------------------------------------------
def test_key_distinguishes_sample_index() -> None:
    base = C.key_for_eval("p", "i", "o", "judges=evaluator", extra="t")
    assert base != C.key_for_eval("p", "i", "o", "judges=evaluator", extra="t", sample_index=1), (
        "评估缓存键没带采样序号：同一用例的第 2 次采样会命中第 1 次的评估对象，"
        "极差按构造为 0，`--samples` 买不到噪声带（§三十·十三 实测 噪声=0.00/缓存命中 4）"
    )
    assert base == C.key_for_eval("p", "i", "o", "judges=evaluator", extra="t", sample_index=0)


# ---------------------------------------------------------------------------
# 2. 核心回归：同输出、不同采样 → 各评一次，极差不再是常数 0
# ---------------------------------------------------------------------------
def test_same_output_across_samples_is_evaluated_twice(live_cache: Path) -> None:
    runs = [_run(0), _run(1)]
    with backend.use(_hook([9.0, 5.0])):
        evs, errors, n_hit, n_calls = J._evaluate_runs(runs, **_kw())
    assert errors == []
    assert n_calls == 2, f"两次采样应各发一次评委调用，实发 {n_calls}"
    assert n_hit == 0, f"第 2 次采样命中了第 1 次的评估缓存（n_cache_hit={n_hit}）"
    assert len(evs) == 1, "同一用例仍折叠成一条"
    assert evs[0]["score_spread"] == 4.0, (
        f"极差是 {evs[0]['score_spread']}，期望 4.0——评委抖动被缓存抹平时这里恒为 0"
    )
    assert evs[0]["sample_scores"] == [9.0, 5.0], evs[0]["sample_scores"]


# ---------------------------------------------------------------------------
# 3. 反向护栏：断点续跑（同 sample_index）必须仍然命中，别把改动变成纯烧钱
# ---------------------------------------------------------------------------
def test_resume_same_sample_still_hits_cache(live_cache: Path) -> None:
    with backend.use(_hook([7.0])):
        ev1, calls1, hit1 = J._evaluate_one(_run(0), errors=[], **_kw())
        ev2, calls2, hit2 = J._evaluate_one(_run(0), errors=[], **_kw())
    assert (calls1, hit1) == (1, False), ev1
    assert (calls2, hit2) == (0, True), (
        "同序号同内容的第二次评估没命中缓存：断点续跑失效，改动只剩成本没有收益"
    )
    assert ev2["weighted_score"] == ev1["weighted_score"]


# ---------------------------------------------------------------------------
# 4. 换 prompt 仍不得串用（这次改动不许把 H1 那半边碰坏）
# ---------------------------------------------------------------------------
def test_different_prompt_never_shares_eval(live_cache: Path) -> None:
    with backend.use(_hook([6.0, 8.0])):
        J._evaluate_one(_run(0), errors=[], **_kw("p"))
        ev, calls, hit = J._evaluate_one(_run(0), errors=[], **_kw("p2"))
    assert (calls, hit) == (1, False), "换了被测提示词却命中旧评估"
    assert ev["weighted_score"] == 8.0
