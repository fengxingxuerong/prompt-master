"""评委抖动必须带出处：本轮没实测过的数不该无条件进噪声带与达标判定。

背景（`docs/evaluation.md` §十八，2026-10-05 零调用归因审计）：
`PM_JUDGE_JITTER=2.6` 冻结在 `.env` 里，此后没有被任何一轮校准重新验证过，而它
是合成噪声带的主项——11 轮真端点共 22 个臂逐臂核对 `noise_total == hypot(noise,
judge_jitter)` 全部成立，剥掉这个常数后真实采样噪声带从 2.891 掉到 1.138，
**60.6% 来自配置常数**。连带后果：C2 判据（`Δ > 本轮自报带`）在很大程度上
测的是这个配置值而不是产品差异，剥掉后 7 轮里 3 轮翻转；同一个常数还经
`ci_lower` 进了达标判定，22 个臂里有 3 个的 `ci_lower` 闸因此翻转。

本模块要立的规则（§十八·七 的建议 2）：
**"实测过的抖动"与"配置里写着的一个数"必须在代码里可区分，且只有前者能进
`noise_total` / `ci_lower` / 平台期余量。**

三条判据：
1. 显式配了数（`PM_JUDGE_JITTER` 或 `PM_<SEAT>_JITTER`）但账本里查不到对应的实测记录
   → 该数进不了带，但**必须被留痕**（报告里说破"这是未验证配置值"）。
2. 账本里有过同 seat 的实测记录 → 允许显式配置覆盖，且默认值优先取**实测值**。
3. 一切失败都必须退化成"没测过"（0），绝不能让带因读文件失败而静默变宽或变窄。

⚠️ 这些判据与 §十八 的读数一起构成完整口径：引用噪声带时必须连同
`jitter_provenance` 一起引用，否则读者会把"配置常数"当成"实测精度"。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pm import schemas


def _ledger(entries: list[dict], log_dir: Path) -> Path:
    p = log_dir / "judge_calibration_history.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(_stamp(entries), ensure_ascii=False), encoding="utf-8")
    return p


def _stamp(entries: list[dict]) -> list[dict]:
    """给缺 `model` 的条目补上当前评委模型（§二十九 仪表身份过滤）。

    真实 `calibrate._save_entry` 写的条目带 `model`；缺它会被判为"不是当前这把
    尺子量的"、不进噪声带。测试造条目时不必手写——要测那条过滤语义请显式写
    `{"model": "别的型号"}`。
    """
    out = []
    for e in entries:
        # 故意混入的非 dict 元素原样保留 —— 本用例正是要验"它们被跳过而非整份作废"，
        # 这里若把它们规整掉，验的就不是同一条路径了。
        if not isinstance(e, dict):
            out.append(e)
            continue
        e = dict(e)
        e.setdefault("model", schemas.current_judge_model(e.get("judge") or "evaluator"))
        out.append(e)
    return out


@pytest.fixture
def logdir(monkeypatch, tmp_path):
    """把产物目录指到 tmp：账本是运行期产物，测试绝不许写进仓库的 logs/。

    同时把 `_measured_jitter` 的缓存清掉——它按路径 memo，而 PM_LOG_DIR 在不同
    用例里指向不同目录；不清就会拿到上一个用例的读数（这正是本文件第一版的红因）。
    """
    schemas._measured_jitter.cache_clear()
    monkeypatch.setenv("PM_LOG_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    """每个用例都从"环境变量干净"出发。宿主机 `.env` 的 PM_JUDGE_JITTER=2.6
    会经 pm.llm 的 load_dotenv 灌进进程，不清掉的话本模块测的是宿主机配置。"""
    for seat in ("PM_JUDGE_JITTER", "PM_EVALUATOR_JITTER", "PM_EVALUATOR_B_JITTER"):
        monkeypatch.delenv(seat, raising=False)
    # JUDGE_JITTER 是 import 期冻结的常量（与 test_measurement.py 同一口径），
    # 改它只能 setattr，改环境变量对它无效。
    monkeypatch.setattr(schemas, "JUDGE_JITTER", 0.0)


# ---------------------------------------------------------------------------
# 判据 1：未验证的配置常数不进带
# ---------------------------------------------------------------------------
def test_config_only_jitter_is_excluded_from_the_band(logdir, monkeypatch):
    """只有 `.env` 写了个数、账本里查不到实测 → 不进带。"""
    monkeypatch.setattr(schemas, "JUDGE_JITTER", 2.6)
    prov = schemas.jitter_provenance()
    assert prov.jitter == 0.0, "未验证的配置常数不得进带"
    assert prov.source == "unverified_config"
    assert schemas.measurement_noise(1.0) == 1.0, "带必须等于纯采样噪声"
    assert schemas.measurement_noise(None) is None, "噪声不可估的语义不许被改写"


def test_provenance_records_the_rejected_value(logdir, monkeypatch):
    """排除它不等于装作没这回事：报告必须能说破"你配了个数、我没敢用"。"""
    monkeypatch.setattr(schemas, "JUDGE_JITTER", 2.6)
    prov = schemas.jitter_provenance()
    assert prov.configured == 2.6, "配过的原值要留痕，否则报告无法披露"
    assert prov.jitter == 0.0
    assert prov.measured is False


def test_ci_lower_ignores_unverified_jitter(logdir, monkeypatch):
    """§十八·五 的连带项：那个常数还经 ci_lower 进了达标判定。"""
    from pm.schemas import AggregateScore

    evals = [_ev(8.0, spread=0.4), _ev(8.4, spread=0.6), _ev(7.6, spread=0.2)]
    monkeypatch.setattr(schemas, "JUDGE_JITTER", 2.6)
    agg = AggregateScore.from_evaluations(evals, n_expected=3)
    assert agg.judge_jitter == 0.0
    assert agg.noise_total == agg.noise
    assert agg.jitter_provenance is not None


# ---------------------------------------------------------------------------
# 判据 2：实测值可用，且优先于配置常数
# ---------------------------------------------------------------------------
def test_measured_jitter_from_ledger_is_used(logdir):
    _ledger([{"judge": "evaluator", "rep_range_mean": 0.54, "ts": "2026-09-29"}], logdir)
    prov = schemas.jitter_provenance()
    assert prov.jitter == 0.54
    assert prov.source == "measured"
    assert prov.measured is True


def test_explicit_config_wins_over_measured_but_is_flagged(logdir, monkeypatch):
    """显式配置仍然优先（保持 `.env` 可控），但必须标成 configured 并留痕。"""
    _ledger([{"judge": "evaluator", "rep_range_mean": 0.54, "ts": "2026-09-29"}], logdir)
    monkeypatch.setenv("PM_EVALUATOR_JITTER", "1.2")
    prov = schemas.jitter_provenance("evaluator")
    assert prov.jitter == 1.2
    assert prov.source == "measured_override"
    assert prov.configured == 1.2


def test_seat_jitter_uses_that_seats_own_measurement(logdir):
    """三把评委复现性差着数量级（实测 A 0.475 / B 1.9 / 仲裁 2.5），
    每把椅子必须读自己那把的实测，不许由最抖的替所有人定。"""
    _ledger(
        [
            {"judge": "evaluator", "rep_range_mean": 0.45, "ts": "2026-09-25"},
            {"judge": "evaluator_b", "rep_range_mean": 1.9, "ts": "2026-09-25"},
        ],
        logdir,
    )
    assert schemas.jitter_provenance("evaluator").jitter == 0.45
    assert schemas.jitter_provenance("evaluator_b").jitter == 1.9
    assert schemas.jitter_provenance("arbiter").jitter == 0.0, "没测过的椅子不给数"


# ---------------------------------------------------------------------------
# 判据 3：任何读盘失败都必须退化成"没测过"，而不是别的
# ---------------------------------------------------------------------------
def test_corrupt_ledger_degrades_to_unmeasured(logdir, monkeypatch):
    """账本损坏时绝不能崩，也不能悄悄拿一个数当实测。"""
    (logdir / "judge_calibration_history.json").write_text("{ not json", encoding="utf-8")
    monkeypatch.setattr(schemas, "JUDGE_JITTER", 2.6)
    prov = schemas.jitter_provenance()
    assert prov.jitter == 0.0
    assert prov.source in {"unverified_config", "unmeasured"}


def test_ledger_shape_junk_is_skipped_not_fatal(logdir):
    """账本被手改成 dict 或混进非 dict 元素时，跳过它们而不是整份作废。

    真实触发路径不止"文件损坏"：账本是 append 的 JSON 数组，人工补一条时很容易
    写出 `[..., "备注", {...}]`。那一条不该让其余实测记录一起失效。
    """
    (logdir / "judge_calibration_history.json").write_text(
        json.dumps(
            _stamp(["备注", {"judge": "evaluator", "rep_range_mean": 0.54, "ts": "2026-10-05"}])
        ),
        encoding="utf-8",
    )
    assert schemas.jitter_provenance("evaluator").jitter == 0.54

    (logdir / "judge_calibration_history.json").write_text(json.dumps({"a": 1}), encoding="utf-8")
    schemas._measured_jitter.cache_clear()
    assert schemas.jitter_provenance("evaluator").jitter == 0.0, "非 list 的账本退化成没测过"


def test_ledger_entries_without_repeatability_are_not_measurements(logdir):
    """MAE/bias 那一坨聚合数**不是**复现性实测——它们与极差无关。"""
    _ledger([{"judge": "evaluator", "mae": 1.04, "bias": 0.29, "n": 10}], logdir)
    assert schemas.jitter_provenance("evaluator").jitter == 0.0


def test_most_recent_measurement_wins(logdir):
    _ledger(
        [
            {"judge": "evaluator", "rep_range_mean": 0.45, "ts": "2026-09-24 23:48:28"},
            {"judge": "evaluator", "rep_range_mean": 0.54, "ts": "2026-09-29 20:22:51"},
            {"judge": "evaluator", "rep_range_mean": 0.11, "ts": "2026-09-29 22:20:45"},
        ],
        logdir,
    )
    assert schemas.jitter_provenance("evaluator").jitter == 0.11


# ---------------------------------------------------------------------------
# 接进真实判定链：噪声带与 ci_lower 必须一起动
# ---------------------------------------------------------------------------
def test_noise_band_and_ci_lower_both_follow_provenance(logdir):
    from pm.schemas import AggregateScore

    evals = [_ev(8.0, spread=0.4), _ev(8.4, spread=0.6), _ev(7.6, spread=0.2)]

    _ledger([{"judge": "evaluator", "rep_range_mean": 0.54, "ts": "2026-09-29"}], logdir)
    measured = AggregateScore.from_evaluations(evals, n_expected=3)

    (logdir / "judge_calibration_history.json").unlink()
    schemas._measured_jitter.cache_clear()
    unmeasured = AggregateScore.from_evaluations(evals, n_expected=3)

    assert measured.judge_jitter == 0.54
    assert unmeasured.judge_jitter == 0.0
    assert measured.noise_total > measured.noise, "实测抖动进带"
    assert unmeasured.noise_total == unmeasured.noise, "未测抖动时不引入任何新数值"
    assert measured.ci_lower < unmeasured.ci_lower, "带宽了，下界就必须更保守"


def _ev(score: float, spread: float = 0.0):
    from pm.schemas import EvaluationResult

    return EvaluationResult(
        dimension_scores={
            "task_completion": score,
            "format_adherence": score,
            "constraint_compliance": score,
            "robustness": score,
            "quality": score,
        },
        model_reported_score=score,
        issues=[],
        suggestions=[],
        should_revise=False,
        judge="evaluator",
        weighted_score=score,
        passed=score >= 8.0,
        score_spread=spread,
    )
