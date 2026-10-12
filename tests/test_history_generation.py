"""`run.py history` 的**分代**口径：同一任务只有落在同一把尺子上的轮次才配做配对统计。

为什么单开一个文件：`test_history_mock_filter.py` 管"哪些臂算 demo / 算崩溃"（取样与准入），
`test_history_task_stats.py` 管"准入之后"的统计与渲染，这里管第三件、也是最新补上的一件
——**尺子换了代，Δ 就不能放同一个均值里**。

背景（§三十一 / §十八）：`calibrate --aggregate` 早已按 `model×rubric` 分代，因为
"bias 是这把尺子相对人工分的系统偏移，换评委模型等于换尺子"。`run.py history` 这一侧
一直只按 task 字符串分组 —— 于是 flash-lite 时代（读数被挤成常数、放水 +3.7）的轮次
会和 deepseek-v4-flash 时代的轮次混进同一个 Δ 均值，读出一个对当前仪表并不成立的"显著"。
`run_*.json` 里的 `judge_health.models` 就是这把尺子的身份，本文件钉住它被用起来。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from pm.cli.history import (
    UNKNOWN_GENERATION,
    _history_command,
    _load_run_history,
    _ruler_generation,
    _task_stats,
)


def _run(run_id: str, task: str, delta: float, *, gen: str, trusted: bool = True) -> dict[str, Any]:
    """按 `_load_run_history` 的形状造一行；`gen` 就是 `judge_gen`（尺子身份）。"""
    base_avg = 7.0
    return {
        "run_id": run_id,
        "task": task,
        "delta": delta,
        "opt_avg": round(base_avg + delta, 2),
        "base_avg": base_avg,
        "trusted": trusted,
        "status": "passed",
        "demo": False,
        "judge_gen": gen,
    }


# --------------------------------------------------------------------------
# 分组：不同尺子不许混进同一个均值
# --------------------------------------------------------------------------
def test_two_rulers_on_one_task_are_not_averaged_together() -> None:
    """同一 task、两把尺子各两条臂：必须分成两组，各自算各自的分。

    混算会把 (1.0+1.2+9.0+9.2)/4=5.1 报成一个"显著为正"的均值——那是两把尺的算术平均，
    不代表任何一把尺量出来的东西。"""
    rows = [
        _run("a1", "t", 1.0, gen="尺A"),
        _run("a2", "t", 1.2, gen="尺A"),
        _run("b1", "t", 9.0, gen="尺B"),
        _run("b2", "t", 9.2, gen="尺B"),
    ]
    out = _task_stats(rows)
    by = {s["judge_gen"]: s["delta_mean"] for s in out}
    assert by == {"尺A": 1.1, "尺B": 9.1}, by


def test_a_ruler_with_a_single_arm_gets_no_verdict_but_the_other_still_does() -> None:
    """跨代之后某把尺只剩 1 条可信臂：那把尺不给判定（n<2），另一把照给。"""
    rows = [
        _run("a1", "t", 1.0, gen="尺A"),
        _run("b1", "t", 5.0, gen="尺B"),
        _run("b2", "t", 5.2, gen="尺B"),
    ]
    out = _task_stats(rows)
    assert len(out) == 1
    assert out[0]["judge_gen"] == "尺B"


def test_runs_without_a_recorded_ruler_never_merge_with_a_known_one() -> None:
    """没有评委配置记录的旧归档归入 UNKNOWN 桶，**不并进**已知代——
    "没记录"不等于"同一把尺子"。"""
    rows = [
        _run("k1", "t", 1.0, gen="尺A"),
        _run("k2", "t", 1.2, gen="尺A"),
        _run("u1", "t", 7.0, gen=UNKNOWN_GENERATION),
        _run("u2", "t", 7.2, gen=UNKNOWN_GENERATION),
    ]
    out = _task_stats(rows)
    assert {s["judge_gen"] for s in out} == {"尺A", UNKNOWN_GENERATION}


def test_missing_judge_gen_field_falls_back_to_unknown() -> None:
    """直接调 `_task_stats` 的调用方（测试、脚本）没带 judge_gen 时，按未知代处理，
    不与任何已知代合并。"""
    r1 = _run("x", "t", 1.0, gen="尺A")
    del r1["judge_gen"]
    r2 = _run("y", "t", 1.2, gen="尺A")
    del r2["judge_gen"]
    assert _task_stats([r1, r2])[0]["judge_gen"] == UNKNOWN_GENERATION


# --------------------------------------------------------------------------
# 尺子身份从哪来：judge_health.models，且与字典顺序无关
# --------------------------------------------------------------------------
def test_ruler_generation_reads_judge_health_and_is_order_independent() -> None:
    a = {"judge_health": {"models": {"evaluator": "m1@0.1", "evaluator_b": "m2@0.1"}}}
    b = {"judge_health": {"models": {"evaluator_b": "m2@0.1", "evaluator": "m1@0.1"}}}
    assert _ruler_generation(a) == _ruler_generation(b)
    assert "m1@0.1" in _ruler_generation(a) and "m2@0.1" in _ruler_generation(a)


def test_ruler_generation_is_unknown_without_a_usable_panel() -> None:
    assert _ruler_generation({}) == UNKNOWN_GENERATION
    assert _ruler_generation({"judge_health": None}) == UNKNOWN_GENERATION
    assert _ruler_generation({"judge_health": {"models": {}}}) == UNKNOWN_GENERATION


def test_load_run_history_records_the_ruler(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`_load_run_history` 必须把 judge_health 折成尺子身份，别只把它留在原始 dict 里。"""
    logs = tmp_path / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    d: dict[str, Any] = {
        "run_id": "r1",
        "task": "t",
        "status": "passed",
        "aggregate": {"avg_score": 8.0},
        "baseline_aggregate": {"avg_score": 7.0},
        "trace": [{"channel": "plain"}],
        "errors": [],
        "judge_health": {"models": {"evaluator": "m1@0.1", "evaluator_b": "m2@0.1"}},
    }
    p = logs / "run_r1.json"
    p.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    os.utime(p, (1_700_000_000, 1_700_000_000))
    monkeypatch.setenv("PM_LOG_DIR", str(logs))
    rows = _load_run_history(10, False)
    assert rows[0]["judge_gen"] == _ruler_generation(d)


