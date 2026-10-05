"""d2 必须随采样次数走：`--repeat N` 里 N≠3 时换算全错（§二十三）。

## 缺陷（**先于本会话存在**）

账本里存的是**极差**（`rep_range_mean`），而合成方差要的是 sd。换算因子
d2 = 极差/σ 依赖采样次数：`d2(3)=1.693`、`d2(5)=2.326`、`d2(2)=1.128`。

代码里 `_RANGE_TO_SD = 1.693` **硬编码 n=3**，而 `calibrate --repeat N` 的 N
是用户自选的，账本也如实记了 `rep_times` —— 只是没人读它。后果：

| N | 线相对正确值 | 方向 |
|---|---|---|
| 2 | 1.50× | 偏高（过严，无害方向） |
| 3 | 1.00× | 正确 |
| 5 | **0.73×** | **偏低 = 门被放松（有害方向）** |

关键在 N>3 时偏松：**多花 5 倍的钱多测几次，反而让仲裁更容易触发** ——
校准动作与门禁方向相反，用户越认真校准门越松。这是"配置组合决定对错"那类
缺陷（同 §二十一 的单采样那一档）：默认 `--repeat 1` 不测，测的人才会踩。

实测（`.env` 那组真实值 A 0.54 / B 2.6，账本记 `rep_times=5`）：
修前线 3.14，正确值应为 2.28 —— 偏松 1.38 倍。

## 修法

账本读出 `rep_times`，按 k 选 d2；表格外（k>8）按 1.25k 渐近并记进未验项。
另外把"极差→sd"这一步收进 `judge_jitter()`，让下游不必再各自记得除一次。
"""

from __future__ import annotations

import json
import math

import pytest
from pm import schemas


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    for seat in ("PM_EVALUATOR_JITTER", "PM_EVALUATOR_B_JITTER", "PM_ARBITER_JITTER"):
        monkeypatch.delenv(seat, raising=False)
    monkeypatch.setenv("PM_JUDGE_DISAGREEMENT", "2.0")
    monkeypatch.setenv("PM_LOG_DIR", str(tmp_path))
    monkeypatch.setattr(schemas, "JUDGE_JITTER", 0.0)
    schemas._measured_jitter.cache_clear()


def _ledger(tmp_path, entries) -> None:
    (tmp_path / "judge_calibration_history.json").write_text(json.dumps(entries), encoding="utf-8")
    schemas._measured_jitter.cache_clear()


def _two_seats(k: int, a: float = 0.54, b: float = 2.6) -> list[dict]:
    """两条**形状与 `calibrate._save_entry` 一致**的账本条目。

    ⚠️ `model` 必须填（2026-10-05 §二十九）：账本按仪表身份过滤，缺 `model`
    的条目视为"无法证明是当前这把尺子量的"、不进噪声带。真实 `calibrate` 写的
    条目带这个字段；只写 `{judge, rep_range_mean, ts}` 的是 09-29 那条历史记录，
    而它正是本轮发现"校准说不算、噪声带照算"的那条。
    """
    model = schemas.current_judge_model("evaluator")
    return [
        {
            "judge": "evaluator",
            "rep_times": k,
            "rep_range_mean": a,
            "ts": "2026-10-05",
            "model": model,
        },
        {
            "judge": "evaluator_b",
            "rep_times": k,
            "rep_range_mean": b,
            "ts": "2026-10-05",
            "model": schemas.current_judge_model("evaluator_b"),
        },
    ]


def _entry(seat: str, rep_range_mean: float, rep_times=..., ts: str = "2026-10-05") -> dict:
    """一条**当前仪表**的账本条目（`model` 已填，见 `_two_seats` 的注记）。"""
    e = {"judge": seat, "rep_range_mean": rep_range_mean, "ts": ts}
    if rep_times is not ...:
        e["rep_times"] = rep_times
    e["model"] = schemas.current_judge_model(seat)
    return e


# ---------------------------------------------------------------------------
# 判据一：d2 随 k 走
# ---------------------------------------------------------------------------
def test_d2_table_matches_published_values() -> None:
    """表本身要先对：d2(n) = E[range]/σ（正态总体）。"""
    assert schemas._range_to_sd(2) == 1.128
    assert schemas._range_to_sd(3) == 1.693
    assert schemas._range_to_sd(5) == 2.326
    assert schemas._range_to_sd(8) == 2.847


