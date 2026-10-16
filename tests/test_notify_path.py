"""告警留痕的路径必须受 `PM_LOG_DIR` 管住（2026-10-17）。

## 为什么这条比"多一个文件"严重

`logs/notifications.jsonl` 是运维告警的**唯一活体凭据**：README §三 引的
「usage_store 损坏累计 554 条」就是从它数出来的；轮换/丢账的排查同样只认它。
它是**旁路**（不抛错），所以一旦写脏，没有任何断言会响。

## 缺口（实测复现，不是读码推断）

`notify.notifications_path()` 把路径写死成 `<repo>/logs/notifications.jsonl`，
而 `pm/paths.log_dir()` 是全仓**唯一**的产物目录口径——它的 docstring 明写
「这份逻辑曾同时住在 `pm/scheduler._log_dir` 与 `pm/cli/support.log_dir` 两处…
第三处迟早复现读死路径事故」。这里正是第三处。

后果实测（2026-10-17）：单独跑
`tests/test_usage_store_rebuild.py` + `tests/test_ledger_durability.py` +
`tests/test_modelhub_ledger_and_usage.py` 三个文件，
仓库 `logs/notifications.jsonl` **从 727 行涨到 731 行**，涨的全是带 pytest
临时路径的假告警。而 conftest 那条「运营目录不许被写脏」的闸只 glob
`run_*.json`——正好漏过它，所以这类泄漏**永远不会被拦**。

## 为什么要动 `conftest` 的闸而不是只在 notify 那边修

失败形状是"某个新写的告警点没想到 log_dir"，`code review` 防不住；
而闸只查一种文件名，等于给下一个人留同一个洞。两处一起改。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from pm.modelhub import notify

ROOT = Path(__file__).resolve().parents[1]


def test_notifications_path_follows_pm_log_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`PM_LOG_DIR` 一设，告警就必须落进去——否则测试与异目录部署都会写进仓库。"""
    sealed = tmp_path / "sealed-logs"
    monkeypatch.setenv("PM_LOG_DIR", str(sealed))

    path = notify.notifications_path()

    assert path.parent == sealed, (
        f"告警路径没跟 `PM_LOG_DIR` 走：拿到 {path.parent}，期望 {sealed} —— "
        "`pm/paths.log_dir()` 是唯一口径，写死 `<repo>/logs` 会把假告警灌进运维证据流"
    )
    assert path.name == "notifications.jsonl", "文件名不许顺带改掉（读侧按这个名字取件）"


def test_notifications_path_defaults_to_repo_logs(monkeypatch: pytest.MonkeyPatch) -> None:
    """`PM_LOG_DIR` 没设时仍回落到 `<repo>/logs/notifications.jsonl`：默认行为零漂移。"""
    monkeypatch.delenv("PM_LOG_DIR", raising=False)

    path = notify.notifications_path()

    assert path == ROOT / "logs" / "notifications.jsonl", (
        "没设 `PM_LOG_DIR` 时必须与历史默认一致，否则线上告警会换地方落"
    )


def test_alert_actually_lands_in_pm_log_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """端到端：密封 `PM_LOG_DIR` 后真触发一次告警，它必须落在密封目录里。

    上一条只验函数返回值；这条验调用链真的接在那个函数上
    （本仓付过学费：`list_placeholders` 定义了、有单测、**没有任何调用点**）。
    """
    sealed = tmp_path / "sealed-logs"
    monkeypatch.setenv("PM_LOG_DIR", str(sealed))

    notify._notify("probe: 密封下的一条假告警", source="test")

    note = sealed / "notifications.jsonl"
    assert note.exists(), "密封目录里没有告警文件 —— 说明 _notify 没走 notifications_path()"
    assert "probe: 密封下的一条假告警" in note.read_text(encoding="utf-8")


def test_operating_log_guard_covers_notifications() -> None:
    """conftest 的运营目录闸必须看得见 `notifications.jsonl`（不只 `run_*.json`）。

    闸只查一种文件名 = 下一个人换一种文件名就能静默写脏运营目录。
    """
    body = (ROOT / "tests" / "conftest.py").read_text(encoding="utf-8")
    guard = body.split("def fail_if_the_suite_touched_the_operating_logs(", 1)[-1]
    assert "notifications" in guard, (
        "运营目录闸里没有 `notifications` —— 它是告警证据文件，测试写进去没人会响"
    )


def test_suite_does_not_write_repo_notifications(tmp_path: Path) -> None:
    """真起一个子进程跑告警路径：仓库 `logs/notifications.jsonl` 行数不许增长。

    ⚠️ 用子进程而不是进程内触发：进程内的 `PM_LOG_DIR` 已被会话夹具封住，
    而这条要证的正是"**忘了封**会怎样"（缺陷的失效形态）。
    """
    live = ROOT / "logs" / "notifications.jsonl"
    before = live.stat().st_size if live.exists() else 0

    code = (
        "import os\n"
        "from pm.modelhub import notify\n"
        "notify._notify('probe: 子进程密封下的一条告警', source='test')\n"
        "print(notify.notifications_path())\n"
    )
    env = {k: v for k, v in os.environ.items() if k != "PM_LOG_DIR"}
    env["PM_LOG_DIR"] = str(tmp_path / "child-logs")
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=120,
    )
    assert proc.returncode == 0, f"子进程崩了：{proc.stderr[-300:]}"
    after = live.stat().st_size if live.exists() else 0
    assert after == before, (
        f"子进程设了 PM_LOG_DIR，仓库 logs/notifications.jsonl 还是被写了"
        f"（{before} → {after} 字节）—— 告警路径没被 PM_LOG_DIR 管住"
    )
