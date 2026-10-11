"""评委校准排程（pm/server.py 校准巡逻线程）回归。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pm import server
from pm.scheduler import TaskManager
from pm.store import MemoryStore

_REPO_ROOT = Path(__file__).resolve().parents[1]


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


# ---------------------------------------------------------------------------
# 接线位置：线程属于"服务真的被拉起来了"，不属于"有人 import 了这个模块"
# ---------------------------------------------------------------------------
_IMPORT_PROBE = (
    "import json, threading\n"
    "import pm.server  # 只导入，不启动服务\n"
    "print(json.dumps({'threads': threading.active_count(),\n"
    "                  'names': sorted(t.name for t in threading.enumerate())}))\n"
)


def test_importing_server_starts_no_patrol_thread():
    """干净子进程里 import pm.server：即便开关开着，也不许起线程。

    为什么必须真起子进程：本用例判的是**导入期**行为，而在一个已经导入完的模块上
    怎么 mock 都伪造不出"没 import"——旧缺陷正是 `_maybe_start_calibration_patrol()`
    裸在模块末尾，本轮实测 import 前后线程数 1→2。CLI 的 submit/wait、MCP 工具、
    每个 `from pm import server` 的用例都白拿一个线程，`--workers N` 下还按 N 翻倍
    （N 份排程 = N 倍真实计费调用）。
    """
    proc = subprocess.run(
        [sys.executable, "-c", _IMPORT_PROBE],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
        cwd=str(_REPO_ROOT),
        stdin=subprocess.DEVNULL,
        env={**os.environ, "PM_CALIBRATE_HOURS": "12", "PYTHONIOENCODING": "utf-8"},
    )
    assert proc.returncode == 0, f"import pm.server 自己就失败了：\n{proc.stderr[-800:]}"
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    assert "judge-calibration-patrol" not in payload["names"], (
        f"导入期就起了排程线程（开关是开着的，但它该在 startup 才起）：{payload}"
    )
    assert payload["threads"] == 1, f"导入期出现了额外线程：{payload}"


def test_startup_hook_starts_the_patrol(monkeypatch: pytest.MonkeyPatch):
    """startup 接线在位：进入应用生命周期才起线程。

    与上一条是一对——只把模块末尾那行删掉会让上面绿、这条红。"移出导入期"和
    "仍然会启动"必须同时被管住，否则最容易的改法是改成谁都不起（假修好）。
    """
    monkeypatch.setenv("PM_CALIBRATE_HOURS", "6")
    started: list[str] = []

    class _FakeThread:
        def __init__(self, target=None, args=None, daemon=None, name=None):
            self._name = name or ""

        def start(self) -> None:
            started.append(self._name)

    monkeypatch.setattr(server.threading, "Thread", _FakeThread)
    # 退出生命周期会触发 _drain_task_pool()，它打的是模块级 _scheduler ——
    # 换个新鲜的顶上，别把全局调度器排空（后续用例的 submit 会得到"已停机"）。
    monkeypatch.setattr(server, "_scheduler", TaskManager(max_workers=1, store=MemoryStore()))
    assert server._maybe_start_calibration_patrol in server.app.router.on_startup, (
        "startup 钩子没接上：线程永远不会起"
    )
    assert started == []
    with TestClient(server.app):
        assert started == ["judge-calibration-patrol"]