def test_measured_sd_is_divided_by_its_own_k(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """同一个极差、k 不同 ⇒ 换算出的 sd 必须不同（钉在**门**上，不钉内部函数）。

    观测口选 `effective_disagreement_threshold()`：它是消费方，
    内部 helper 改名/重构不该让这条红（那属于过度绑定实现）。
    """
    monkeypatch.setenv("PM_JUDGE_DISAGREEMENT", "0.1")
    _ledger(tmp_path, _two_seats(3))
    g3 = schemas.effective_disagreement_threshold()
    _ledger(tmp_path, _two_seats(5))
    g5 = schemas.effective_disagreement_threshold()
    assert g3 != g5, "硬编码 n=3 会让这两个读数一样 —— 那就是缺陷本身"
    assert g3 == pytest.approx(round(2.0 * math.hypot(0.54, 2.6) / 1.693, 2), abs=0.01)
    assert g5 == pytest.approx(round(2.0 * math.hypot(0.54, 2.6) / 2.326, 2), abs=0.01)


def test_gate_is_correct_for_k_five(tmp_path) -> None:
    """实测复现缺陷的那一组：k=5 时线必须落到 2.28 附近，不是 3.14。"""
    _ledger(tmp_path, _two_seats(5))
    gate = schemas.effective_disagreement_threshold()
    expected = round(2.0 * math.hypot(0.54 / 2.326, 2.6 / 2.326), 2)
    assert gate == expected, "k=5 必须用 d2(5)"
    assert gate < 3.14, "修前那个偏松的读数不许回来"


def test_gate_tightens_as_k_grows(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """核心方向判据：k 越大 ⇒ 同一个极差被换算成越小的 sd ⇒ 线越严。

    这条红了说明"多测几次反而更松"那个反直觉行为还在。

    `PM_JUDGE_DISAGREEMENT` 压到 0.1 让 `max(configured, …)` 不吃掉了读数 ——
    否则 k≥8 时算出来的 1.87/… 会被默认的 2.0 兜底，三档读数都变成 2.0，
    观察不到方向。（兜底本身是 §二十 要的"只许更严"，这里只是要测量它。）
    """
    monkeypatch.setenv("PM_JUDGE_DISAGREEMENT", "0.1")
    _ledger(tmp_path, _two_seats(3))
    g3 = schemas.effective_disagreement_threshold()
    _ledger(tmp_path, _two_seats(5))
    g5 = schemas.effective_disagreement_threshold()
    _ledger(tmp_path, _two_seats(8))
    g8 = schemas.effective_disagreement_threshold()
    assert g3 > g5 > g8, "采样次数越多，换算越严；现在是反的"


def test_k_two_is_not_over_corrected(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """k=2 的 d2 最小 ⇒ 换算后的 sd 最大 ⇒ 线最严（保守方向）。"""
    monkeypatch.setenv("PM_JUDGE_DISAGREEMENT", "0.1")
    _ledger(tmp_path, _two_seats(2))
    g2 = schemas.effective_disagreement_threshold()
    _ledger(tmp_path, _two_seats(3))
    g3 = schemas.effective_disagreement_threshold()
    assert g2 > g3


# ---------------------------------------------------------------------------
# 判据二：缺 `rep_times` 时不许瞎猜，回落 n=3
# ---------------------------------------------------------------------------
def test_missing_repeat_times_falls_back_to_three(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """账本里没有 `rep_times`（如现存那唯一一条的形状）时按 n=3 算。

    ⚠️ 2026-10-05 §二十九 起这条只对**当前仪表**的条目成立：现存那条 09-29 记录
    因为没有 `model`，现在会被仪表身份过滤挡在噪声带之外（那是刻意的）。
    """
    monkeypatch.setenv("PM_JUDGE_DISAGREEMENT", "0.1")
    _ledger(tmp_path, [_entry("evaluator", 0.54, ts="2026-09-29")])
    got = schemas.effective_disagreement_threshold()
    assert got == pytest.approx(round(2.0 * (0.54 / 1.693), 2), abs=0.01)


def test_absurd_repeat_times_is_ignored(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """`rep_times=1` / 0 / 负数 / 非数 一律按 n=3，不许拿它换算。"""
    monkeypatch.setenv("PM_JUDGE_DISAGREEMENT", "0.1")
    want = pytest.approx(round(2.0 * (0.54 / 1.693), 2), abs=0.01)
    for bad in (1, 0, -5, None, "many"):
        _ledger(tmp_path, [_entry("evaluator", 0.54, rep_times=bad, ts="x")])
        assert schemas.effective_disagreement_threshold() == want


def test_beyond_table_uses_the_asymptote(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """k>8 走外推；仍须单调不降（外推错了这条会红）。"""
    monkeypatch.setenv("PM_JUDGE_DISAGREEMENT", "0.1")
    _ledger(tmp_path, _two_seats(8))
    g8 = schemas.effective_disagreement_threshold()
    _ledger(tmp_path, _two_seats(12))
    g12 = schemas.effective_disagreement_threshold()
    assert g8 > g12, "外推区也要随 k 变严"


# ---------------------------------------------------------------------------
# 判据三：§二十 的判据（两座位、只许更严）不许被本轮破坏
# ---------------------------------------------------------------------------
def test_arbiter_still_stays_out_of_the_line(tmp_path) -> None:
    _ledger(tmp_path, [*_two_seats(5), _entry("arbiter", 4.43, rep_times=5)])
    gate = schemas.effective_disagreement_threshold()
    assert gate == round(2.0 * math.hypot(0.54 / 2.326, 2.6 / 2.326), 2)


def test_explicit_config_still_wins_over_the_ledger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`.env` 是一手：配了就用配的。⚠️ 但它同样是**极差**，照样要除 d2(3)。

    这条是本轮真实踩过的坑：一度把配置值当成 sd 直接用，线偏松 1.69 倍，
    `run.py --selftest` 的"双评委分歧触发了仲裁"当场变红。
    """
    monkeypatch.setenv("PM_EVALUATOR_JITTER", "0.7")
    monkeypatch.setenv("PM_EVALUATOR_B_JITTER", "2.6")
    gate = schemas.effective_disagreement_threshold()
    assert gate == round(2.0 * math.hypot(0.7 / 1.693, 2.6 / 1.693), 2), "配置值是极差，要换算"
    assert gate == 3.18, "与 2026-09-20 实测同口径"
