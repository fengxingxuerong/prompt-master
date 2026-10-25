"""客户端断流时，流式链路上的上游连接与泵线程必须被收掉。

实测缺陷（2026-10-31 审计）：`_stream_response` 里的 SSE 生成器只在**正常走完**时
才碰上游；客户端断流时 Starlette 会 close 这个生成器，`GeneratorExit` 不是 `Exception`，
于是 `except Exception` 接不住，旧版的实际后果是：

1. 上游 `gen` 从不 close ⇒ 那条 socket 一直挂着；
2. 泵线程一直往**无界** `queue.Queue()` 里塞数据，而下游已经走了，没人消费
   ⇒ 内存随上游产出线性增长。

两条都是"每条断流留一份"，长跑网关下会累积。

测试取的是**同步生成器本体**：把 `StreamingResponse` 换成一个只记参数的壳，
拿到 `_stream_response` 交给它去流的那支生成器，再对它做 `next()/close()` ——
这样测的是真实代码路径，而不是"Starlette 会不会替我们关"这种别人的实现细节。
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from pm.modelhub import server as S
from pm.modelhub.pool import ModelEntry


class _FakeHub:
    """只实现 `_stream_response` 真正用到的那三个方法。"""

    def __init__(self, entry: ModelEntry):
        self._entry = entry
        self.successes = 0
        self.failures: list[str] = []

    def _ordered_enabled(self) -> list[ModelEntry]:
        return [self._entry]

    def _mark_success(self, entry: ModelEntry) -> None:
        self.successes += 1

    def _mark_failure(self, entry: ModelEntry, reason: str) -> None:
        self.failures.append(reason)


def _entry() -> ModelEntry:
    return ModelEntry({"name": "m1", "base_url": "http://127.0.0.1:9/v1", "api_key": "k"})


def _req() -> Any:
    return S.ChatRequest(messages=[{"role": "user", "content": "hi"}], stream=True)


def _capture_stream(monkeypatch: pytest.MonkeyPatch, fake_upstream: Any) -> Any:
    """跑一次 `_stream_response`，返回它交给 StreamingResponse 的**同步生成器**。"""
    captured: dict[str, Any] = {}

    class _Capture:
        def __init__(self, gen: Any, **kwargs: Any):
            captured["gen"] = gen
            captured["kwargs"] = kwargs

    monkeypatch.setattr(S, "stream_upstream", fake_upstream)
    monkeypatch.setattr(S, "StreamingResponse", _Capture)
    S._stream_response(
        _FakeHub(_entry()), _req(), [{"role": "user", "content": "hi"}], None, None, {}
    )
    return captured["gen"]


def test_client_disconnect_closes_the_upstream_generator(monkeypatch: pytest.MonkeyPatch) -> None:
    """关掉下游生成器后，上游生成器的 finally 必须跑到（= socket 被释放）。"""
    closed: list[str] = []

    def fake_stream_upstream(*_a: Any, **_k: Any) -> tuple[str, Any]:
        def gen() -> Any:
            try:
                while True:
                    yield b'data: {"choices":[{"delta":{"content":"x"}}]}\n\n'
            finally:
                closed.append("upstream-closed")

        return "m1", gen()

    it = _capture_stream(monkeypatch, fake_stream_upstream)
    next(it)  # 首帧
    assert closed == [], "还没断流，上游不该提前关"
    it.close()  # 模拟客户端断流
    assert closed == ["upstream-closed"], "客户端断流后上游生成器没被 close（socket 泄漏）"


def test_disconnect_stops_the_pump(monkeypatch: pytest.MonkeyPatch) -> None:
    """断流后泵不能再无限生产（否则线程活着、队列涨着，只是没人看）。"""
    produced = 0
    gate: list[str] = []

    def fake_stream_upstream(*_a: Any, **_k: Any) -> tuple[str, Any]:
        def gen() -> Any:
            nonlocal produced
            try:
                while True:
                    produced += 1
                    yield b'data: {"choices":[{"delta":{"content":"x"}}]}\n\n'
            finally:
                gate.append("closed")

        return "m1", gen()

    it = _capture_stream(monkeypatch, fake_stream_upstream)
    next(it)
    it.close()
    time.sleep(0.05)
    assert gate == ["closed"], "断流后上游仍没被关"
    assert produced < 10_000, "断流后泵仍在把上游读穿"


def test_stream_call_lands_in_the_daily_aggregate(monkeypatch: pytest.MonkeyPatch) -> None:
    """流式调用必须进 `/v1/usage` 的日聚合。

    实测缺陷（2026-10-31 审计）：非流式路径记 `record_call`，流式这条路
    只写台账事件 —— 于是日聚合**漏掉全部流式调用**。台账（事实源）里有、
    看板（日聚合）里没有，这种"两个读数各说一套"比缺数据更难查：
    有人对着看板核对账单，会平白少算一半。
    """
    from pm.modelhub.usage_store import load_daily

    frames = [
        b'data: {"choices":[{"delta":{"content":"a"}}]}\n\n',
        b'data: {"choices":[{"delta":{"content":"b"}}]}\n\n',
    ]

    def fake_stream_upstream(*_a: Any, **_k: Any) -> tuple[str, Any]:
        def gen() -> Any:
            yield from frames

        return "m1", gen()

    it = _capture_stream(monkeypatch, fake_stream_upstream)
    for _ in it:  # 正常跑完
        pass

    daily = load_daily()
    # 形状：{日: {calls, success, by_model: {模型: {...}}, ...}}
    models = {m for day in daily.values() for m in (day.get("by_model") or {})}
    assert "m1" in models, f"流式调用没进日聚合（只进了台账）：{daily}"
    assert sum(int(d.get("calls", 0)) for d in daily.values()) >= 1


def test_pump_stops_when_downstream_is_already_gone() -> None:
    """下游已走（旗标已置）时，泵第一轮就该退出，不许继续读上游。

    这条与下一条覆盖的是泵里**两个"停"的分支**——覆盖率实测它们原本是未覆盖的
    （`pm/modelhub/server.py` 320、323-326），而它们恰恰是本轮修复的全部内容：
    只测"正常读完"等于没测这次改动。
    """
    produced = 0

    def upstream() -> Any:
        nonlocal produced
        for _ in range(10_000):
            produced += 1
            yield b"x"

    q: Any = S.queue.Queue(maxsize=4)
    closed = S.threading.Event()
    closed.set()  # 下游已经走了
    S._pump_frames(upstream(), q, closed)
    assert produced == 1, f"旗标已置仍继续读上游（读了 {produced} 帧）"
    assert q.get_nowait() is None, "收尾的 None 哨兵丢了"


def test_pump_gives_up_when_the_queue_is_full() -> None:
    """队列满（下游不消费）时，泵必须放弃并置旗标 —— 这是"不涨内存"那条的实现。"""
    q: Any = S.queue.Queue(maxsize=1)
    q.put_nowait(b"occupied")  # 先把队列占满
    closed = S.threading.Event()

    def upstream() -> Any:
        yield b"never-consumed"

    S._pump_frames(upstream(), q, closed)
    assert closed.is_set(), "队列满了却没置旗标：泵会一直空转/堆积"
    assert q.get_nowait() == b"occupied"


def test_pump_queue_is_bounded() -> None:
    """队列必须有界：无界 Queue 在下游不消费时会一直吃内存。

    判据落在**泵的实现**上（`_open_frame_pump` / `_pump_frames`）而不是
    `_stream_response` 里 —— 2026-10-31 把泵搬成独立函数（复杂度棘轮不接受
    主函数涨到 31），结构判据也要跟着搬到它真正住的地方。
    """
    import inspect

    src = inspect.getsource(S._open_frame_pump)
    assert "queue.Queue(maxsize=" in src, "泵队列退回了无界写法"
    assert "closed = threading.Event()" in src, "断流旗标没了：泵会一直生产"
    # 泵必须"下游走了就停"，否则有界队列也只是把内存增长换成线程空转
    pump_src = inspect.getsource(S._pump_frames)
    assert "closed.is_set()" in pump_src
    assert "queue.Full" in pump_src, "队列满了没有退出路径"
