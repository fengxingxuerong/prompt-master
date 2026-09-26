"""任务记录存储层回归（多 worker 共享状态的前提）。

覆盖三层：
1. `MemoryStore` / `SqliteStore` 的协议行为一致（读写、按插入序淘汰、report_path 还原）；
2. **跨实例可见性**：两个 SqliteStore 指向同一个文件 —— 一个写、另一个读，
   这正是"两个 worker 共享一份记录"的最小复现（没有它就谈不上 --workers > 1）；
3. `TaskManager` 接 SQLite 后仍能跑完整任务，且**另一个 TaskManager 实例**
   （模拟另一个 worker）能查到状态与报告。
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from pm.scheduler import TaskManager, _progress_of
from pm.store import MemoryStore, RaceRecord, SqliteStore, TaskRecord, store_from_env


# --------------------------------------------------------------------------
# 协议行为：两个实现都得满足
# --------------------------------------------------------------------------
@pytest.fixture(params=["memory", "sqlite"])
def store(request, tmp_path: Path):
    if request.param == "memory":
        yield MemoryStore()
    else:
        s = SqliteStore(tmp_path / "tasks.db")
        yield s
        s.close()


def test_put_get_and_update(store):
    store.put_task(TaskRecord(run_id="r1", status="pending"))
    rec = store.get_task("r1")
    assert rec is not None and rec.status == "pending"

    store.update_task("r1", status="running", progress={"iteration": 2})
    rec = store.get_task("r1")
    assert rec is not None
    assert rec.status == "running"
    assert rec.progress == {"iteration": 2}
    # 未提到的字段不能被抹掉
    assert store.get_task("r1").run_id == "r1"


def test_update_missing_record_is_noop(store):
    """记录被淘汰后任务仍在跑：update 必须是 no-op，不能凭空造记录。"""
    store.update_task("nope", status="running")
    assert store.get_task("nope") is None


def test_report_path_roundtrip(store, tmp_path: Path):
    p = tmp_path / "report_r2.md"
    store.put_task(TaskRecord(run_id="r2", status="finished", report_path=p))
    rec = store.get_task("r2")
    assert rec is not None
    assert isinstance(rec.report_path, Path)
    assert rec.report_path == p


def test_evict_keeps_newest(store):
    for i in range(6):
        store.put_task(TaskRecord(run_id=f"r{i}"))
    store.evict(3)
    ids = store.list_task_ids()
    assert ids == ["r3", "r4", "r5"], f"淘汰应保留最新的 3 条：{ids}"
    assert store.count_tasks() == 3


def test_evict_does_not_reorder_on_update(store):
    """更新（进度刷新）不能改变插入序，否则淘汰会踢掉正在跑的任务。"""
    for i in range(4):
        store.put_task(TaskRecord(run_id=f"r{i}"))
    store.update_task("r0", progress={"iteration": 9})
    store.evict(2)
    assert store.list_task_ids() == ["r2", "r3"]


def test_race_records(store):
    store.put_race(RaceRecord(race_id="rac1", name="赛马", run_ids=["a", "b"], spec_count=2))
    race = store.get_race("rac1")
    assert race is not None
    assert race.run_ids == ["a", "b"] and race.spec_count == 2
    assert store.count_races() == 1


def test_unknown_record_returns_none(store):
    assert store.get_task("missing") is None
    assert store.get_race("missing") is None


# --------------------------------------------------------------------------
# 跨实例可见性：这就是多 worker 的核心诉求
# --------------------------------------------------------------------------
def test_sqlite_store_is_visible_across_instances(tmp_path: Path):
    db = tmp_path / "shared.db"
    writer = SqliteStore(db)
    reader = SqliteStore(db)  # 模拟另一个 worker 进程
    try:
        writer.put_task(TaskRecord(run_id="w1", status="running"))
        writer.update_task("w1", progress={"status": "running", "iteration": 1, "llm_calls": 7})
        seen = reader.get_task("w1")
        assert seen is not None, "另一个实例读不到记录 = 多 worker 时 status 会 404"
        assert seen.progress == {"status": "running", "iteration": 1, "llm_calls": 7}

        writer.update_task("w1", status="finished", result={"status": "passed"})
        assert reader.get_task("w1").status == "finished"
        assert reader.count_tasks() == 1
    finally:
        writer.close()
        reader.close()


def test_concurrent_writes_do_not_corrupt(tmp_path: Path):
    """并发写同一份记录：线程锁 + WAL 下不应该丢记录或抛 sqlite 错误。"""
    import threading

    db = tmp_path / "concurrent.db"
    store = SqliteStore(db)
    try:
        for i in range(5):
            store.put_task(TaskRecord(run_id=f"c{i}"))

        def worker(i: int) -> None:
            for n in range(20):
                store.update_task(f"c{i}", progress={"iteration": n})

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert store.count_tasks() == 5
        for i in range(5):
            assert store.get_task(f"c{i}").progress == {"iteration": 19}
    finally:
        store.close()


def test_store_from_env(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("PM_TASK_DB", raising=False)
    assert isinstance(store_from_env(), MemoryStore)

    db = tmp_path / "env.db"
    monkeypatch.setenv("PM_TASK_DB", str(db))
    s = store_from_env()
    try:
        assert isinstance(s, SqliteStore)
        assert db.exists()
    finally:
        s.close()


# --------------------------------------------------------------------------
# TaskManager 接 SQLite：端到端
# --------------------------------------------------------------------------
def _wait_terminal(tm: TaskManager, rid: str, timeout: float = 60.0) -> dict:
    deadline = time.time() + timeout
    st: dict = {}
    while time.time() < deadline:
        st = tm.get_status(rid) or {}
        if st.get("status") in ("passed", "max_iterations", "failed", "early_stopped"):
            return st
        time.sleep(0.2)
    return st


def test_taskmanager_on_sqlite_is_visible_to_other_instance(tmp_path: Path, monkeypatch):
    """一个 manager 跑任务，另一个 manager（同库）能查到状态与报告。"""
    monkeypatch.setenv("PM_FAKE_BACKEND", "progress")
    monkeypatch.setenv("PM_LOG_DIR", str(tmp_path))
    db = tmp_path / "tasks.db"

    runner = TaskManager(max_workers=1, store=SqliteStore(db))
    observer = TaskManager(max_workers=1, store=SqliteStore(db))  # 另一个 worker
    try:
        rid = runner.submit("sql-run", task="让 AI 分析销售数据", n_test_cases=1, max_iterations=1)
        st = _wait_terminal(observer, rid)
        assert st.get("status") in ("passed", "max_iterations", "early_stopped"), st
        # 记录里的用例数与报告都要能从"另一个进程"读到
        assert st.get("run_id") == rid
        assert observer.get_report(rid), "另一个实例读不到报告 = 多 worker 时 report 会 404"
    finally:
        runner.store.close()
        observer.store.close()


def test_taskmanager_memory_store_compat_view(tmp_path: Path, monkeypatch):
    """旧访问方式（tm._tasks[rid] / len(tm._tasks)）在抽象层之后仍然可用。"""
    monkeypatch.setenv("PM_FAKE_BACKEND", "progress")
    monkeypatch.setenv("PM_LOG_DIR", str(tmp_path))
    tm = TaskManager(max_workers=1, max_records=3)
    try:
        rid = tm.submit("view-run", task="t", n_test_cases=1, max_iterations=1)
        assert rid in tm._tasks
        assert tm._tasks[rid].status in ("pending", "running", "finished")
        assert len(tm._tasks) >= 1
        for i in range(5):
            tm.submit(f"view-{i}", task="t", n_test_cases=1, max_iterations=1)
        assert len(tm._tasks) <= 4  # 淘汰后有界
    finally:
        # 等队尾而非首个 rid：max_records=3 时 view-run 早已被淘汰（get_status
        # 返回 None，store 层也查不到），等它会空转满 timeout=30 秒（实测 30s 全
        # 浪费在这）。串行队列下队尾终态 ⇔ 全部跑完，排空意图不变。
        _wait_terminal(tm, "view-4", timeout=30)


def test_progress_snapshot_shape_is_store_agnostic(tmp_path: Path, monkeypatch):
    """SQLite 往返不能把进度快照的结构弄坏（_progress_of 的字段要原样还在）。"""
    monkeypatch.setenv("PM_FAKE_BACKEND", "progress")
    monkeypatch.setenv("PM_LOG_DIR", str(tmp_path))
    db = tmp_path / "shape.db"
    store = SqliteStore(db)
    tm = TaskManager(max_workers=1, store=store)
    try:
        rid = tm.submit("shape-run", task="t", n_test_cases=1, max_iterations=1)
        st = _wait_terminal(tm, rid)
        assert st.get("status") in ("passed", "max_iterations", "early_stopped")
        # 关键字段必须存活（JSON 往返后类型可能变化，但键与可用性不能丢）
        assert st.get("run_id") == rid
        assert isinstance(st.get("llm_calls"), int)
        assert st.get("task") == "t"
        # 快照能被再次裁一遍而不炸（历史上这里 TypeError 过）
        assert isinstance(_progress_of(st), dict)
    finally:
        store.close()
