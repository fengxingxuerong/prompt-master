"""两个存储后端必须给同一个答案。

实测缺陷（2026-10-31 审计，全部实测于临时目录）：

| 场景 | 内存后端（旧） | sqlite 后端（旧） |
|---|---|---|
| `evict(0)` | 清空 ⇒ 提交后立刻 404 | 夹到 1 ⇒ 留 1 条 |
| `evict(-1)` | `next(iter({}))` 抛 `StopIteration` | 夹到 1 照跑 |
| `update_task` 并发写两个字段 | 一把锁，正常 | 读/写各取一次锁 ⇒ 丢一个字段 |
| `RecordView("race")` | `len`=1 但 `list()` 为空 | 同 |

"同一个配置在两个后端给出不同结果"是最难查的一类问题：换 `PM_TASK_DB` 就变行为，
而症状（任务凭空 404）看起来像调度器坏了。这里把四条都钉成**同口径**。
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
from pm.store import MemoryStore, RaceRecord, SqliteStore, TaskRecord, make_view


@pytest.fixture(params=["memory", "sqlite"])
def store(request: pytest.FixtureRequest, tmp_path: Path):
    s = MemoryStore() if request.param == "memory" else SqliteStore(tmp_path / "t.db")
    yield s
    s.close()


# ------------------------------------------------------------------ evict 语义
def test_evict_zero_means_no_cap_not_wipe(store) -> None:
    """`evict(0)` 不许把表清空（旧版内存后端会，导致任务提交后立刻 404）。"""
    for i in range(3):
        store.put_task(TaskRecord(run_id=f"r{i}"))
    store.evict(0)
    assert store.list_task_ids() == ["r0", "r1", "r2"], "0 被当成'保留 0 条'了"


def test_evict_negative_is_not_a_crash(store) -> None:
    """负数不许抛异常（旧版内存后端在空表上会 StopIteration）。"""
    store.evict(-5)
    store.put_task(TaskRecord(run_id="r1"))
    store.evict(-5)
    assert store.list_task_ids() == ["r1"]

    empty = MemoryStore()
    try:
        empty.evict(-1)  # 空表 + 负数：旧版就是在这里抛的
    finally:
        empty.close()


def test_evict_positive_keeps_newest(store) -> None:
    """正数语义不变：保留最新 N 条。"""
    for i in range(6):
        store.put_task(TaskRecord(run_id=f"r{i}"))
    store.evict(3)
    assert store.list_task_ids() == ["r3", "r4", "r5"]


# ------------------------------------------------------- update 不许丢字段
def test_concurrent_updates_do_not_lose_fields(store) -> None:
    """两个线程各写不同字段：两个都必须落地（sqlite 旧版会丢一个）。"""
    store.put_task(TaskRecord(run_id="r1", status="pending"))
    rounds = 30
    for _ in range(rounds):
        store.update_task("r1", status="running", progress={"iteration": 1})

    def bump_progress() -> None:
        for i in range(20):
            store.update_task("r1", progress={"iteration": i + 1})

    t = threading.Thread(target=bump_progress)
    t.start()
    store.update_task("r1", status="finished")
    t.join()

    rec = store.get_task("r1")
    assert rec is not None
    assert rec.status == "finished", "另一线程写的 status 被覆盖了"
    assert rec.progress == {"iteration": 20}, "进度被覆盖了"


# ------------------------------------------------------------- 视图可迭代
def test_race_view_is_iterable(store) -> None:
    """`len(view)` 与 `list(view)` 必须一致（旧版 race 视图能数不能逛）。"""
    store.put_race(RaceRecord(race_id="a", name="A", run_ids=["r1"], spec_count=1))
    store.put_race(RaceRecord(race_id="b", name="B", run_ids=["r2"], spec_count=1))
    view = make_view(store, "race")
    assert len(view) == 2
    assert sorted(view) == ["a", "b"], "race 视图迭代为空：能数不能逛"
    assert "a" in view


def test_task_view_matches_its_own_len(store) -> None:
    for i in range(2):
        store.put_task(TaskRecord(run_id=f"t{i}"))
    view = make_view(store, "task")
    assert len(view) == len(list(view)) == 2
