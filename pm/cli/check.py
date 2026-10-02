"""`check` 子命令：对一份提示词做零调用的静态体检。

竞品（如 prompt-optimizer）的第一入口是"先把我这版提示词分析一遍"，而本产品的
规则闸（`pm/quality.py`）此前只在优化循环内部生效：用户想知道"我这版哪儿不行"
必须先交钱跑完整轮。这里把那把免费的尺子单独递出去——零 LLM、零出网、秒级返回。

它给出的不是"好不好"而是"会不会出事"：每条命中都对应真实事故（见 quality.py 的
注释），所以判据比评委可信。也正因此它不替代优化闭环：静态规则看不出"这份提示词
跑起来能不能完成任务"，那仍然需要用例、评分与基线对比。

退出码沿用全局协议（`EXIT_PASSED`=0 / `EXIT_UNDELIVERED`=1 / `EXIT_CONFIG`=2）：
0 = 无命中，1 = 有命中，2 = 参数错误。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from pm.prompts import WRAPPER_TAGS
from pm.quality import (
    CONSTRAINT_LIMIT,
    MIN_PROMPT_LENGTH,
    check_prompt_quality,
    count_constraints,
    delimiter_problems,
    structure_items,
)

from .support import _EXIT_PASSED, EXIT_CONFIG, EXIT_UNDELIVERED, setup_logging


def collect_findings(text: str) -> list[dict[str, Any]]:
    """确定性规则命中清单：[{code, detail}]，空列表 = 未命中任何一条。"""
    return [{"code": i.code, "detail": i.detail} for i in check_prompt_quality(text).issues]


def summarize(text: str) -> dict[str, Any]:
    """体检读数（只给数字与结构，不下判断）。"""
    counted = count_constraints(text)
    return {
        "chars": len(text.strip()),
        "lines": len([ln for ln in text.splitlines() if ln.strip()]),
        "constraints": counted["total"],
        "constraint_sections": counted["sections"] or "（无约束段）",
        "constraint_limit": CONSTRAINT_LIMIT,
        "min_length": MIN_PROMPT_LENGTH,
        "structure_items": len(structure_items(text)),
        "delimiter_problems": delimiter_problems(text),
        "leak_tags": [t for t in WRAPPER_TAGS if t in text],
    }


def render_text(source: str, findings: list[dict[str, Any]], stats: dict[str, Any]) -> str:
    """人读版输出。命中在前、统计在后——读者先要知道改什么。"""
    lines: list[str] = [
        f"提示词体检：{source}（{stats['chars']} 字符 / {stats['lines']} 行）",
        "",
    ]
    if findings:
        lines.append(f"命中 {len(findings)} 条确定性规则：")
        lines.append("")
        lines.extend(f"{n}. [{f['code']}] {f['detail']}" for n, f in enumerate(findings, 1))
    else:
        lines.append(
            "未命中任何确定性规则。这只说明「实测到会出事的那几类」没踩到，"
            "不说明它能完成任务——后者要用 --task 跑优化闭环（用例 + 评分 + 基线对比）才知道。"
        )
    lines.append("")
    lines.append(
        f"结构统计：约束条目 {stats['constraints']} 条（上限 {stats['constraint_limit']}，"
        f"段落 {stats['constraint_sections']}），可逐条核对的条目 {stats['structure_items']} 条"
    )
    if stats["delimiter_problems"]:
        lines.append(f"定界符问题：{'；'.join(stats['delimiter_problems'])}")
    if stats["leak_tags"]:
        lines.append(f"命中本系统包装标签：{'、'.join(stats['leak_tags'])}")
    return "\n".join(lines)


def _read_target(p: argparse.ArgumentParser, args: argparse.Namespace) -> tuple[str, str]:
    """返回 (正文, 来源标签)。参数不合法时由 argparse 退出（码 2）。"""
    if not args.prompt and not args.prompt_file:
        p.error("需要 --prompt 或 --prompt-file（体检的是你已有的那一版提示词）")
    if args.prompt and args.prompt_file:
        p.error("--prompt 与 --prompt-file 互斥，二选一")
    if args.prompt_file:
        try:
            text = Path(args.prompt_file).read_text(encoding="utf-8")
        except OSError as e:
            p.error(f"--prompt-file 读取失败：{e}")
            raise SystemExit(EXIT_CONFIG) from e  # argparse.error 不会返回，此行为 mypy 兜底
        source = args.prompt_file
    else:
        text = str(args.prompt)
        source = "<--prompt>"
    if not text.strip():
        p.error("待体检的提示词为空")
    return text, source


def check_command(argv: list[str]) -> int:
    """`run.py check` 入口。"""
    p = argparse.ArgumentParser(
        prog="run.py check",
        description="对一份已有提示词做零调用静态体检（不联网、不花 token）",
    )
    p.add_argument("--prompt", help="要体检的提示词正文")
    p.add_argument("--prompt-file", help="从文件读取要体检的提示词")
    p.add_argument("--json", action="store_true", help="输出 JSON（智能体消费）")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    setup_logging(args.verbose)
    text, source = _read_target(p, args)
    findings = collect_findings(text)
    stats = summarize(text)

    if args.json:
        print(
            json.dumps(
                {
                    "mode": "prompt_check",
                    "source": source,
                    "ok": not findings,
                    "findings": findings,
                    **stats,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(render_text(source, findings, stats))
    return _EXIT_PASSED if not findings else EXIT_UNDELIVERED
