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

from pm.casesaudit import DEFAULT_MODE, audit_cases
from pm.prompts import WRAPPER_TAGS
from pm.quality import (
    CONSTRAINT_LIMIT,
    MIN_PROMPT_LENGTH,
    check_prompt_quality,
    count_constraints,
    delimiter_problems,
    structure_items,
)
from pm.textio import TextDecodeError, read_text_auto, warn_if_fallback
from pm.verdict import attach, decide, unvalidated

from .support import (
    _EXIT_PASSED,
    CASES_FILE_HINT,
    EXIT_CONFIG,
    EXIT_UNDELIVERED,
    MAX_SEED_CASES,
    assert_mode_arg,
    case_entry_fields,
    read_cases_file,
    resolve_case_mode,
    setup_logging,
)


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
        target = Path(args.prompt_file)
        if target.is_dir():
            # Windows 上直接读目录报 Permission denied，用户会以为文件权限坏了
            p.error(f"--prompt-file 要的是文件，不是目录：{target}")
        try:
            decoded = read_text_auto(target)
        except OSError as e:
            p.error(f"--prompt-file 读取失败：{e}")
            raise SystemExit(EXIT_CONFIG) from e  # argparse.error 不会返回，此行为 mypy 兜底
        except TextDecodeError as e:
            p.error(f"--prompt-file 解码失败：{e}")
            raise SystemExit(EXIT_CONFIG) from e
        warn_if_fallback(decoded, "--prompt-file")
        text = decoded.text
        source = args.prompt_file
    else:
        text = str(args.prompt)
        source = "<--prompt>"
    if not text.strip():
        p.error("待体检的提示词为空")
    return text, source


def render_cases(source: str, report: dict[str, Any]) -> str:
    """用例集体检的人读输出：先给票数，再给逐条命中，最后划清它能说什么、不能说什么。"""
    stats = report["stats"]
    findings = report["findings"]
    lines: list[str] = [
        f"用例集体检：{source}（{stats['cases']} 条，上限 {stats['max_cases']}）",
        "",
        (
            f"能一票否决的：{stats['veto_capable']} 条"
            f"（其中 {stats['echo_only']} 条属「expected 就在 input 里」，只挡得住丢内容，挡不住复述）"
            f"｜写法会被降级成提醒的：{stats['advisory']} 条"
            f"｜只交评委核验的（rule）：{stats['judge_only']} 条"
            f"｜空 expected 根本不执行的：{stats['no_assertion']} 条"
        ),
        "",
    ]
    if findings:
        lines.append(f"命中 {len(findings)} 条：")
        lines.append("")
        for n, f in enumerate(findings, 1):
            where = f"#{f['case']}" if f.get("case") else "整卷"
            lines.append(f"{n}. [{f['code']}] {where}：{f['detail']}")
    else:
        lines.append("未命中任何结构性问题。")
    lines.append("")
    lines.append(
        "⚠️ 这把尺子说的是「这套考卷有几票能否决」，不是「优化有没有用」。后者只有基线对照一条路"
        "（`run.py --task … --prompt-file 你的原稿`，Δ 才读得出比你自己好多少），"
        "而评委那把尺子目前还没对准所有者的标准（README §三）。"
    )
    strong = stats["veto_strong_cases"]
    if stats["veto_strong"] == 0:
        lines.append(
            "这份考卷里一票否决强判据为 0 条 ⇒ 达标判定将完全由评委印象分决定；"
            "想让它能挡住差输出，至少给一条 expected 是输出侧真会出现的字面片段"
            "（且不要出现在 input 里）。"
        )
    elif strong:
        lines.append(f"强判据落在第 {', '.join(str(i) for i in strong)} 条。")
    return "\n".join(lines)


def _cases_target(p: argparse.ArgumentParser, args: argparse.Namespace) -> dict[str, Any]:
    """`--cases-file` 模式：取值路径与跑批完全同源（`support.case_entry_fields` 与
    `support.resolve_case_mode`），所以"体检说没问题"与"跑起来被拦"不会各说一套。"""
    raw = read_cases_file(p, args.cases_file)
    cases: list[dict[str, Any]] = []
    for i, item in enumerate(raw):
        fields = case_entry_fields(item)
        cases.append(
            {
                "input": fields["input"],
                "expected": fields["expected"],
                "mode": resolve_case_mode(fields["mode_raw"], args.assert_mode, i, p),
                "scenario": fields["scenario"],
            }
        )
    return audit_cases(cases, max_cases=MAX_SEED_CASES)


