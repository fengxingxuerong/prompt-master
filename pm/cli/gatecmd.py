"""`gate` 子命令：提示词改动的回归门禁（零调用、CI 用）。

与 `diff` 的分工：
- `diff A B` 是**通用**差分工具（比两份任意正文）；
- `gate` 是**专用**门禁（固定检查仓库里的提示词资产、自动从 git 取基线、
  基线缺失时显式吵而不是静默放行）。

退出码协议（沿用全局口径，语义针对本命令收窄）：
  0 = 没有新引入的规则问题（放行）
  1 = 有新引入的规则问题（拦截；这是 CI 该红的那一半）
  2 = 门禁自己没跑起来（拿不到基线 / 抽取不到模板 / 对象配置有误）
  3 = 客观上没有基线可对比（首次推送）—— 默认按放行处理，但会打横幅说明；
      配 `--require-baseline` 时按 2 处理（要求严格门禁的场景）
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from pm.promptgate import DEFAULT_PATHS, GateError, gate, render_text

from .support import _EXIT_PASSED, EXIT_CONFIG, EXIT_UNDELIVERED, setup_logging


def gate_command(argv: list[str]) -> int:
    """`run.py gate` 入口。"""
    p = argparse.ArgumentParser(
        prog="run.py gate",
        description="提示词改动的回归门禁：只拦本次改动新引入的规则问题（零调用）",
    )
    p.add_argument(
        "--base",
        default=None,
        help="基线 ref（默认自动探测 origin/main → origin/master → main → master → HEAD~1）；"
        "CI 里传 github.event.before 或 PR 的 base",
    )
    p.add_argument(
        "--path",
        action="append",
        default=None,
        help=f"检查对象（可重复；默认 {'、'.join(DEFAULT_PATHS)}）",
    )
    p.add_argument("--json", action="store_true", help="输出 JSON（CI/智能体消费）")
    p.add_argument(
        "--require-baseline",
        action="store_true",
        help="没有基线可对比时判为失败（默认按放行并打横幅说明）",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    setup_logging(args.verbose)
    paths = tuple(args.path) if args.path else DEFAULT_PATHS

    try:
        result: dict[str, Any] = gate(base_ref=args.base, paths=paths, root=Path.cwd())
    except GateError as e:
        # 门禁自己坏了：必须与"改动有问题"分开，退出码也分开（2 不是 1）
        if args.json:
            print(
                json.dumps(
                    {
                        "mode": "prompt_gate",
                        "ok": False,
                        "gate_error": str(e),
                        "hint": "这是门禁自身没能执行，不是「提示词有回归」；先修 CI 配置或 --base",
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            print(f"❌ 门禁未能执行：{e}")
            print("   注意：这是门禁自己坏了，不是「提示词有回归」。")
        return EXIT_CONFIG

    if args.json:
        payload = {**result, "require_baseline": args.require_baseline}
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        print(render_text(result))

    if result["skipped"]:
        # 没有基线：默认放行但绝不假装这是"检查通过"（render_text 已写明）
        return EXIT_CONFIG if args.require_baseline else _EXIT_PASSED
    return EXIT_UNDELIVERED if result["regressed"] else _EXIT_PASSED
