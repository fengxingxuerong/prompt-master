"""pm.cli —— PromptMaster 命令行实现（run.py / `prompt-master` 是入口薄壳，这里承载全部逻辑）。

这里**刻意不做** PEP 562 惰性导出 `main` / `selftest` / `run_pipeline`（2026-09-25 移除）。

原因：`main`、`selftest` 与同名子模块 `pm.cli.main` / `pm.cli.selftest` 撞名。
惰性 `__getattr__` 会把**函数**写进包的 `__dict__`，而 import 系统会把**模块**绑到同一个
属性上 —— 谁先发生，`pm.cli.selftest` 就是谁。于是同一个名字有两种含义，测试与调用方
拿到函数还是模块取决于导入顺序：`monkeypatch.setattr("pm.cli.selftest.selftest", ...)`
在有的执行顺序下抛 `'function' object has no attribute 'selftest'`
（tests/test_cli_units.py::test_main_selftest_dispatch 单跑/与 test_cli_guards 同跑时就红）。
一个"看顺序变含义"的属性比没有更糟：它让门禁结果不可复现。

要拿这些实现请用显式路径：`from pm.cli.main import main`、
`from pm.cli.selftest import selftest`、`from pm.cli.pipeline import run_pipeline`。
仓库内所有消费者（含 run.py）本来就写的这种形式，删除惰性导出零影响。
"""

from __future__ import annotations

from typing import Any

from .agent_mode import agent_subcommand
from .calibrate import _calibrate_command
from .history import _history_command
from .library import _library_command
from .support import (
    EXIT_CONFIG,
    EXIT_FAILED,
    EXIT_UNDELIVERED,
    LOG_DIR,
    assert_mode_arg,
    emit_json_result,
    recursion_budget,
    save_artifacts,
    setup_logging,
)

__all__ = [
    "EXIT_CONFIG",
    "EXIT_FAILED",
    "EXIT_UNDELIVERED",
    "LOG_DIR",
    "_calibrate_command",
    "_history_command",
    "_library_command",
    "agent_subcommand",
    "assert_mode_arg",
    "emit_json_result",
    "recursion_budget",
    "save_artifacts",
    "setup_logging",
]


def __getattr__(name: str) -> Any:
    """兜底：误用旧的 `from pm.cli import main/selftest` 时给出可执行的纠正，而不是静默歧义。"""
    if name in {"main", "selftest", "run_pipeline"}:
        module = {"main": "main", "selftest": "selftest", "run_pipeline": "pipeline"}[name]
        raise AttributeError(
            f"`pm.cli.{name}` 是**函数还是子模块**取决于导入顺序，故已移除。"
            f"请改写 `from pm.cli.{module} import {name}`"
            "（原因见本包 __init__ 的文档串）。"
        )
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
