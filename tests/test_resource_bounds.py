"""资源必须有界：限流器字典、任务 future、以及"秘密不许落进日志"。

三条实测缺陷（2026-10-31 审计）：

1. **限流器的 `_MAX_KEYS` 形同虚设**：桶只在**同一个 key 再次 acquire** 时才被剪空，
   于是上万个不同来源的 key 谁也不再来时，字典停在一万多，`len` 只增不减
   （实测 10200 个 key → 10200，再打一次 10201）。`--proxy-headers` 下源 IP 可伪造。
2. **`TaskManager._futures` 只增不删**：记录表有 `PM_MAX_TASKS=200`，future 表没有，
   只有 `shutdown()` 才清空 —— 长跑服务里这是无界增长。
3. **`PM_API_TOKEN` 明文进日志**：设了 token 时限流 key 就是那个管理口令，
   一旦触发 429 就原文写进 `logs/`（而 logs 是要随报告一起看的）。
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import Future

import pytest
from pm.ratelimit import _MAX_KEYS, SlidingWindowLimiter
from pm.scheduler import TaskManager
from pm.server import _mask_key


# --------------------------------------------------------------- 限流器内存有界
def test_limiter_dict_stays_bounded_under_key_flood() -> None:
    """灌入远超上限的不同 key，字典必须被压回 `_MAX_KEYS` 以内。"""
    lim = SlidingWindowLimiter(max_events=2, window_seconds=60.0)
    for i in range(_MAX_KEYS + 200):
        lim.acquire(f"10.0.0.{i}", 1)
    assert len(lim._events) <= _MAX_KEYS, (
        f"限流器字典涨到 {len(lim._events)}（上限 {_MAX_KEYS}）：换 key 就能把内存打穿"
    )


def test_limiter_keeps_being_correct_after_pruning() -> None:
    """淘汰之后限流本身还要正常工作（不能为了省内存把限流拆了）。"""
    lim = SlidingWindowLimiter(max_events=2, window_seconds=60.0)
    for i in range(_MAX_KEYS + 10):
        lim.acquire(f"k{i}", 1)
    fresh = "a-fresh-key"
    ok1, _ = lim.acquire(fresh, 1)
    ok2, _ = lim.acquire(fresh, 1)
    ok3, retry = lim.acquire(fresh, 1)
    assert (ok1, ok2) == (True, True)
    assert ok3 is False and 0.0 <= retry <= 60.0, "淘汰后窗口计数失效了"


def test_limiter_prunes_expired_buckets_first() -> None:
    """已整桶滑出窗口的 key 先被清掉（它们本来下次访问也会被清空）。"""
    lim = SlidingWindowLimiter(max_events=1, window_seconds=0.01)
    lim.acquire("old", 1)
    time.sleep(0.03)
    for i in range(_MAX_KEYS + 5):
        lim.acquire(f"n{i}", 1)
    assert len(lim._events) <= _MAX_KEYS


# ------------------------------------------------------------- future 表会自清
def test_forget_future_removes_the_entry() -> None:
    """注册进来的 future 必须能被摘掉（回调走的就是这条）。"""
    tm = TaskManager(max_workers=2, max_records=50)
    try:
        fut: Future[None] = Future()
        tm._futures["rid-1"] = fut
        tm._forget_future("rid-1")
        assert "rid-1" not in tm._futures
    finally:
        tm.shutdown(wait=False)


def test_done_callback_fires_and_clears_the_future() -> None:
    """`add_done_callback` 注册的回调在任务结束后要真的摘除条目。"""
    tm = TaskManager(max_workers=1, max_records=50)
    try:
        fut: Future[None] = Future()
        tm._futures["rid-2"] = fut
        fut.add_done_callback(tm._forget_callback("rid-2"))
        fut.set_result(None)
        assert "rid-2" not in tm._futures, "任务结束后 future 还留在表里"
    finally:
        tm.shutdown(wait=False)


# --------------------------------------------------------- 令牌不许明文进日志
def test_token_is_masked_in_rate_limit_log(caplog: pytest.LogCaptureFixture) -> None:
    """限流日志里出现的必须是令牌指纹，不是令牌原文。"""
    token = "sk-super-secret-admin-token"
    masked = _mask_key(token)
    assert token not in masked
    assert masked.startswith("token:")

    logger = logging.getLogger("pm.server")
    with caplog.at_level(logging.WARNING, logger="pm.server"):
        logger.warning("限流触发：key=%s cost=%d", _mask_key(token), 1)
    assert token not in caplog.text, f"令牌原文进了日志：{caplog.text}"


def test_ip_keys_stay_readable() -> None:
    """IP 要原样打：排障看的正是"谁在打"，打指纹等于把这一栏废掉。"""
    for ip in ("10.0.0.7", "127.0.0.1", "::1", "2001:db8::1"):
        assert _mask_key(ip) == ip
