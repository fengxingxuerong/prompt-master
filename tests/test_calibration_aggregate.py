"""跨轮聚合（aggregate_history）的回归测试：账本重算、零 LLM。

被测的承诺（docs/evaluation.md §十四）：
- 合并读数是把各轮逐锚点配对**并起来重算**，不是把各轮聚合数再平均——
  轮级 MAE 的平均在 n 不同时没有含义，逐锚点合并才有。
- CI 是**按锚点聚类**的重抽不确定度（同一锚点的多轮读数一起走），
  不是按配对独立重抽——后者会把锚点间方差撕碎，CI 系统性偏窄。
- 「无明细可聚合」返回 None 而不是报错：旧账本只有轮级数，没有不是事故。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pm.calibration import aggregate_history, render_aggregate
from pm.schemas import PASS_THRESHOLD


def _rec(
    ts: str,
    judge: str = "evaluator",
    mode: str | None = "impression",
    items: list[dict[str, Any]] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    rec: dict[str, Any] = {"ts": ts, "judge": judge, "n": len(items or []), "items": items or []}
    if mode is not None:  # mode=None 模拟 mode 字段引入之前的旧记录（默认 impression）
        rec["mode"] = mode
    rec.update(extra)
    return rec


def _it(sid: str, human: float, judge: float) -> dict[str, Any]:
    return {
        "id": sid,
        "human": human,
        "judge": judge,
        "delta": round(judge - human, 2),
        "flag": False,
    }


# 两轮已知解：轮1 Δ = [+1, -1]（MAE 1.0）；轮2 Δ = [+1, -1, 0]（MAE 2/3）。
# 合并 5 对：MAE = 4/5 = 0.8，bias = 0。判定线两侧人工/评委各 2/3 → 全一致。
# 不带 model 字段：直接调 aggregate_history 的用例都不传 model（不过滤），
# 需要分代的 CLI/报告路径在各自测试里于运行时补 model/rubric（与解析同源）。
_TWO_ROUNDS = [
    _rec("2026-09-24 10:00:00", items=[_it("a", 8.0, 9.0), _it("b", 6.0, 5.0)], mae=1.0, bias=0.0),
    _rec(
        "2026-09-25 10:00:00",
        items=[_it("a", 8.0, 9.0), _it("b", 6.0, 5.0), _it("c", 7.0, 7.0)],
        mae=0.67,
        bias=0.0,
    ),
]


def test_pooled_math_is_recomputed_from_pairs_not_averaged() -> None:
    agg = aggregate_history(_TWO_ROUNDS, mode="impression", judge="evaluator", bootstrap=0)
    assert agg is not None
    pooled = agg["pooled"]
    # 0.8 = (1+1+1+1+0)/5：若错误地平均两轮的轮级 MAE 会得到 (1.0+0.67)/2 = 0.835
    assert pooled["mae"] == 0.8
    assert pooled["bias"] == 0.0
    assert pooled["n_pairs"] == 5
    assert pooled["n_anchors"] == 3
    assert pooled["n_rounds"] == 2
    dec = pooled["decision"]
    assert dec["usable"] is True
    assert dec["agree"] == 1.0  # 人工 [8,6,8,6,7] 与评委 [9,5,9,5,7] 过线判定完全一致
    assert dec["kappa"] == 1.0


def test_filtering_by_mode_judge_and_item_presence() -> None:
    history = [
        *_TWO_ROUNDS,
        _rec("2026-09-23 10:00:00", judge="evaluator_b", items=[_it("a", 8.0, 9.0)]),  # 别的评委
        _rec("2026-09-23 11:00:00", mode="checklist", items=[_it("a", 8.0, 9.0)]),  # 别的协议
        _rec("2026-09-22 10:00:00"),  # 旧账本：没有 items，重算不出分布
        _rec(
            "2026-09-21 10:00:00", mode=None, items=[_it("d", 5.0, 4.0)]
        ),  # 无 mode 字段 = impression
    ]
    agg = aggregate_history(history, mode="impression", judge="evaluator", bootstrap=0)
    assert agg is not None
    assert agg["rounds_used"] == 3  # 两轮 + 一条 mode 缺省的旧记录
    assert agg["pooled"]["n_pairs"] == 6
    # 保持账本的追加序（追加序=时间序），不做 ts 字符串排序
    assert agg["rounds"][0]["ts"] == "2026-09-24 10:00:00"
    assert agg["rounds"][-1]["ts"] == "2026-09-21 10:00:00"


def test_ci_contains_point_estimate_and_skips_when_degenerate() -> None:
    agg = aggregate_history(_TWO_ROUNDS, mode="impression", judge="evaluator")
    assert agg is not None
    ci = agg["pooled"]["mae_ci95"]
    assert ci[0] <= agg["pooled"]["mae"] <= ci[1]
    assert ci[0] < ci[1]

    no_boot = aggregate_history(_TWO_ROUNDS, mode="impression", judge="evaluator", bootstrap=0)
    assert no_boot is not None
    assert "mae_ci95" not in no_boot["pooled"]

    one_anchor = [
        _rec("t1", items=[_it("a", 8.0, 9.0)]),
        _rec("t2", items=[_it("a", 8.0, 8.5)]),
    ]
    degenerate = aggregate_history(one_anchor, mode="impression", judge="evaluator")
    assert degenerate is not None
    assert "mae_ci95" not in degenerate["pooled"]
    assert "退化" in degenerate["pooled"]["ci_skipped"]


def test_dispersion_rep_crossings_and_papers_differ() -> None:
    history = [
        _rec(
            "t1",
            items=[_it("a", 8.0, 9.0), _it("b", 6.0, 5.0)],
            mae=1.0,
            bias=0.5,
            decision_agree=0.5,
            decision_kappa=0.0,
            rep_range_max=2.5,
            rep_threshold=2.0,
        ),
        _rec(
            "t2",
            items=[_it("a", 8.0, 8.0), _it("b", 6.0, 6.0)],
            mae=0.0,
            bias=0.0,
            decision_agree=1.0,
            decision_kappa=1.0,
            rep_range_max=1.0,
            rep_threshold=2.0,
        ),
        _rec("t3", items=[_it("a", 8.0, 8.0), _it("b", 6.0, 6.0), _it("c", 7.0, 7.0)], mae=0.0),
    ]
    agg = aggregate_history(history, mode="impression", judge="evaluator", bootstrap=0)
    assert agg is not None
    mae_span = agg["dispersion"]["mae"]
    # t1/t2/t3 都有 mae → 跨度覆盖 3 轮（t1 1.0、t2/t3 0.0）
    assert mae_span == {"min": 0.0, "max": 1.0, "range": 1.0, "n_rounds": 3}
    assert agg["rep_threshold_crossings"] == 1
    assert agg["n_rep_rounds"] == 2
    assert agg["papers_differ"] is True  # t3 的 n=3，考卷换过


def test_none_cases_and_last_trimming() -> None:
    assert aggregate_history([], mode="impression", judge="evaluator") is None
    assert aggregate_history([_rec("t1")], mode="impression", judge="evaluator") is None  # 无 items
    assert aggregate_history(_TWO_ROUNDS, mode="checklist", judge="evaluator") is None
    agg = aggregate_history(_TWO_ROUNDS, mode="impression", judge="evaluator", last=1, bootstrap=0)
    assert agg is not None
    assert agg["rounds_used"] == 1
    assert agg["pooled"]["n_pairs"] == 3  # 只剩最近一轮


def test_render_aggregate_numbers_and_honesty_lines() -> None:
    agg = aggregate_history(
        [dict(r, rep_range_max=2.5, rep_threshold=2.0) for r in _TWO_ROUNDS],
        mode="impression",
        judge="evaluator",
    )
    text = render_aggregate(agg)
    assert text != ""
    assert "跨轮聚合" in text
    assert "MAE **0.8**" in text
    assert "95% CI" in text
    assert "轮间极差" in text  # 两轮 → 有轮间信息
    assert "复现极差越线" in text
    assert "怎么读" in text
    assert render_aggregate(None) == ""


def test_render_single_round_hides_dispersion_line() -> None:
    agg = aggregate_history(_TWO_ROUNDS, last=1, mode="impression", judge="evaluator")
    text = render_aggregate(agg)
    assert "轮间极差（" not in text  # 单轮没有"轮间"可言，极差 0.0 是噪声不是信息
    assert "95% CI" in text


def test_cli_aggregate_flag_reads_ledger_offline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--aggregate 是零调用的离线入口：不装 Key、不碰样本文件也能把账本读明白。"""
    from pm.cli.calibrate import _calibrate_command
    from pm.llm import build_config

    # 轮记录的 model/rubric 必须在**测试运行时**与 --aggregate 的解析同源（不能在
    # 模块导入时求值——conftest 密封 env 发生在导入之后，两个时点的解析可以不同）。
    from pm.prompts import rubric_stamp

    cur_model = build_config("evaluator").model
    cur_rubric = rubric_stamp()
    rounds = [dict(r, model=cur_model, rubric=cur_rubric) for r in _TWO_ROUNDS]
    ledger = tmp_path / "judge_calibration_history.json"
    ledger.write_text(json.dumps(rounds, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr("pm.cli.calibrate._calib_history", lambda: ledger)

    rc = _calibrate_command(["--aggregate", "--scoring-mode", "impression", "--json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["aggregate"]["pooled"]["mae"] == 0.8
    assert out["aggregate"]["rounds_used"] == 2


def test_cli_aggregate_json_error_path_is_single_json_object(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """空账本也必须守「--json 时 stdout 恰好一个 JSON 对象」的约定。

    错误路径走 stderr 的话，MCP 的 _run_cli_json 会在 json.loads("") 上炸出
    与真实原因无关的错——消费方（智能体）拿到 {"error": ...} 才能自己判断下一步。
    """
    from pm.cli.calibrate import _calibrate_command

    ledger = tmp_path / "judge_calibration_history.json"
    ledger.write_text("[]", encoding="utf-8")
    monkeypatch.setattr("pm.cli.calibrate._calib_history", lambda: ledger)

    rc = _calibrate_command(["--aggregate", "--json"])
    assert rc == 2
    out = json.loads(capsys.readouterr().out)
    assert "error" in out and "可聚合" in out["error"]


def test_cli_full_flow_appends_aggregate_after_saving(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """完整校准流程：本次入账后，聚合必须把本次也算进去（账本先行，聚合随后）。"""
    from pm import calibration as calib
    from pm.cli import calibrate as calib_cli
    from pm.cli.calibrate import _calibrate_command

    fake_samples = [
        {
            "id": "a",
            "human_score": 8.0,
            "original_task": "t",
            "prompt": "p",
            "test_input": "i",
            "test_output": "o",
        },
        {
            "id": "b",
            "human_score": 6.0,
            "original_task": "t",
            "prompt": "p",
            "test_input": "i",
            "test_output": "o",
        },
    ]
    fake_analysis: dict[str, Any] = {
        "n": 2,
        "bias": 0.0,
        "mae": 1.0,
        "r": 1.0,
        "rho": 1.0,
        "pairs": [_it("a", 8.0, 9.0), _it("b", 6.0, 5.0)],
        "flags": [],
        "decision": {
            "n": 2,
            "line": PASS_THRESHOLD,
            "agree": 1.0,
            "kappa": 1.0,
            "usable": True,
            "n_lenient": 0,
            "n_strict": 0,
            "n_human_pass": 1,
            "n_judge_pass": 1,
        },
        "human_band_hist": {},
        "bands_covered": 2,
        "n_failed": 0,
        "failed": [],
    }
    ledger = tmp_path / "history.json"
    monkeypatch.setattr(calib, "load_samples", lambda _path: fake_samples)
    monkeypatch.setattr(calib, "calibrate", lambda _s, _r, _m="impression": (fake_analysis, []))
    monkeypatch.setattr(calib_cli, "_calib_history", lambda: ledger)

    rc = _calibrate_command(["--samples", "unused.json", "--scoring-mode", "impression", "--json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["saved"] is True
    agg = out["aggregate"]
    assert agg is not None
    assert agg["rounds_used"] == 1  # 账本里只有刚入账的这条
    assert agg["pooled"]["n_pairs"] == 2
    assert agg["pooled"]["mae"] == 1.0
    # 账本确实写了，且逐条明细在——聚合的原料就是它
    saved = json.loads(ledger.read_text(encoding="utf-8"))
    assert len(saved) == 1 and len(saved[0]["items"]) == 2

    rc2 = _calibrate_command(["--samples", "unused.json", "--scoring-mode", "impression"])
    assert rc2 == 0
    assert "跨轮聚合" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# 模型纪元过滤（2026-10-04 evaluator 换型落地）：聚合按评委模型分代
# ---------------------------------------------------------------------------


def test_aggregate_filters_by_judge_model_for_epoch_switching() -> None:
    """换型纪元：flash-lite 旧尺与 deepseek 新尺不能混进同一把聚合读数。

    bias +3.09 的旧仪表轮次混入新仪表轮次，披露行刻画的就是「两把尺的平均」
    而不是当前仪表——聚合必须能按账本轮记录自带的 model 字段分代。
    """
    flash = _rec("2026-09-27 19:38:25", model="sensenova-6.8-flash-lite")
    flash["items"] = [_it("a", 5.0, 8.0)]
    flash["model"] = "sensenova-6.8-flash-lite"
    flash2 = _rec("2026-09-29 18:51:36")
    flash2["items"] = [_it("b", 6.0, 9.0)]
    flash2["model"] = "sensenova-6.8-flash-lite"
    ds = _rec("2026-10-04 20:00:00")
    ds["items"] = [_it("c", 7.0, 7.1)]
    ds["model"] = "deepseek-v4-flash"

    out = aggregate_history([flash, flash2, ds], judge="evaluator", model="deepseek-v4-flash")
    assert out is not None
    assert out["rounds_used"] == 1
    assert out["pooled"]["n_pairs"] == 1
    assert abs(out["pooled"]["bias"] - 0.1) < 1e-9

    # 旧行为回归：不传 model 时三轮全合并（历史读数复现路径不受影响）
    legacy = aggregate_history([flash, flash2, ds], judge="evaluator")
    assert legacy is not None
    assert legacy["rounds_used"] == 3


def test_aggregate_model_none_matching_rounds_returns_none() -> None:
    """换型后第一次校准之前，新模型在账本里还没有轮次 ⇒ 返回 None（不报错）。"""
    flash = _rec("2026-09-27 19:38:25")
    flash["items"] = [_it("a", 5.0, 8.0)]
    flash["model"] = "sensenova-6.8-flash-lite"
    assert aggregate_history([flash], judge="evaluator", model="deepseek-v4-flash") is None


def test_aggregate_filters_by_rubric_epoch_same_model() -> None:
    """model 过滤还不够：同模型在旧 rubric 下的轮次也不是同一把尺。

    实测依据（2026-10-04 换型落地）：deepseek-v4-flash 在 2026-09-27 旧 rubric
    下 bias +4.2、在现行 rubric 下 +0.29——只按模型过滤会把两代评分标准混进
    一份 bias。模型 × rubric 二元组才是"当前仪表"的完整刻画。
    """
    old_rubric = _rec("2026-09-27 16:00:12", rubric="706313c656")
    old_rubric["items"] = [_it("a", 2.0, 6.2)]
    old_rubric["model"] = "deepseek-v4-flash"
    old_rubric2 = _rec("2026-09-27 17:07:20", rubric="2d638417ee")
    old_rubric2["items"] = [_it("b", 1.0, 5.1)]
    old_rubric2["model"] = "deepseek-v4-flash"
    current = _rec("2026-10-04 21:04:01", rubric="38802252ee")
    current["items"] = [_it("c", 7.0, 7.2)]
    current["model"] = "deepseek-v4-flash"

    out = aggregate_history(
        [old_rubric, old_rubric2, current],
        judge="evaluator",
        model="deepseek-v4-flash",
        rubric="38802252ee",
    )
    assert out is not None
    assert out["rounds_used"] == 1
    assert out["pooled"]["n_pairs"] == 1
    assert abs(out["pooled"]["bias"] - 0.2) < 1e-9

    # 锚点集不同但 rubric 相同的同代考卷照常合并（§十四 先例）：
    # rubric 过滤不要求 anchors 指纹一致。
    other_anchors = _rec("2026-10-05 10:00:00", rubric="38802252ee")
    other_anchors["items"] = [_it("d", 5.0, 5.5)]
    other_anchors["model"] = "deepseek-v4-flash"
    out2 = aggregate_history(
        [current, other_anchors],
        judge="evaluator",
        model="deepseek-v4-flash",
        rubric="38802252ee",
    )
    assert out2 is not None
    assert out2["rounds_used"] == 2
