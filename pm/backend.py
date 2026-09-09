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
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
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
