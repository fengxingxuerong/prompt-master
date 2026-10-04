"""提示词/运行差分：两份交付之间"到底改了什么"（零调用、确定性）。

竞品依据（2026-10-02 调研，见 docs/iteration/competitor-analysis-prompt.md）：
提示词管理类竞品的**第一入口都是版本对比**——PromptLayer 的 git 式版本 diff、
PromptHub 的 A/B 变体、Braintrust 的实验对比、promptfoo 的 CI 回归门禁，
共同点是把"一次改动"变成**可核对的差异**，而不是"我觉得这版读起来更好"。
本项目此前缺这一层：
- `check` 回答"这一版哪儿不行"（单个时点的静态命中）；
- `history` 回答"跨 run 分数怎么变"（聚合统计）；
- 但没人回答"这一版相对上一版**改了什么、修掉几条规则问题、又新引入几条**"。
改 prompt 的人真正需要的就是这个，否则只能靠肉眼看两份长文。

为什么必须零调用：版本对比要能在 CI 里**每次提交都跑**，任何一次 LLM 调用都会
让它退化成"偶尔跑一次"。所以全部判据都取自 `pm.quality`（与 `check` 同源）与
`pm.scoring` 的结构解析——**同一个 prompt 在两条路径上得出不同结论**才是更大的问题，
`tests/test_promptdiff.py` 对此有交叉断言。

产物分三层，各自回答一个问题：
1. `rule_delta`——规则命中层面：这次改动**修好**了哪些、**新引入**了哪些（可直接当 CI 门禁）；
2. `structure_delta`——结构层面：约束条目、标签段、可核对条目的增删；
3. `unified`——文本层面：逐行 diff（人读的最后一公里）。
"""

from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .quality import check_prompt_quality, count_constraints, delimiter_problems, structure_items

# 单条增删条目在报告里的展示上限：diff 是给人扫的，不是把两份提示词又贴一遍
_ITEM_CLIP = 100
# 结构层面最多各列多少条增删（超出只报计数）
_ITEM_SHOW = 12


class DiffSourceError(Exception):
    """来源无法解析（路径不存在、run_id 查无此运行、正文为空）。"""


@dataclass(frozen=True)
class DiffSource:
    """一个可对比的来源：标签（显示用）+ 正文。"""

    label: str
    text: str
    kind: str  # "file" | "run" | "text"


