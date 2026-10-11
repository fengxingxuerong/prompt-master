"""账本里只有"复现性行"时，calibrate 不许把整轮实测吃掉。

2026-10-10 实测：照报告的建议跑 `run.py calibrate --judge evaluator --repeat 3`
（11 条锚点 × 3 次 ≈ 44 次真实调用、55 分钟），全部打完后崩在

    pm/cli/calibrate.py:496  d_bias = round(float(analysis["bias"]) - float(prev["bias"]), 2)
    KeyError: 'bias'

结果 stdout 0 字节、账本一条没写——因为 `_save_entry`（第 397 行）排在
`_drift_vs_history`（第 395 行）**之后**：一个纯展示用的"与上一轮比"把 44 发实测全吞了。

根因不是"少写一个 if"。账本里那条 2026-09-29 的记录只有 `{judge, rep_range_mean, ts}`，
它是**复现性行**（评委与自己比），不是校准行（评委与人工分比），本来就没有 bias/mae。
而 `_comparable()` 只检查指纹字段在不在——"老记录缺指纹按可比处理"这个设计本身是对的
（否则漂移检测会被静默停掉），但它把这行当成了 bias/MAE 的基线。
**读侧早就认识这个形状**（`tests/test_d2_scaling.py:59` 明写"只写 `{judge, rep_range_mean, ts}`
的是 09-29 那条历史记录"），写侧的漂移比较从没处理过它。

判据（可查的事实，不是我的断言）：
- 账本只有复现性行 ⇒ 不许抛，且本次结果必须照样入账；
- 账本有真正的校准行 ⇒ 漂移读数照旧（别把功能修没）；
- 既不可比又没数字的行 ⇒ 也不许抛（`changed` 分支同样硬取 `bias`）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pm import calibration as calibrate_judge  # noqa: E402
from pm.cli import calibrate as cli_calibrate  # noqa: E402
from pm.cli.calibrate import _cohort_of, _drift_vs_history  # noqa: E402

from test_cli_agent import _patch_calib  # noqa: E402
from test_cli_units import _patch_log_dir  # noqa: E402

# 真账本里那条 09-29 记录的原样形状（复现性行，无 bias/mae）
REP_ROW: dict[str, Any] = {"judge": "evaluator", "rep_range_mean": 0.54, "ts": "2026-09-29"}

ANALYSIS: dict[str, Any] = {"n": 11, "bias": 3.09, "mae": 3.13, "r": 0.4}
COHORT = _cohort_of("evaluator", "impression", "deepseek-v4-flash", "rub", "anch", 11)


def _seed_ledger(tmp_path: Path, rows: list[dict[str, Any]]) -> None:
    (tmp_path / "judge_calibration_history.json").write_text(
        json.dumps(rows, ensure_ascii=False), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# 1. 事故复现：入口级，跑完必须把实测留在账本里
# ---------------------------------------------------------------------------
def test_rep_row_only_ledger_still_saves_the_measurement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """只有复现性行时：不许抛 KeyError，且本轮结果必须入账（展示不许吃掉实测）。"""
    _patch_log_dir(monkeypatch, tmp_path)
    _seed_ledger(tmp_path, [dict(REP_ROW)])
    _patch_calib(monkeypatch, dict(ANALYSIS))
    monkeypatch.setattr(
        calibrate_judge,
        "repeatability",
        lambda samples, judge, times, mode="impression": {
            "role": judge,
            "times": times,
            "n_items": 11,
            "range_mean": 0.61,
            "range_max": 1.2,
            "worst_id": "s3",
            "disagreement_threshold": 2.0,
            "threshold_exceeded": False,
            "per_item": [],
        },
    )

    rc = cli_calibrate._calibrate_command(["--json", "--repeat", "3"])
    out = capsys.readouterr().out
    assert rc == 0, f"calibrate 因为一条展示用的比较崩掉（rc={rc}）：{out[-400:]}"
    payload = json.loads(out)
    assert payload["analysis"]["repeatability"]["range_mean"] == 0.61

    saved = json.loads((tmp_path / "judge_calibration_history.json").read_text(encoding="utf-8"))
    assert len(saved) == 2, f"实测没入账（账本仍是 {len(saved)} 条）——44 发调用白烧"
    assert saved[0] == REP_ROW, "老记录不许被动到"
    assert saved[-1]["rep_range_max"] == 1.2 and saved[-1]["rep_times"] == 3


# ---------------------------------------------------------------------------
# 2. 单测：复现性行不能当 bias/MAE 的基线，也不许抛
# ---------------------------------------------------------------------------
def test_rep_row_is_not_a_bias_baseline() -> None:
    assert _drift_vs_history([dict(REP_ROW)], COHORT, dict(ANALYSIS)) is None


def test_incomparable_row_without_numbers_does_not_raise() -> None:
    """`changed` 分支同样硬取 bias：尺子变了 + 那把尺子没数字 ⇒ 只能没有基线。"""
    row = dict(REP_ROW, model="glm-5.2", rubric="other", anchors="other", n=11)
    assert _drift_vs_history([row], COHORT, dict(ANALYSIS)) is None


# ---------------------------------------------------------------------------
# 3. 反向护栏：真有校准行时，漂移读数照旧（别把功能修没）
# ---------------------------------------------------------------------------
def test_real_calibration_row_still_yields_drift() -> None:
    prev = {
        "judge": "evaluator",
        "mode": "impression",
        "ts": "2026-10-05 20:00:00",
        "bias": 2.0,
        "mae": 2.5,
        "model": "deepseek-v4-flash",
        "rubric": "rub",
        "anchors": "anch",
        "n": 11,
    }
    drift = _drift_vs_history([prev], COHORT, dict(ANALYSIS))
    assert drift is not None, "同口径的校准行必须仍被当作基线，否则漂移检测被静默停掉"
    assert drift["prev_bias"] == 2.0
    assert drift["delta_bias"] == pytest.approx(1.09)
    assert drift["delta_mae"] == pytest.approx(0.63)
    assert drift["drifted"] is True


def test_mixed_ledger_prefers_the_real_calibration_row() -> None:
    """复现性行插在中间时，基线要落在最近那条**有数字**的校准行上。"""
    prev = {
        "judge": "evaluator",
        "mode": "impression",
        "ts": "2026-10-05 20:00:00",
        "bias": 1.0,
        "mae": 1.5,
        "model": "deepseek-v4-flash",
        "rubric": "rub",
        "anchors": "anch",
        "n": 11,
    }
    drift = _drift_vs_history([prev, dict(REP_ROW)], COHORT, dict(ANALYSIS))
    assert drift is not None and drift["prev_bias"] == 1.0
