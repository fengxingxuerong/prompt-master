#!/usr/bin/env python3
"""PromptMaster CLI —— 提示词自动优化闭环。

用法示例：
    # 1) 无 API Key 也能跑：验证图拓扑与控制流（不验证效果）
    python run.py --selftest

    # 1b) 无 Key 走假后端跑完整流程（演示模式，与 server 的 PM_FAKE_BACKEND 一致）
    PM_FAKE_BACKEND=progress python run.py --task "让 AI 分析销售数据"

    # 2) 真实运行
    export PM_API_KEY=sk-xxx
    python run.py --task "让 AI 分析销售数据" --target-model "deepseek-v3"

    # 3) 交互式澄清（需求不清晰时会向你提问）
    python run.py --task "写个 prompt" --interactive

    # 4) 从文件读取需求，输出报告
    python run.py --task-file req.txt --out report.md
"""

# 结构说明（2026-09-25 更新）：CLI 实现已整体下沉到 pm.cli 子包，本文件只保留启动薄壳。
# 启动步骤（顺序是行为的一部分，两步在 prepare_console 里、一步必须留在这里）：
#   1) 仓库根插入 sys.path —— 只能在这里做，因为它得早于 `from pm.bootstrap import ...`；
#   2) load_dotenv() 抢在任何 pm.llm 导入之前（它在模块级读 env）；
#   3) ensure_utf8_stdio() 抢在任何打印之前（Windows 中文机 GBK 码页兜底）。
# 装包后的 `prompt-master` 命令走 pm/cli/console.py，共用第 2、3 步 —— 两个入口两套引导
# 迟早分叉，这仓库已经在别处吃过好几次。
# tests 里 `import run` 仍可取到 assert_mode_arg / recursion_budget 等历史公开名。

from __future__ import annotations

import sys
from pathlib import Path

# 这一行必须排在任何 pm 导入之前：`python run.py` 时 sys.path[0] 恰好是仓库根，
# 但用 runpy/别的目录启动时不是 —— 把它挪进 prepare_console() 会直接
# ModuleNotFoundError: No module named 'pm'（2026-09-25 踩过，被探针用例逮住）。
sys.path.insert(0, str(Path(__file__).parent))

from pm.bootstrap import prepare_console

prepare_console()

import pm.cli as _cli  # noqa: E402

# 历史 API 兼容：tests 与既有脚本可能 `import run` 后直接取用这些名字
EXIT_CONFIG = _cli.EXIT_CONFIG
EXIT_FAILED = _cli.EXIT_FAILED
EXIT_UNDELIVERED = _cli.EXIT_UNDELIVERED
LOG_DIR = _cli.LOG_DIR
assert_mode_arg = _cli.assert_mode_arg
emit_json_result = _cli.emit_json_result
# main 显式从子模块导入：mypy 会把 `_cli.main` 静态解析为同名子模块而报
# "Module not callable"。（pm.cli 包级一度有过 `main`/`selftest` 的惰性导出，与子模块撞名、
# 含义随导入顺序变化，2026-09-25 已移除 —— 见 pm/cli/__init__.py 的文档串。）
from typing import Any  # noqa: E402

from pm.cli.main import main  # noqa: E402

recursion_budget = _cli.recursion_budget
save_artifacts = _cli.save_artifacts
setup_logging = _cli.setup_logging

# run_pipeline / selftest 会拉起 langchain 重依赖链（各约 700ms），且仓库内已无
# 使用者：兼容转发同样惰性化，`from run import run_pipeline` 首次访问时才加载
_LAZY_RUN_EXPORTS: dict[str, tuple[str, str]] = {
    "run_pipeline": ("pm.cli.pipeline", "run_pipeline"),
    "selftest": ("pm.cli.selftest", "selftest"),
}


def __getattr__(name: str) -> Any:
    if name in _LAZY_RUN_EXPORTS:
        import importlib

        module_name, attr = _LAZY_RUN_EXPORTS[name]
        value = getattr(importlib.import_module(module_name), attr)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


if __name__ == "__main__":
    sys.exit(main())