def _clip(text: str, limit: int = _ITEM_CLIP) -> str:
    flat = " ".join((text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def resolve_source(spec: str, log_dir: Path | None = None) -> DiffSource:
    """把 `--a/--b` 的一侧解析成正文：先当文件，再当 run_id。

    顺序是有意的：文件路径是**用户显式指向的东西**，而 run_id 是短十六进制串，
    两者极少真的相撞；万一相撞（本地真有个叫 `abc123` 的文件），
    用户想对比的多半就是那个文件，run 还能用完整路径或 `logs/run_x.json` 指。
    """
    raw = (spec or "").strip()
    if not raw:
        raise DiffSourceError("来源为空")

    path = Path(raw)
    if path.exists() and path.is_file():
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as e:  # 存在但读不了（权限/占用）与不存在要分开说
            raise DiffSourceError(f"{raw} 读取失败：{e}") from e
        if not text.strip():
            raise DiffSourceError(f"{raw} 是空文件，没有可对比的正文")
        return DiffSource(label=raw, text=text, kind="file")

    run = _load_run_prompt(raw, log_dir)
    if run is not None:
        return run

    raise DiffSourceError(
        f"{raw} 既不是可读文件，也不是已知的 run_id。"
        "给文件路径，或给 logs/ 里某个运行的 run_id（如 `run.py diff a1b2c3d4e5f6 x.md`）"
    )


def _load_run_prompt(run_id: str, log_dir: Path | None) -> DiffSource | None:
    """按 run_id 取该次运行的最终交付提示词。"""
    from .cli.support import log_dir as _default_log_dir

    d = log_dir or _default_log_dir()
    for name in (f"run_{run_id}.json", f"{run_id}.json"):
        f = d / name
        if not f.exists():
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            raise DiffSourceError(f"运行记录 {f.name} 读不出来：{e}") from e
        prompt = str(data.get("prompt") or "")
        if not prompt.strip():
            # 明确区分"没这个 run"与"这个 run 没产出提示词"：后者是真实故障现场
            raise DiffSourceError(
                f"运行 {run_id} 没有交付提示词（status={data.get('status')}）——"
                "该轮可能失败在优化阶段之前，没有可对比的正文"
            )
        return DiffSource(label=f"run:{run_id}", text=prompt, kind="run")
    return None


def rule_findings(text: str) -> set[str]:
    """该正文命中的规则 code 集合（与 `check` 子命令同源，不另写一套判据）。"""
    return {i.code for i in check_prompt_quality(text).issues}


# 引号内的"引用"：把 `不写"停止处理"` 这类**禁止示例列举**与真正的抑制条款区分开。
# 为什么必须区分（2026-10-02 实测）：OPTIMIZER_SYSTEM 里就写着
# 「不写"停止处理""不输出任何结论"……」，这是**教模型别写**这类条款的正面规则，
# 却被 `_SUPPRESSIVE_RULE_RE` 字面命中成一堆 suppressive_rule，
# 于是"在这个模板上再加一条真的抑制条款"在 code 级完全看不出来（集合前后一样）——
# 门禁恰好对它最该管的那个模板失效。
_QUOTED = re.compile(r"「[^」]*」|『[^』]*』|《[^》]*》|“[^”]*”|‘[^’]*’|\"[^\"]*\"|'[^']*'")


def _effective_suppressive(text: str) -> set[str]:
    """只保留**非引用**语境下的抑制式措辞（与实际条款语义相近的那个口径）。"""
    from .quality import _SUPPRESSIVE_RULE_RE

    stripped = _QUOTED.sub(" ", text or "")
    return set(_SUPPRESSIVE_RULE_RE.findall(stripped))


def rule_signals(text: str) -> dict[str, set[str]]:
    """对有「可枚举命中项」的规则，返回 code -> 命中项集合。

    为什么需要它（实测盲区，2026-10-02 变异测试）：只比 code 集合时，
    **某条规则已经命中的情况下，改动让它命中得更多是检不出来的**。
    真实场景：`OPTIMIZER_SYSTEM` 本来就含抑制式措辞（因此 baseline 已命中
    `suppressive_rule`），这次又在里面加了一条「若输入异常则停止处理，不输出任何结论」——
    code 集合前后都是 `{suppressive_rule}`，差集为空 → 判为"无回归"。
    这门禁恰好对**最常被修改的那几个模板**失效，必须补上。

    suppressive_rule 走 `_effective_suppressive`（剥引用），否则基线的假阳性
    会让两侧命中项完全相同、肉眼可见的差别反而测不出来。

    刻意只覆盖有确定性枚举语义的规则，不做语义猜测：
    - suppressive_rule / meta_leak / context_leak：命中词本身
    - delimiter_unbalanced：具体的不配平描述
    - blanket_missing_branch / too_short / constraint_overload：是布尔或计数语义，
      没有"命中项"概念，交给 code 通道与结构通道（约束条目增删）负责。
    """
    from . import quality

    text = text or ""
    sig: dict[str, set[str]] = {}

    suppressive = _effective_suppressive(text)
    if suppressive:
        sig["suppressive_rule"] = suppressive

    # meta_leak：强特征直接判；弱特征要组合命中才判（口径与 check 完全一致）
    strong = {m for m in quality.STRONG_META_MARKERS if m in text}
    weak = {m for m in quality.WEAK_META_MARKERS if m in text}
    if strong:
        sig["meta_leak"] = strong
    elif len(weak) >= quality.WEAK_COMBO_MIN:
        sig["meta_leak"] = weak

    leaked = {t for t in quality.LEAK_TAGS if t in text}
    if leaked:
        sig["context_leak"] = leaked

    delim = set(quality.delimiter_problems(text))
    if delim:
        sig["delimiter_unbalanced"] = delim

    return sig


def rule_delta(before: str, after: str) -> dict[str, list[str]]:
    """规则层面的得失：`fixed` = 改掉了，`introduced` = 这次改动新引入的。

    只比 code 不比 detail 文本：detail 里带数字（条数、字符数），
    改一条约束就会让两边的 detail 同时变化，按文本比会把"修好一条"报成"又坏一条"。
    但**code 相同不代表命中项相同** —— 那条通道由 `_items_delta` 补上。
    """
    b, a = rule_findings(before), rule_findings(after)
    return {"fixed": sorted(b - a), "introduced": sorted(a - b), "still": sorted(a & b)}


def _items_delta(before: str, after: str) -> dict[str, dict[str, list[str]]]:
    """同一 code 下"命中项"的增删。

    这是 code 通道的补充，不是替代：code 级"修好/新引入"仍然是主判据，
    它负责抓住"已命中规则上继续加重"这一类（code 集合看不出来的）回归。
    """
    sb, sa = rule_signals(before), rule_signals(after)
    out: dict[str, dict[str, list[str]]] = {}
    for code in sorted(set(sb) | set(sa)):
        added = (sa.get(code) or set()) - (sb.get(code) or set())
        removed = (sb.get(code) or set()) - (sa.get(code) or set())
        if added or removed:
            out[code] = {"added": sorted(added), "removed": sorted(removed)}
    return out


def _sections(text: str) -> list[tuple[str, str]]:
    """摊平成 (段名, 条目) —— 复用 scoring 的解析，保证"能逐条核对"的口径一致。"""
    return structure_items(text)


def structure_delta(before: str, after: str) -> dict[str, Any]:
    """结构层面的增删：约束预算、标签段、可核对条目。"""
    cb, ca = count_constraints(before), count_constraints(after)
    items_b, items_a = _sections(before), _sections(after)
    sec_b = {s for s, _ in items_b}
    sec_a = {s for s, _ in items_a}

    texts_b = {t for _, t in items_b}
    texts_a = {t for _, t in items_a}
    added = [t for t in (texts_a - texts_b) if t.strip()]
    removed = [t for t in (texts_b - texts_a) if t.strip()]

    return {
        "chars": {
            "before": len(before.strip()),
            "after": len(after.strip()),
            "delta": len(after.strip()) - len(before.strip()),
        },
        "constraints": {
            "before": cb["total"],
            "after": ca["total"],
            "delta": ca["total"] - cb["total"],
            "sections_before": cb["sections"],
            "sections_after": ca["sections"],
        },
        "sections": {
            "added": sorted(sec_a - sec_b),
            "removed": sorted(sec_b - sec_a),
            "common": sorted(sec_a & sec_b),
        },
        "items": {
            "before": len(texts_b),
            "after": len(texts_a),
            "added": [_clip(t) for t in added[:_ITEM_SHOW]],
            "removed": [_clip(t) for t in removed[:_ITEM_SHOW]],
            "n_added": len(added),
            "n_removed": len(removed),
        },
        "delimiters_before": delimiter_problems(before),
        "delimiters_after": delimiter_problems(after),
    }


def unified_diff(
    before: str,
    after: str,
    from_label: str = "before",
    to_label: str = "after",
    context: int = 3,
) -> str:
    """逐行 unified diff（人读的最后一公里）。"""
    lines = difflib.unified_diff(
        (before or "").splitlines(),
        (after or "").splitlines(),
        fromfile=from_label,
        tofile=to_label,
        lineterm="",
        n=max(0, int(context)),
    )
    return "\n".join(lines)


def summarize_change(delta: dict[str, Any]) -> str:
    """一句话概括这次改动（给报告与 CI 日志用，不下"变好了"的判断）。

    注意最后那条兜底：正文确实改了、但规则与结构都没动时，**必须说"改了但没触发规则"**，
    而不是含糊地说"规则集没变化"——后者会被读成"什么都没改"，
    而用户明明改了措辞（措辞变化没有确定性判据，这是本模块的已知边界，要写出来而不是藏起来）。
    """
    fixed = delta["rules"]["fixed"]
    introduced = delta["rules"]["introduced"]
    parts: list[str] = []
    if fixed:
        parts.append(f"修掉 {len(fixed)} 类规则问题")
    if introduced:
        parts.append(f"新引入 {len(introduced)} 类规则问题")
    rewritten = delta.get("regressed_items") or {}
    if rewritten:
        n = sum(len(v) for v in rewritten.values())
        parts.append(f"已命中的规则上又新增 {n} 处命中项")
    c = delta["structure"]["constraints"]
    if c["delta"]:
        parts.append(f"约束条目 {c['before']}→{c['after']}")
    if not parts:
        if delta.get("identical"):
            parts.append("两份正文一致")
        else:
            parts.append("正文有改动，但未触发任何确定性规则、约束条目数也未变")
    return "；".join(parts)


def diff_prompts(
    before: str, after: str, *, a_label: str = "before", b_label: str = "after", context: int = 3
) -> dict[str, Any]:
    """完整差分结果（JSON 可序列化，供 CLI 的 `--json` 与 CI 消费）。"""
    rules = rule_delta(before, after)
    items = _items_delta(before, after)
    delta: dict[str, Any] = {
        "rules": rules,
        # 同一 code 下命中项的增删：补上"已命中规则上继续加重"这个 code 通道看不见的盲区
        "rule_items": items,
        "structure": structure_delta(before, after),
        "a": a_label,
        "b": b_label,
        "identical": (before or "").strip() == (after or "").strip(),
        "unified": unified_diff(before, after, a_label, b_label, context),
    }
    # 回归判定 = code 级新引入 ∪ 已命中规则上的命中项新增。
    # 后者是一类会被随手做出来的改动（"再加一条不行就停"），
    # 而它在这条判据之前是完全隐形的。
    regressed_items = {
        c: v["added"] for c, v in items.items() if v["added"] and c in rules["still"]
    }
    delta["regressed_items"] = regressed_items
    delta["summary"] = summarize_change(delta)
    delta["regressed"] = bool(rules["introduced"]) or bool(regressed_items)
    return delta
