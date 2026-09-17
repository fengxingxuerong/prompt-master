"""pm.cli —— PromptMaster 命令行实现（run.py 是薄壳入口，这里承载全部逻辑）。"""

from __future__ import annotations

from .agent_mode import agent_subcommand
from .calibrate import _calibrate_command
from .history import _history_command
from .library import _library_command
from .main import main
from .pipeline import run_pipeline
from .selftest import selftest
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
