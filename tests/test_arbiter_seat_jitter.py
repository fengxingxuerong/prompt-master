"""仲裁触发线只由**两位投票评委**决定，仲裁者自己的抖动不进这条线。

背景：`docs/evaluation.md` §十八·六 列过一个"未审项"——`PM_ARBITER_JITTER`
（实测极差 4.43，三把评委里最抖）在触发线的链路上有没有入口？
初版答案是"没有"，于是本轮做了一次实现 + 复核，**结论是初版答案才对**。

这一节把当时的判断过程钉成回归测试，重点钉住**为什么不该加**：

1. 这条线量的是两位投票评委的分歧；仲裁发生在分歧**之后**，
   把第三把的抖动并进来是拿后一件东西解释前一件。
2. 合成量纲不对：仲裁极差 4.43 是一次**仲裁**的波动，A/B 是各自**一次打分**的波动，
   三个数不在同一个"差"里。
3. 最狠的一条是可观测后果：`√(0.7²+2.6²+4.43²)/1.693` 会把线抬到 **6.12**，
   而仲裁者自身实测极差是 4.43 —— 线抬到仲裁波动之上，等于**宣布这套评委测不出分歧**，
   恰好是这条线要防的反面。

所以：仲裁者的复现性仍然重要，但它的正确去处是"`仲裁结论有多可信`"的披露，
不是投票分歧的触发线（见 `pm/report.py` 的仲裁出处披露行）。
"""

from __future__ import annotations

import math

import pytest
from pm import schemas


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> None:
    for seat in ("PM_EVALUATOR_JITTER", "PM_EVALUATOR_B_JITTER", "PM_ARBITER_JITTER"):
        monkeypatch.delenv(seat, raising=False)
    monkeypatch.setenv("PM_JUDGE_DISAGREEMENT", "2.0")


def _two_seat_gate(a: float, b: float, base: float = 2.0) -> float:
    """独立复算触发线（不复用被测函数，避免自己验自己）。

    ⚠️ a / b 是**极差**：`.env.example` 明写 `PM_<SEAT>_JITTER` 是"同输入重复打分
    观察到的最大极差"，由 `calibrate --repeat` 量出，不是 sd。要除 d2(k) 才是 sd。
    漏掉这一步会让线偏松 1.69 倍（`--repeat 3` 时）—— 本轮踩过一次，
    是 `run.py --selftest` 抓出来的（见 §二十三·五）。
    """
    d2 = 1.693
    return max(base, round(2.0 * math.hypot(a / d2, b / d2), 2))


# ---------------------------------------------------------------------------
# 核心判据：仲裁者不参与合成
# ---------------------------------------------------------------------------
def test_arbiter_jitter_must_not_reach_the_disagreement_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """把仲裁者从 0 调到 4.43，触发线必须**纹丝不动**。

    这条红了就说明有人又把仲裁者并进了合成 —— 而那会把线抬到仲裁自身极差之上。
    """
    monkeypatch.setenv("PM_EVALUATOR_JITTER", "0.7")
    monkeypatch.setenv("PM_EVALUATOR_B_JITTER", "2.6")

    monkeypatch.setenv("PM_ARBITER_JITTER", "0")
    calm = schemas.effective_disagreement_threshold()

    monkeypatch.setenv("PM_ARBITER_JITTER", "4.43")
    loaded = schemas.effective_disagreement_threshold()

    assert calm == loaded, "仲裁者的抖动不得改变投票分歧的触发线"
    assert calm == _two_seat_gate(0.7, 2.6), "线由两位投票评委合成"


def test_the_naive_three_seat_form_would_break_the_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """钉住"为什么不该加"：三座位合成的线会高过仲裁者自身的实测极差。

    这不是抽象的洁癖 —— 线一旦高过仲裁波动，分歧永远触发不了仲裁，
    双评委交叉验证静默退化成"取平均"。
    """
    arbiter_range = 4.43  # 实测：仲裁一次 4.45、一次 8.88
    monkeypatch.setenv("PM_EVALUATOR_JITTER", "0.7")
    monkeypatch.setenv("PM_EVALUATOR_B_JITTER", "2.6")
    monkeypatch.setenv("PM_ARBITER_JITTER", str(arbiter_range))

    naive = round(2.0 * math.sqrt(0.7**2 + 2.6**2 + arbiter_range**2) / 1.693, 2)
    actual = schemas.effective_disagreement_threshold()

    assert naive > arbiter_range, "三座位合成的线会高过仲裁自身波动（本轮不采信的方案）"
    assert actual == _two_seat_gate(0.7, 2.6)
    assert actual < naive


def test_gate_matches_the_two_seat_formula(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PM_EVALUATOR_JITTER", "0.7")
    monkeypatch.setenv("PM_EVALUATOR_B_JITTER", "2.6")
    assert schemas.effective_disagreement_threshold() == _two_seat_gate(0.7, 2.6)


def test_all_silent_means_the_configured_threshold_verbatim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """两把都没数时逐字等于配置值 —— 默认路径零行为变化。"""
    monkeypatch.setenv("PM_JUDGE_DISAGREEMENT", "2.5")
    assert schemas.effective_disagreement_threshold() == 2.5


def test_the_gate_only_ever_tightens(monkeypatch: pytest.MonkeyPatch) -> None:
    """配置值高过抖动合成值时不许被压低（放松 = 制造假阳性）。"""
    monkeypatch.setenv("PM_EVALUATOR_JITTER", "0.3")
    monkeypatch.setenv("PM_EVALUATOR_B_JITTER", "0.3")
    monkeypatch.setenv("PM_JUDGE_DISAGREEMENT", "9.0")
    assert schemas.effective_disagreement_threshold() == 9.0


def test_arbiter_measurement_stays_available_for_disclosure(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """仲裁者的实测仍然读得到 —— 只是不参与这条线。

    它要去的地方是"仲裁结论有多可信"的披露（`pm/report.py`）；读不到的话
    那个披露就没有数据源。
    """
    import json

    (tmp_path / "judge_calibration_history.json").write_text(
        json.dumps(
            [
                {
                    "judge": "arbiter",
                    "rep_range_mean": 4.43,
                    "ts": "2026-09-25",
                    # §二十九：账本按仪表身份过滤，缺 model 不算当前这把尺子的实测
                    "model": schemas.current_judge_model("arbiter"),
                }
            ]
        ),
        encoding="utf-8",
    )
    schemas._measured_jitter.cache_clear()
    monkeypatch.delenv("PM_ARBITER_JITTER", raising=False)

    # 账本里是**极差**，`judge_jitter()` 原样返回（换算发生在消费方）
    assert schemas.judge_jitter("arbiter") == 4.43
    # 而触发线不受影响（两把投票评委都没数）
    monkeypatch.setenv("PM_JUDGE_DISAGREEMENT", "2.0")
    assert schemas.effective_disagreement_threshold() == 2.0