def _prompt_payload(
    source: str, findings: list[dict[str, Any]], stats: dict[str, Any]
) -> dict[str, Any]:
    """`check --prompt*` 的 JSON 载荷（三态裁决，见 `pm/verdict.py`）。

    这一路**只有一个**可能没核验成的分支：抽查内容为空时早就退 2 了，所以正常情形下
    要么 confirmed（没命中）要么 rejected（命中）。`unvalidated` 留空数组而不是省略键 ——
    省键会让消费方写成 `payload["unvalidated"]` 时 KeyError，而"没有未决事实"
    恰恰是这里最常见的情形，不该考调用方的防御性写法。
    """
    payload: dict[str, Any] = {
        "mode": "prompt_check",
        "source": source,
        "findings": findings,
        **stats,
        "unvalidated": [],
    }
    return attach(payload, decide(rejected=bool(findings), unvalidated_facts=[]))


def _cases_payload(
    source: str | None, findings: list[dict[str, Any]], stats: dict[str, Any]
) -> dict[str, Any]:
    """`check --cases-file` 的 JSON 载荷：**此处是三态真正有用的地方**。

    一张考卷可以"结构上没问题"（findings 为空）却**一票否决强判据为 0 条**
    ——那种考卷挡不住差输出，达标判定将完全由评委印象分决定（`render_cases` 里
    早就有这句话，但 JSON 出口把它吞了：只给 `ok: true`）。
    ⇒ 强判据为 0 记 `no_strong_veto`，一张否决票都没有记 `no_veto_capable`。
    """
    payload: dict[str, Any] = {
        "mode": "cases_check",
        "source": source,
        "findings": findings,
        **stats,
        "unvalidated": [],
    }
    if not findings:
        facts: list[dict[str, str]] = []
        if not stats.get("veto_strong"):
            facts.append(
                unvalidated(
                    "no_strong_veto",
                    "这份考卷里一票否决的强判据为 0 条 ⇒ 达标判定将完全由评委印象分决定",
                )
            )
        if not stats.get("veto_capable"):
            facts.append(
                unvalidated(
                    "no_veto_capable",
                    "这张考卷没有任何一条能一票否决的用例 ⇒ 它挡不住差输出",
                )
            )
        payload["unvalidated"] = facts
    rejected = bool(findings)
    return attach(payload, decide(rejected=rejected, unvalidated_facts=payload["unvalidated"]))


def check_command(argv: list[str]) -> int:
    """`run.py check` 入口。给 `--cases-file` 时体检的是一份用例集，否则是一份提示词。"""
    p = argparse.ArgumentParser(
        prog="run.py check",
        description="零调用静态体检：一份已有提示词，或一份用例集（不联网、不花 token）",
    )
    p.add_argument("--prompt", help="要体检的提示词正文")
    p.add_argument("--prompt-file", help="从文件读取要体检的提示词")
    p.add_argument(
        "--cases-file",
        help="改体检一份用例集（与 --cases-file 跑批用的是同一条加载路径）" + CASES_FILE_HINT,
    )
    p.add_argument(
        "--assert-mode",
        default=DEFAULT_MODE,
        type=assert_mode_arg,
        metavar="MODE",
        help=f"用例集没写 mode 时的缺省（与 run.py 同一份常量与同一个校验器，默认 {DEFAULT_MODE}）",
    )
    p.add_argument("--json", action="store_true", help="输出 JSON（智能体消费）")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    setup_logging(args.verbose)

    if args.cases_file:
        if args.prompt or args.prompt_file:
            p.error(
                "--cases-file 与 --prompt/--prompt-file 互斥：一次体检一样东西，退出码才有单一含义"
            )
        report = _cases_target(p, args)
        findings, stats = report["findings"], report["stats"]
        if args.json:
            print(
                json.dumps(
                    _cases_payload(args.cases_file, findings, stats),
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            print(render_cases(args.cases_file, report))
        return _EXIT_PASSED if not findings else EXIT_UNDELIVERED

    text, source = _read_target(p, args)
    findings = collect_findings(text)
    stats = summarize(text)

    if args.json:
        print(
            json.dumps(
                _prompt_payload(source, findings, stats),
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(render_text(source, findings, stats))
    return _EXIT_PASSED if not findings else EXIT_UNDELIVERED
