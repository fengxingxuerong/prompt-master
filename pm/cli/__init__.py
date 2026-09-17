"""pm.cli —— PromptMaster 命令行实现（run.py 是薄壳入口，这里承载全部逻辑）。

main / run_pipeline / selftest 三个符号经 pm.graph 拉起 langchain 重依赖链
（单独 import 各需约 700ms）。为让 `python run.py --help`、--dry-run、
history/library 等轻路径不付这笔启动税，三者改为按需加载（PEP 562）：
包级属性首次访问时才 import，其余轻符号保持顶层导出。
"""

from __future__ import annotations

import importlib
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
    "main",
    "recursion_budget",
    "run_pipeline",
    "save_artifacts",
    "selftest",
    "setup_logging",
]

# 惰性符号 → (相对模块名, 属性名)；首次访问后缓存回 globals
_LAZY_EXPORTS: dict[str, tuple[str, str]] = {
    "main": (".main", "main"),
    "run_pipeline": (".pipeline", "run_pipeline"),
    "selftest": (".selftest", "selftest"),
}


def __getattr__(name: str) -> Any:
    if name in _LAZY_EXPORTS:
        module_name, attr = _LAZY_EXPORTS[name]
        value = getattr(importlib.import_module(module_name, __package__), attr)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
