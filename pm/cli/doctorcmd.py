"""`run.py doctor`：零 LLM 调用的环境诊断入口。

判据与渲染都在 `pm/doctor.py`；这里只做参数解析与出口约定：
- 人读版：逐项 ✓/⚠/✗ + 角色配置表 + 一句结论；
- `--json`：单个对象（`mode=doctor`），stdout 不混任何人类可读行；
- 退出码 0/1/2 分别对应无问题 / 只有警告 / 有错误。
"""

from __future__ import annotations

import argparse
import json

from pm.doctor import build_report, render_doctor


def doctor_command(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="run.py doctor",
        description="环境诊断：Python / .env / Key / 角色配置 / 日志目录 / 端口 / 常驻服务 / MCP"
        "（零 LLM 调用、不出网）",
    )
    p.add_argument("--json", action="store_true", help="输出单个 JSON 对象（Agent/CI 消费）")
    args = p.parse_args(argv)

    report = build_report()
    if args.json:
        print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
    else:
        print(render_doctor(report))
    return report.exit_code


__all__ = ["doctor_command"]
