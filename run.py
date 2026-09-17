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

# 结构说明（2026-09-17）：CLI 实现已整体下沉到 pm.cli 子包，本文件只保留启动薄壳。
# 下面三步的**顺序**是行为的一部分，必须原样保留：
#   1) load_dotenv() 抢在任何 pm 导入之前（pm/llm 读 env 的时间点依赖它）；
#   2) 仓库根插入 sys.path（不装包也能 `python run.py` 直跑）；
#   3) ensure_utf8_stdio() 抢在任何打印之前（Windows 中文机 GBK 码页兜底）。
# tests 里 `import run` 仍可取到 assert_mode_arg / recursion_budget 等历史公开名。

from __future__ import annotations

import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, str(Path(__file__).parent))

from pm.bootstrap import ensure_utf8_stdio  # noqa: E402

# Windows 控制台/重定向编码兜底（L12）：任何打印之前先统一为 UTF-8
ensure_utf8_stdio()

import pm.cli as _cli  # noqa: E402

# 历史 API 兼容：tests 与既有脚本可能 `import run` 后直接取用这些名字
EXIT_CONFIG = _cli.EXIT_CONFIG
EXIT_FAILED = _cli.EXIT_FAILED
EXIT_UNDELIVERED = _cli.EXIT_UNDELIVERED
LOG_DIR = _cli.LOG_DIR
assert_mode_arg = _cli.assert_mode_arg
emit_json_result = _cli.emit_json_result
main = _cli.main
recursion_budget = _cli.recursion_budget
run_pipeline = _cli.run_pipeline
save_artifacts = _cli.save_artifacts
selftest = _cli.selftest
setup_logging = _cli.setup_logging

if __name__ == "__main__":
    sys.exit(main())