# --------------------------------------------------------------------------
# 渲染：分代要说出来
# --------------------------------------------------------------------------
def _archive(
    run_id: str, task: str, avg: float, base: float, *, models: dict[str, str]
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "task": task,
        "status": "passed",
        "aggregate": {
            "avg_score": avg,
            "min_score": min(avg, base),
            "n_passed": 2,
            "untrusted_case_indices": [],
        },
        "baseline_aggregate": {"avg_score": base},
        "trace": [{"channel": "plain"}],
        "errors": [],
        "judge_health": {"models": models},
    }


def _fake_logs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, runs: list[dict[str, Any]]) -> None:
    logs = tmp_path / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    for i, r in enumerate(runs):
        p = logs / f"run_{r['run_id']}.json"
        p.write_text(json.dumps(r, ensure_ascii=False), encoding="utf-8")
        os.utime(p, (1_700_000_000 + i * 60, 1_700_000_000 + i * 60))
    monkeypatch.setenv("PM_LOG_DIR", str(logs))


RULER_A = {"evaluator": "old-flash-lite@0.1", "evaluator_b": "glm-5.2@0.1"}
RULER_B = {"evaluator": "new-flash@0.1", "evaluator_b": "glm-5.2@0.1"}


def test_text_output_warns_when_a_task_spans_two_rulers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """一个任务跨了两把尺：两组各印一行，并且**明说**它们不是一列数。"""
    _fake_logs(
        monkeypatch,
        tmp_path,
        [
            _archive("old1", "上线登录", 8.0, 7.0, models=RULER_A),
            _archive("old2", "上线登录", 8.1, 7.0, models=RULER_A),
            _archive("new1", "上线登录", 8.5, 7.0, models=RULER_B),
            _archive("new2", "上线登录", 8.6, 7.0, models=RULER_B),
        ],
    )
    assert _history_command(["--last", "50"]) == 0
    out = capsys.readouterr().out
    assert "评委" in out  # 尺子身份被印出来了
    assert "跨" in out and "尺" in out  # 跨代警告


def test_json_task_groups_carry_the_ruler(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _fake_logs(
        monkeypatch,
        tmp_path,
        [
            _archive("old1", "上线登录", 8.0, 7.0, models=RULER_A),
            _archive("old2", "上线登录", 8.1, 7.0, models=RULER_A),
            _archive("new1", "上线登录", 8.5, 7.0, models=RULER_B),
            _archive("new2", "上线登录", 8.6, 7.0, models=RULER_B),
        ],
    )
    assert _history_command(["--last", "50", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    gens = {g["judge_gen"] for g in payload["task_groups"]}
    assert len(gens) == 2
    assert all("evaluator=" in g for g in gens)
