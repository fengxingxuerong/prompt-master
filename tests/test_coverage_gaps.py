"""覆盖率缺口分支测试（2026-09-18 全方位测试盘点第二批）。

各模块主路径已在既有 test_*.py 覆盖；本文件集中补 --cov-report=term-missing
当时仍缺失的分支：report 用量台账、scheduler 落盘与报告兜底、server 校准巡检
与 history 子进程、assertions 子进程隔离的异常路径、baseline/revise 的失败与
跳过分支、common 开关解析、bootstrap 引导兜底。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time as _time_mod
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pm import scheduler as pm_scheduler
from pm import server as pm_server
from pm.assertions import _child_match, _match_isolated, _seconds, check_assertion
from pm.bootstrap import ensure_utf8_stdio
from pm.nodes import baseline as baseline_mod
from pm.nodes import common as nodes_common
from pm.nodes import revise as revise_mod
from pm.report import _report_safe_int, render_report
from pm.scheduler import TaskManager, _build_race_report, _int_env, _save_artifacts
from pm.schemas import PreferenceResult
from pm.store import TaskRecord


def _state(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "run_id": "gap1",
        "task": "让 AI 分析销售数据",
        "iteration": 1,
        "target_model": "fake-target",
        "n_test_cases": 1,
        "max_iterations": 1,
        "prompt": "旧版提示词",
        "prompt_history": [],
        "test_cases": [{"input": "a", "expected": "b"}],
    }
    base.update(kw)
    return base


def _jdump(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


class _StopLoop(Exception):
    """哨兵：巡检循环的 sleep 被替换为抛出它，用于在首轮后退出 while True。"""


def _boom(*a: Any, **kw: Any) -> Any:
    raise RuntimeError("boom")


def _wait_status(
    tm: TaskManager, run_id: str, want: str = "failed", timeout: float = 15.0
) -> dict[str, Any]:
    deadline = _time_mod.monotonic() + timeout
    while _time_mod.monotonic() < deadline:
        st = tm.get_status(run_id)
        if st and st.get("status") == want:
            return st
        _time_mod.sleep(0.05)
    raise AssertionError(f"任务 {run_id} 未进入 {want}：{tm.get_status(run_id)}")


# ---------------------------------------------------------------- report


def test_safe_int_garbage_returns_zero() -> None:
    assert _report_safe_int("abc") == 0
    assert _report_safe_int(None) == 0
    assert _report_safe_int(-5) == 0
    assert _report_safe_int(7) == 7


def test_render_report_includes_usage_ledger() -> None:
    state = _state(
        status="passed",
        llm_usage={
            "generate": {"calls": 2, "input_tokens": 100, "output_tokens": 50, "latency_ms": 1500},
            "evaluate": {"calls": 1, "input_tokens": 50, "output_tokens": 30, "latency_ms": 500},
        },
    )
    md = render_report(state)[0]  # render_report 返回 (markdown, meta) 元组
    assert "## 模型用量与耗时（按角色）" in md
    assert "| generate | 2 | 100 | 50 | 1.5s |" in md
    assert "| **合计** | 3 | 150 | 80 | 2.0s |" in md


# ---------------------------------------------------------------- scheduler


def test_save_artifacts_oserror_returns_none_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("占位文件，当作目录用", encoding="utf-8")
    monkeypatch.setattr(pm_scheduler, "_log_dir", lambda: blocker)
    log_path, report_path = _save_artifacts("osr1", _state())
    assert log_path is None and report_path is None


def test_int_env_fallbacks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PM_GAP_INT", "abc")
    assert _int_env("PM_GAP_INT", 7) == 7
    monkeypatch.setenv("PM_GAP_INT", "")
    assert _int_env("PM_GAP_INT", 7) == 7
    monkeypatch.setenv("PM_GAP_INT", " 3 ")
    assert _int_env("PM_GAP_INT", 7) == 3


def test_task_manager_duplicate_id_and_failed_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pm_scheduler, "build_app", _boom)
    tm = TaskManager(max_workers=1)
    assert tm.submit("dup1", task="T") == "dup1"
    with pytest.raises(ValueError, match="已存在"):
        tm.submit("dup1", task="T")
    st = _wait_status(tm, "dup1")
    assert st["status"] == "failed" and "boom" in str(st.get("error"))
    # 失败任务没有产物：记录无 report_path、logs/ 也无同名 fallback
    assert tm.get_report("dup1") is None


def test_get_report_from_record_and_log_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pm_scheduler, "_log_dir", lambda: tmp_path)
    tm = TaskManager()
    rp = tmp_path / "report_rf1.md"
    rp.write_text("记录内报告", encoding="utf-8")
    tm.store.put_task(TaskRecord(run_id="rf1", status="finished", report_path=rp))
    assert tm.get_report("rf1") == "记录内报告"

    fallback = tmp_path / "report_rf2.md"
    fallback.write_text("重启后找回", encoding="utf-8")
    tm.store.put_task(TaskRecord(run_id="rf2", status="pending"))
    assert tm.get_report("rf2") == "重启后找回"


def test_run_task_invalid_scenario_warns_and_task_file_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("PM_FAKE_BACKEND", "bogus-scene")
    task_file = tmp_path / "task.txt"
    task_file.write_text("从文件读取的任务", encoding="utf-8")
    monkeypatch.setattr(pm_scheduler, "build_app", _boom)
    tm = TaskManager(max_workers=1)
    with caplog.at_level(logging.WARNING):
        tm.submit("scn1", task_file=str(task_file))
        st = _wait_status(tm, "scn1")
    assert st["status"] == "failed" and "boom" in str(st.get("error"))
    assert "不是可用场景" in caplog.text


def test_build_race_report_flags_incomplete_cases() -> None:
    from pm.store import RaceRecord

    race = RaceRecord(race_id="rc1", name="赛马", run_ids=["a", "b"], spec_count=2)
    statuses = {
        "a": {
            "status": "finished",
            "iteration": 2,
            "aggregate": {
                "avg_score": 8.5,
                "min_score": 8,
                "n_cases": 2,
                "n_cases_expected": 3,
                "passed": True,
                "cases_complete": False,
            },
        },
        "b": {
            "status": "finished",
            "iteration": 1,
            "aggregate": {"avg_score": 7.0, "min_score": 7, "n_cases": 2, "passed": False},
        },
    }
    md = _build_race_report(race, statuses, {})
    assert "| a | finished | 2 | 8.5 | 8 | 2/3 | True |" in md
    assert "实际用例数少于声明" in md


# ---------------------------------------------------------------- server


def test_calibrate_patrol_loop_reports_each_outcome(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def _run_once(result: Any) -> None:
        monkeypatch.setattr(pm_server, "_scheduled_calibrate_once", lambda judge: result)
        monkeypatch.setattr(
            pm_server._time, "sleep", lambda secs: (_ for _ in ()).throw(_StopLoop())
        )
        with caplog.at_level(logging.INFO):
            with pytest.raises(_StopLoop):
                pm_server._calibrate_patrol_loop(0.001, "evaluator")

    _run_once(None)  # 排程内部已告警的静默分支
    _run_once(
        {
            "drift": {
                "drifted": True,
                "prev_bias": 0.1,
                "delta_bias": -0.7,
                "prev_mae": 0.2,
                "delta_mae": 0.5,
            },
            "analysis": {"bias": -0.6},
        }
    )
    assert "评委漂移告警" in caplog.text and "建议人工复核锚点样本" in caplog.text

    _run_once({"drift": {"drifted": False, "delta_bias": 0.01, "delta_mae": 0.02}})
    assert "定期校准稳定" in caplog.text

    _run_once({"drift": None})
    assert "首次建账" in caplog.text


def test_calibrate_patrol_loop_swallows_exceptions(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(pm_server, "_scheduled_calibrate_once", _boom)
    monkeypatch.setattr(pm_server._time, "sleep", lambda secs: (_ for _ in ()).throw(_StopLoop()))
    with caplog.at_level(logging.WARNING):
        with pytest.raises(_StopLoop):
            pm_server._calibrate_patrol_loop(0.001, "evaluator")
    assert "定期校准失败" in caplog.text and "不影响服务" in caplog.text


def test_history_endpoint_real_subprocess() -> None:
    """真实子进程跑 `run.py history --json`：DEVNULL stdin 是踩过坑的回归护栏。"""
    from pm.server import history_endpoint

    resp = asyncio.run(history_endpoint())
    body = json.loads(bytes(resp.body).decode("utf-8"))
    assert isinstance(body, dict)


def test_history_endpoint_bad_json_becomes_500(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi import HTTPException
    from pm.server import history_endpoint

    fake = SimpleNamespace(stdout="这不是JSON", stderr="err!")
    monkeypatch.setattr(pm_server.subprocess, "run", lambda *a, **kw: fake)
    with pytest.raises(HTTPException) as ei:
        asyncio.run(history_endpoint())
    assert ei.value.status_code == 500
    assert "history 输出异常" in str(ei.value.detail)


# ---------------------------------------------------------------- assertions


def test_child_match_invalid_regex() -> None:
    import multiprocessing as mp

    q = mp.Queue()
    _child_match("[unclosed", "任意输出", q)
    ok, matched, err = q.get(timeout=5)
    assert ok is False and matched is False and "正则非法" in err


def test_child_match_generic_error() -> None:
    import multiprocessing as mp

    q = mp.Queue()
    _child_match("a", None, q)  # text=None → re.search 抛 TypeError
    ok, _matched, err = q.get(timeout=5)
    assert ok is False and "匹配执行失败" in err


def test_match_isolated_inline_fallback_when_process_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Ctx:
        def Process(self, **kw: Any) -> Any:
            raise OSError("无法创建子进程")

        def Queue(self) -> Any:
            raise OSError("无法创建队列")

    monkeypatch.setattr("multiprocessing.get_context", lambda name=None: _Ctx())
    status, matched, err = _match_isolated("A", "bAcd", 1.0)
    assert status == "ok" and matched is True and err == ""
    status, matched, err = _match_isolated("[", "x", 1.0)
    assert status == "error" and "正则非法" in err


def test_match_isolated_child_dies_without_result(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Proc:
        def start(self) -> None:
            pass

        def join(self, t: float) -> None:
            pass

        def is_alive(self) -> bool:
            return False

    class _Q:
        def get(self, timeout: float | None = None) -> Any:
            raise RuntimeError("子进程没来得及写回")

    class _Ctx:
        def Process(self, **kw: Any) -> Any:
            return _Proc()

        def Queue(self) -> Any:
            return _Q()

    monkeypatch.setattr("multiprocessing.get_context", lambda name=None: _Ctx())
    status, _matched, err = _match_isolated("a", "a", 1.0)
    assert status == "error" and "未返回结果" in err


def test_seconds_env_fallbacks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PM_GAP_S", "abc")
    assert _seconds("PM_GAP_S", 2.0) == 2.0
    monkeypatch.setenv("PM_GAP_S", "-1")
    assert _seconds("PM_GAP_S", 2.0) == 2.0
    monkeypatch.setenv("PM_GAP_S", "0.5")
    assert _seconds("PM_GAP_S", 2.0) == 0.5


def test_regex_assertion_invalid_pattern_reports_error() -> None:
    r = check_assertion("[unclosed", "任意输出", "regex")
    assert r is not None and r.passed is False and "正则非法" in r.detail


# ---------------------------------------------------------------- nodes/common


def test_strip_code_fence_variants() -> None:
    assert nodes_common._strip_code_fence("```text\n内容```") == "内容"
    assert nodes_common._strip_code_fence("  无围栏  ") == "无围栏"
    no_fence = "x```y```z"  # 不以围栏开头 → 原样返回
    assert nodes_common._strip_code_fence(no_fence) == no_fence


def test_feature_enabled_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    for v in ("0", "false", "no", "off", ""):
        monkeypatch.setenv("PM_GAP_F", v)
        assert nodes_common._feature_enabled("PM_GAP_F", True) is False
    for v in ("1", "true", "yes", "on"):
        monkeypatch.setenv("PM_GAP_F", v)
        assert nodes_common._feature_enabled("PM_GAP_F", False) is True
    monkeypatch.setenv("PM_GAP_F", "banana")
    assert nodes_common._feature_enabled("PM_GAP_F", True) is True  # 非法值回退默认


# ---------------------------------------------------------------- bootstrap


def test_ensure_utf8_stdio_noop_outside_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "name", "posix")
    ensure_utf8_stdio()  # 直接返回，不应触碰任何流


def test_ensure_utf8_stdio_reconfigure_failure_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Boom:
        def reconfigure(self, **kw: Any) -> None:
            raise OSError("宿主不支持 reconfigure")

    monkeypatch.setattr(sys, "stdout", _Boom())
    monkeypatch.setattr(sys, "stderr", _Boom())
    ensure_utf8_stdio()  # 引导兜底：静默跳过


# ---------------------------------------------------------------- nodes/baseline


def test_baseline_node_skip_reasons(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PM_BASELINE", "1")
    out = baseline_mod.baseline_node(_state(baseline_runs=[{"output": "x"}]))
    assert "基线已跑过" in _jdump(out)
    out2 = baseline_mod.baseline_node(_state(test_cases=[]))
    assert "无测试用例" in _jdump(out2)


def test_baseline_node_failure_does_not_block(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PM_BASELINE", "1")
    monkeypatch.setattr(baseline_mod, "_run_matrix", _boom)
    out = baseline_mod.baseline_node(_state())
    dumped = _jdump(out)
    assert "baseline_failed" in dumped and "boom" in dumped


def test_compare_node_skip_reasons(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PM_BASELINE", "1")
    monkeypatch.setenv("PM_PAIRWISE", "1")
    out = baseline_mod.compare_node(_state(test_runs=[{"output": "a"}]))
    assert "缺少基线" in _jdump(out)
    out2 = baseline_mod.compare_node(
        _state(
            baseline_runs=[{"test_case_index": 0, "output": "b"}],
            test_runs=[{"test_case_index": 1, "output": "c"}],
        )
    )
    assert "没有可对齐的用例" in _jdump(out2)


def _pref(winner: str) -> PreferenceResult:
    return PreferenceResult(winner=winner, decisive=True, reason="差异指向输出结构")


def test_compare_node_all_comparator_calls_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PM_BASELINE", "1")
    monkeypatch.setenv("PM_PAIRWISE", "1")
    monkeypatch.setattr(baseline_mod.llm, "structured_call", _boom)
    out = baseline_mod.compare_node(
        _state(
            baseline_runs=[{"test_case_index": 0, "output": "基线输出"}],
            test_runs=[{"test_case_index": 0, "output": "本轮输出"}],
        )
    )
    dumped = _jdump(out)
    assert "no_signal" in dumped and "compare#0" in dumped


def test_compare_node_conflict_attribution(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PM_BASELINE", "1")
    monkeypatch.setenv("PM_PAIRWISE", "1")
    # 选一个 flip=True 的 run_id：A 侧=基线，winner="A" ⇒ 判定"更差"，与达标结果冲突
    rid = next(r for r in ("a", "b", "c", "d") if baseline_mod._ab_flip(r, 0))
    monkeypatch.setattr(baseline_mod.llm, "structured_call", lambda *a, **kw: (_pref("A"), None))
    out = baseline_mod.compare_node(
        _state(
            run_id=rid,
            aggregate={"passed": True, "avg_score": 8.5},
            baseline_runs=[{"test_case_index": 0, "output": "基线输出"}],
            test_runs=[{"test_case_index": 0, "output": "本轮输出"}],
        )
    )
    assert "结论存疑" in _jdump(out)


# ---------------------------------------------------------------- nodes/revise


def test_revise_node_llm_failure_returns_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("pm.nodes.revise._generate_prompt_with_gate", _boom)
    out = revise_mod.revise_node(_state(prompt_history=[]))
    dumped = _jdump(out)
    assert "failed" in dumped and "boom" in dumped


def test_revise_node_accumulates_quality_issues(monkeypatch: pytest.MonkeyPatch) -> None:
    gate = SimpleNamespace(ok=False, describe=lambda: "提示词包含疑似注入")
    monkeypatch.setattr(
        "pm.nodes.revise._generate_prompt_with_gate",
        lambda *a, **kw: ("新提示词", {"gate": "quality"}, gate, 2),
    )
    out = revise_mod.revise_node(_state(prompt_history=[]))
    dumped = _jdump(out)
    assert "疑似注入" in dumped and "prompt_quality_issues" in dumped
