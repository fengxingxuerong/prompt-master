"""M3.5 stderr 噪音治理：TaskManager 停机排空生命周期钩子回归。

此前进程退出时线程池没有排空钩子：排队任务被强杀、在跑任务的异常无人取回
（未检索的 Future 异常 → 退出期 "Exception ignored" 类 stderr 噪音）。
"""

from __future__ import annotations

import threading
import time

import pytest
from fastapi.testclient import TestClient
from pm.scheduler import TaskManager
from pm.store import MemoryStore


def _make_tm() -> TaskManager:
    return TaskManager(max_workers=1, store=MemoryStore())


def test_shutdown_cancels_queued_and_drains_running(monkeypatch):
    """排队任务被取消（记录标 failed）；在跑任务等它自然收尾后 DB 为 finished。"""
    tm = _make_tm()
    release = threading.Event()
    started = threading.Event()

    def slow_task(run_id, kwargs):
        started.set()
        release.wait(timeout=10)
        tm.store.update_task(run_id, status="finished")

    monkeypatch.setattr(tm, "_run_task", slow_task)

    rid_a = tm.submit("A", task="x")
    assert started.wait(timeout=5), "在跑任务应先占住唯一 worker"
    tm.submit("B", task="x")  # 排队

    drain: dict[str, list[str]] = {}
    t = threading.Thread(target=lambda: drain.update(res=tm.shutdown(wait=True)))
    t.start()
    # B 应在 A 仍在跑时就被取消（worker 被 A 占住，B 未启动）
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        rec_b = tm.store.get_task("B")
        if rec_b and rec_b.status == "failed":
            break
        time.sleep(0.02)
    assert tm.store.get_task("B").status == "failed"
    assert "取消" in (tm.store.get_task("B").error or "")
    release.set()  # 放行 A，让排空等待自然收尾
    t.join(timeout=10)

    assert drain["res"] == ["B"]
    assert tm.store.get_task(rid_a).status == "finished", "排空应等在跑任务落库完成"
    assert tm._drained is True


def test_shutdown_retrieves_running_failure_silently(monkeypatch):
    """在跑任务抛异常：shutdown 负责取回（消音），不向外传播。"""
    tm = _make_tm()

    def boom(run_id, kwargs):
        # 模拟真 _run_task 契约：异常先落 failed 记录，再让 future 带异常收尾
        tm.store.update_task(run_id, status="failed", error="任务内部炸了")
        raise RuntimeError("任务内部炸了")

    monkeypatch.setattr(tm, "_run_task", boom)
    tm.submit("X", task="x")
    cancelled = tm.shutdown(wait=True)  # 不应抛 RuntimeError
    assert cancelled == []
    assert tm.store.get_task("X").status == "failed"


def test_submit_rejected_after_shutdown_and_idempotent():
    """停机后拒绝新任务（明确报错而非解释器期 RuntimeError）；重复 shutdown 幂等。"""
    tm = _make_tm()
    tm.shutdown(wait=False)
    with pytest.raises(RuntimeError, match="停机"):
        tm.submit("Y", task="x")
    assert tm.shutdown() == []


def test_shutdown_timeout_returns_without_hanging(monkeypatch):
    """PM_DRAIN_TIMEOUT 场景：卡死任务到点转不等待，shutdown 不无限挂起。"""
    tm = _make_tm()
    release = threading.Event()

    def stuck(run_id, kwargs):
        release.wait(timeout=30)

    monkeypatch.setattr(tm, "_run_task", stuck)
    tm.submit("S", task="x")
    t0 = time.monotonic()
    tm.shutdown(wait=True, timeout=0.2)  # 0.2s 后放弃等待，但不应抛错
    assert time.monotonic() - t0 < 5
    release.set()  # 收尾工作线程，避免测试进程退出时被 join 卡住


def test_server_shutdown_hook_drains_pool(monkeypatch):
    """server 接线：TestClient 生命周期退出时触发排空（真实 uvicorn 停机同路径）。"""
    from pm import server

    fresh = _make_tm()
    monkeypatch.setattr(server, "_scheduler", fresh)
    assert len(server.app.router.on_shutdown) >= 1, "停机钩子应在位"
    with TestClient(server.app):
        assert fresh._drained is False
    assert fresh._drained is True, "生命周期退出应触发排空"
