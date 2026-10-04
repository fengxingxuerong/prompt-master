"""`diff` 子命令：两份提示词 / 两个运行之间的确定性差分（零调用）。

竞品对照（2026-10-02 调研）：PromptLayer 的版本 diff、promptfoo 的 CI 回归门禁
都把"这次改动改了什么"当作一等入口。本项目的 `check` 只看单个时点，
`history` 只看聚合分数，中间那块"相对上一版改了什么"一直空着。

退出码沿用全局协议，但语义针对本命令收窄：
  0 = 无新引入的规则问题（改动是安全的，或只是修好了几条）
  1 = **有新引入**的规则问题（这次改动把已知事故模式带回来了 → 适合当 CI 门禁）
  2 = 参数或来源错误
注意"修好 0 条"不会判 1：diff 不回答"够不够好"，只回答"有没有变坏"——
后者才是有客观判据的那一半。
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from pm.promptdiff import (
    DiffSourceError,
    _clip,
    diff_prompts,
    resolve_source,
)

from .support import _EXIT_PASSED, EXIT_CONFIG, EXIT_UNDELIVERED, setup_logging


def _explain(rule: str) -> str:
    """规则 code 的短释义：CI 日志里只给 code，读的人得去翻源码，等于没解释。"""
    return {
        "meta_leak": "元话语泄漏（把优化流程的话写进了交付物）",
        "context_leak": "任务上下文泄漏（混进了本系统的包装标签）",
        "too_short": "过短（疑似空泛/截断）",
        "constraint_overload": "约束超载（条目越多越执行不到位）",
        "delimiter_unbalanced": "定界符不配平（放大注入面）",
        "suppressive_rule": "抑制型规则（让模型少说/不说）",
        "blanket_missing_branch": "「数据缺失」写成全局兜底（会把已有数据一起判缺失）",
    }.get(rule, rule)


def render_text(delta: dict[str, Any], findings_explain: bool = True) -> str:
    """人读版：先结论（有没有变坏），再逐层细节。"""
    lines: list[str] = []
    lines.append(f"提示词差分：{delta['a']} → {delta['b']}")
    lines.append("")
    if delta["identical"]:
        lines.append("两份正文完全一致（去空白后逐字相同），没有任何改动。")
        return "\n".join(lines)

    rules = delta["rules"]
    if delta["regressed"]:
        extra = sum(len(v) for v in (delta.get("regressed_items") or {}).values())
        lines.append("❌ 本次改动**新引入**了确定性规则问题：")
        for code in rules["introduced"]:
            lines.append(f"  - [{code}] {_explain(code) if findings_explain else ''}".rstrip())
        if extra:
            lines.append(
                f"  - 另有 {extra} 处在**已命中的规则上继续加重**（code 集合没变，但命中项变多）："
            )
            for code, added in (delta.get("regressed_items") or {}).items():
                for item in added:
                    lines.append(f"      · [{code}] 新增「{_clip(item)}」")
    else:
        lines.append("✅ 未新引入任何确定性规则问题。")
    if rules["fixed"]:
        lines.append(f"✅ 修掉 {len(rules['fixed'])} 类规则问题：")
        for code in rules["fixed"]:
            lines.append(f"  - [{code}] {_explain(code) if findings_explain else ''}".rstrip())
    if rules["still"]:
        lines.append(f"⚠️ 两版都还在（本次没动）：{'、'.join(rules['still'])}")
    lines.append("")

    st = delta["structure"]
    c, ch = st["constraints"], st["chars"]
    lines.append("结构变化：")
    lines.append(f"  - 字符数 {ch['before']} → {ch['after']}（{ch['delta']:+d}）")
    lines.append(
        f"  - 约束条目 {c['before']} → {c['after']}（{c['delta']:+d}）"
        + (f"｜{c['sections_before']} → {c['sections_after']}" if c["sections_after"] else "")
    )
    items = st["items"]
    lines.append(f"  - 可逐条核对条目 {items['before']} → {items['after']}")
    for t in items["added"]:
        lines.append(f"      + {t}")
    if items["n_added"] > len(items["added"]):
        lines.append(f"      …（另有 {items['n_added'] - len(items['added'])} 条新增未列出）")
    for t in items["removed"]:
        lines.append(f"      - {t}")
    if items["n_removed"] > len(items["removed"]):
        lines.append(f"      …（另有 {items['n_removed'] - len(items['removed'])} 条删除未列出）")
    lines.append("")

    if delta.get("unified"):
        lines.append("逐行 diff：")
        lines.append("")
        lines.append(delta["unified"])
        lines.append("")
    lines.append(f"> {delta['summary']}")
    if delta["regressed"]:
        lines.append(
            "> 判据只认「新引入」：修好多少条都不构成通过的充分条件，"
            "但新引入一条就意味着这次改动把一个已知事故模式带回来了。"
        )
    return "\n".join(lines)


def diff_command(argv: list[str]) -> int:
    """`run.py diff <A> <B>` 入口。"""
    p = argparse.ArgumentParser(
        prog="run.py diff",
        description="两份提示词（或两个运行）之间的确定性差分：零调用、不联网、秒级",
        epilog="来源写法：文件路径，或 logs/ 里的 run_id。",
    )
    p.add_argument("a", help="左侧：文件路径或 run_id")
    p.add_argument("b", help="右侧：文件路径或 run_id")
    p.add_argument("--json", action="store_true", help="输出 JSON（智能体/CI 消费）")
    p.add_argument(
        "--context",
        type=int,
        default=3,
        help="逐行 diff 的上下文行数（默认 3；0 = 只看增删行）",
    )
    p.add_argument(
        "--no-diff",
        action="store_true",
        help="不输出逐行 diff，只看规则与结构变化（长提示词在 CI 日志里用）",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    setup_logging(args.verbose)

    try:
        src_a = resolve_source(args.a)
        src_b = resolve_source(args.b)
    except DiffSourceError as e:
        p.error(str(e))
        raise SystemExit(EXIT_CONFIG) from e  # argparse.error 不返回，此行给 mypy 兜底

    delta = diff_prompts(
        src_a.text,
        src_b.text,
        a_label=src_a.label,
        b_label=src_b.label,
        context=max(0, args.context),
    )
    if args.no_diff:
        delta["unified"] = ""

    if args.json:
        print(
            json.dumps(
                {"mode": "prompt_diff", "ok": not delta["regressed"], **delta},
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        )
    else:
        print(render_text(delta))
    return EXIT_UNDELIVERED if delta["regressed"] else _EXIT_PASSED
