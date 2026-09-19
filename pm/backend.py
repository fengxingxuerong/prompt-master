"""
可注入的调用钩子：给演示模式与测试提供**任务级隔离**的 LLM 后端。

为什么需要它（对应审查结论 C4 / H4）：
旧版 `pm/scheduler.py` 用 `unittest.mock.patch` 在**进程级**替换 `pm.nodes.structured_call`
/ `pm.nodes.plain_call`，并按任务 start/stop。并发跑多个任务时会出三类事故：

1. 先结束的任务 `stop()` 把补丁摘掉，同批仍在跑的任务突然改打真实端点（无 Key 直接 failed）；
2. 后启动的 patcher 把"已被打上的 mock"记成原函数，停止顺序不确定 → mock 永久残留，
   此后该进程所有请求（哪怕配了真实 Key）都被静默喂假数据并返回 passed；
3. 演示模式顺手改的 `os.environ["PM_*_CACHE"]="0"` 只改不还原，缓存被永久关闭。

ContextVar 的作用域是「当前线程 / 当前异步任务」：注入与撤销只影响自己，
不改动任何模块属性，因此不存在"谁把谁的补丁摘掉"这类竞态。
但**线程池工作线程不会继承提交者的上下文**，所以需要下面的 `carry_context`；
这也是旧版注释里没写明白的一点（对应审查结论 C5）。
"""

from __future__ import annotations

import functools
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CallHook:
    """替换一次 LLM 调用的钩子。签名与 `pm.llm.structured_call` / `plain_call` 保持一致。"""

    structured: Callable[..., tuple[Any, dict[str, Any]]] | None = None
    plain: Callable[..., tuple[str, dict[str, Any]]] | None = None
    # 钩子生效期间禁用本地缓存：演示/自测不应污染 logs/*_cache.json，
    # 也不必再去改 os.environ（那是进程级副作用）。
    disable_cache: bool = False


_VAR: ContextVar[CallHook | None] = ContextVar("pm_call_hook", default=None)


def current() -> CallHook | None:
    """当前上下文生效的钩子（无则走真实 LLM 通道）。"""
    return _VAR.get()


@contextmanager
def use(hook: CallHook | None) -> Iterator[None]:
    token = _VAR.set(hook)
    try:
        yield
    finally:
        _VAR.reset(token)


def cache_disabled() -> bool:
    """当前上下文是否要求禁用本地缓存。"""
    hook = _VAR.get()
    return bool(hook is not None and hook.disable_cache)


@contextmanager
def with_cache_disabled() -> Iterator[None]:
    """在**当前钩子不变**的前提下追加"禁缓存"。

    为什么不直接 `use(CallHook(disable_cache=True))`：`use` 是整体替换 `_VAR`，
    在 `pm.testing.scope()` 里面那样做会把假后端的 structured/plain 一起丢掉，
    表现是"自测忽然开始打真实端点"——比缓存污染严重得多。复现性测量必须绕缓存
    （否则同一 prompt 第二次永远命中，测出来的极差恒为 0，是自欺），
    但它恰好是最需要在假后端里被测试的功能，所以合成而不是替换。
    """
    cur = _VAR.get()
    with use(
        CallHook(
            structured=cur.structured if cur is not None else None,
            plain=cur.plain if cur is not None else None,
            disable_cache=True,
        )
    ):
        yield


def carry_context(fn: Callable[..., Any]) -> Callable[..., Any]:
    """把提交侧线程的 ContextVar 快照带进线程池工作线程。

    为什么需要：`ThreadPoolExecutor` 的工作线程不继承提交者的上下文（只有
    `asyncio.to_thread` 会）。并发分支一旦走线程池，`use(hook)` 注入的假后端、
    `disable_cache` 开关、以及 `pm.testing` 的运行态计数上下文全都会消失，表现是：

    - 测试与 `PM_FAKE_BACKEND` 演示模式里，目标模型调用绕过假实现去碰真实端点
      （没配 Key 整批 failed，“无 Key 也能跑”的卖点直接属伪；配了 Key 则测试静默烧真实额度）；
    - 钩子里的 `disable_cache` 在并发分支失效，演示跑会污染 `logs/*_cache.json`。

    用法：`ex.map(carry_context(worker), items)` —— 必须在提交侧（主线程）包装。
    每个任务用一份独立快照：同一个 `Context` 不允许被两个线程同时 enter。
    """
    parent = copy_context()
    carried = [(var, parent[var]) for var in parent]

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        def _run() -> Any:
            for var, value in carried:
                if var.get(None) is not value:
                    var.set(value)
            return fn(*args, **kwargs)

        return copy_context().run(_run)

    return wrapper
