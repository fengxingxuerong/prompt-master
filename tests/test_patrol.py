"""评委校准排程（pm/server.py 校准巡逻线程）回归。"""

from __future__ import annotations

import json
import subprocess

import pytest
from pm import server


def test_scheduled_calibrate_once_parses_json(monkeypatch):
    """后台校准 = subprocess 调 run.py calibrate --json，stdout 解析为 dict。"""
    captured = {}

    def fake_run(*args, **kwargs):
        captured["args"] = args[0]
        captured["kwargs"] = kwargs
        out = json.dumps({"analysis": {"bias": 0.2, "mae": 0.4}, "drift": {"drifted": False}})
        return subprocess.CompletedProcess(args[0], 0, stdout=out, stderr="")

    monkeypatch.setattr(server.subprocess, "run", fake_run)
    result = server._scheduled_calibrate_once("evaluator_b")
    assert result and result["analysis"]["bias"] == 0.2
    assert captured["args"][-4:] == ["calibrate", "--judge", "evaluator_b", "--json"]
    assert captured["kwargs"]["stdin"] == subprocess.DEVNULL, "必须掐断继承的 stdio，防 stdio 阻塞"


def test_scheduled_calibrate_bad_output_returns_none(monkeypatch):
    """run.py 崩溃/输出非 JSON 时返回 None（排程循环内部已告警，不炸线程）。"""

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 1, stdout="Traceback...", stderr="boom")

    monkeypatch.setattr(server.subprocess, "run", fake_run)
    assert server._scheduled_calibrate_once() is None


def test_patrol_starts_with_valid_interval(monkeypatch):
    monkeypatch.setenv("PM_CALIBRATE_HOURS", "12")
    created = {}

    class _FakeThread:
        def __init__(self, target=None, args=None, daemon=None, name=None):
            created["args"] = args
            created["name"] = name

        def start(self) -> None:
            created["started"] = True

    monkeypatch.setattr(server.threading, "Thread", _FakeThread)
    server._maybe_start_calibration_patrol()
    assert created["started"] is True
    assert created["name"] == "judge-calibration-patrol"
    assert created["args"] == (12.0, "evaluator")


def test_patrol_ignores_invalid_interval(monkeypatch, caplog):
    monkeypatch.setenv("PM_CALIBRATE_HOURS", "abc")
    started = []

    class _FakeThread:
        def __init__(self, *a, **k) -> None:
            started.append(1)

        def start(self) -> None:
            started.append(2)

    monkeypatch.setattr(server.threading, "Thread", _FakeThread)
    with caplog.at_level("WARNING", logger="pm.server"):
        server._maybe_start_calibration_patrol()
    assert started == []
    assert any("不是数字" in r.getMessage() for r in caplog.records)


def test_patrol_disabled_by_default(monkeypatch):
    """PM_CALIBRATE_HOURS 未设时不起线程：校准是真实计费调用，必须显式开启。"""
    monkeypatch.delenv("PM_CALIBRATE_HOURS", raising=False)
    started = []

    class _FakeThread:
        def __init__(self, *a, **k) -> None:
            started.append(1)

        def start(self) -> None:
            started.append(2)

    monkeypatch.setattr(server.threading, "Thread", _FakeThread)
    server._maybe_start_calibration_patrol()
    assert started == []


def test_patrol_loop_survives_calibration_failure(monkeypatch, caplog):
    """排程循环单次失败不能炸线程：记 WARNING 后继续下一轮。"""
    monkeypatch.setattr(
        server,
        "_scheduled_calibrate_once",
        lambda judge="evaluator": (_ for _ in ()).throw(RuntimeError("网络炸了")),
    )
    sleeps: list[float] = []
    monkeypatch.setattr(
        server._time,
        "sleep",
        lambda s: (
            sleeps.append(s)
            or (_ for _ in ()).throw(
                KeyboardInterrupt  # 用异常退出循环，验证第一轮失败后走到了 sleep
            )
        ),
    )
    with pytest.raises(KeyboardInterrupt):
        server._calibrate_patrol_loop(0.001, "evaluator")
    assert any("不影响服务" in r.getMessage() for r in caplog.records)
    assert sleeps and sleeps[0] == 0.001 * 3600
