"""用例集的零调用体检：算"这套考卷有几票能挡住差输出"，不是"优化有没有用"。

为什么要单独一把免费的尺子（2026-10-13）：产品自己已经在报告里承认"用例区分度不足"
（`pm/report.py` 的 `BASELINE_DISCRIMINATION_FLOOR`：基线均分 ≥8.0 就告警），但那条话
**只有在你已经花掉 34~57 次真实调用 / 34~67 分钟之后**才看得见。实测两份原稿改进轮
（`logs/report_59ef9e45e3a5.md` Δ+0.0、`logs/report_e3312b5ad4e6.md` Δ−0.35）都是这样
烧完才说"连原始需求都能拿高分"。这里把那笔账提前到零调用：先数清考卷的否决票数，
再决定要不要为它花钱。

它说的是什么、不是什么：
- 是：结构性事实。每条判据都**与既有执行/预检路径同源**，不新造阈值 ——
  `pm.assertions.check_assertion` 对空 expected 与 `rule` 返回 None（= 不计入否决）、
  对 contains/exact 写成需求规则降级为提醒、`run.py --cases-file` 的条数上限、
  `pm/nodes/execute.py` 的场景覆盖校验（n≥3 才要求注入）。
- 不是：效果证明。否决票数多 ≠ 优化有用；那仍然只有"基线臂 vs 优化臂"一条路，
  而那把尺子自己还没收敛（README §三：AUC 0.623、CI 覆盖 0.5、bias +3.09）。

⚠️ 一条刻意不算"写错"的判据：`contains` 的 expected 就在本条 input 里（数据里写着"华东"，
断言也要求输出含"华东"）。这类是**内容保留性断言**，归档记着它当初就是这么选的
（`docs/archive/e2e_iteration_log_2026-09-13.md`：case#0 → 「GLM-5」"内容保留性断言"）。
所以这里只说"它测不出任务质量、复读输入即通过"，不说它不该存在。
"""

from __future__ import annotations

import re
from typing import Any

from pm.assertions import RULE_MODE, _is_literal_flag, looks_like_rule

# 与 run.py 的 --assert-mode 缺省值一致（那条 help 写的就是"默认 contains"）。
DEFAULT_MODE = "contains"
# 只有这两种模式存在"expected 该是字面片段还是需求规则"的问题。
LITERAL_MODES = ("contains", "exact")
VALID_MODES = ("exact", "contains", "regex", RULE_MODE)

# 场景词表与 execute.py 的 mockgen 覆盖校验同源（那里是闭包，这里是它调用的纯函数）。
SCENARIO_MAIN_PATH = "main_path"
SCENARIO_BOUNDARY = "boundary"
SCENARIO_INJECTION = "injection"
INJECTION_MIN_CASES = 3  # n≥3 才要求注入那一格：两条用例时不该强要


def mode_of(case: dict[str, Any]) -> str:
    """这条用例实际会跑的断言模式。

    2026-10-31 补 `assert_mode` 回退：运行时（`pm/nodes/execute.py::_mode_for`、
    `pm/cli/support.py::case_entry_fields`）读的是 `mode or assert_mode or 缺省`，
    而这里只读 `mode`。于是同一份 JSON 换成 `"assert_mode"` 键就变成两套结论：
    一条 `{"assert_mode": "custom:x", "expected": ""}` 会被体检报成
    "no_assertion（白占一个用例位）"，而它跑起来其实是执行的；
    `{"assert_mode": "rule"}` 会被算成 contains，judge_only 四个桶全数错。
    """
    return str(case.get("mode") or case.get("assert_mode") or DEFAULT_MODE)


def mode_is_valid(mode: str) -> bool:
    """是否在四种基础模式或 custom:<name> 里（custom 是否已注册不在这里判 —— 那要跑起来才知道）。"""
    return mode in VALID_MODES or mode.startswith("custom:")


def has_assertion(case: dict[str, Any]) -> bool:
    """这条有没有一票否决的资格。判据与 `check_assertion` 的 None 分支逐条对齐：
    `rule` 永远走评委；非 custom 模式的空 expected 永远不计数；custom 由函数自己定。"""
    mode = mode_of(case)
    if mode == RULE_MODE:
        return False
    if mode.startswith("custom:"):
        return True
    return bool(str(case.get("expected") or "").strip())


def is_advisory(case: dict[str, Any]) -> bool:
    """contains/exact 的 expected 读起来像需求规则 ⇒ 执行时被降级成提醒，不计入否决。

    `"literal": true` 的用例不算（2026-10-31）：那是用户显式声明"这就是输出里会出现
    的原话"，运行时照旧逐字比对、照旧有一票否决权。体检必须与运行时同一口径，
    否则会出现"体检说这条没有否决权、跑起来它却真的否决了"。
    """
    if mode_of(case) not in LITERAL_MODES:
        return False
    raw = case.get("literal") or case.get("expected_is_literal")
    if _is_literal_flag(raw):
        return False
    return looks_like_rule(str(case.get("expected") or ""))


def echoes_input(case: dict[str, Any]) -> bool:
    """字面比对的 expected 就出现在本条 input 里 ⇒ 复读输入的输也能过。"""
    if mode_of(case) not in LITERAL_MODES:
        return False
    exp = str(case.get("expected") or "").strip()
    return bool(exp) and exp in str(case.get("input") or "")


def _normalized(text: str) -> str:
    return re.sub(r"\s+", "", str(text or "")).casefold()


def missing_scenarios(kinds: list[str], n: int) -> list[str]:
    """场景覆盖缺口（判据与 `pm/nodes/execute.py::_has_coverage` 同口径，由它反向调用）。"""
    lowered = {str(k).strip().lower() for k in kinds if str(k).strip()}
    missing: list[str] = []
    if SCENARIO_MAIN_PATH not in lowered:
        missing.append(f"{SCENARIO_MAIN_PATH}（典型主路径）")
    if SCENARIO_BOUNDARY not in lowered:
        missing.append(f"{SCENARIO_BOUNDARY}（边界/歧义）")
    if n >= INJECTION_MIN_CASES and SCENARIO_INJECTION not in lowered:
        missing.append(f"{SCENARIO_INJECTION}（提示词注入，指令混在正常数据里）")
    return missing


def _finding(code: str, detail: str, case: int | None = None) -> dict[str, Any]:
    return {"code": code, "case": case, "detail": detail}


def _is_hollow(case: dict[str, Any]) -> bool:
    """非 custom、非 rule，而 expected 是空的 —— 执行时 `check_assertion` 直接返回 None 的那一类。"""
    mode = mode_of(case)
    if mode == RULE_MODE or mode.startswith("custom:"):
        return False
    return not str(case.get("expected") or "").strip()


def _case_findings(index: int, case: dict[str, Any]) -> list[dict[str, Any]]:
    """单条用例的结构性命中。

    （从 `audit_cases` 里拆出来是为了过复杂度棘轮：整卷判据与单条判据挤在一个函数里
    实测 CC 12，读者还要在"循环里的循环"和"循环后的统计"之间来回跳。）
    """
    out: list[dict[str, Any]] = []
    inp = str(case.get("input") or "")
    mode = mode_of(case)
    if not inp.strip():
        out.append(
            _finding("empty_input", "缺少非空 input ⇒ 这一条会被 --cases-file 当场拦住", index)
        )
    if not mode_is_valid(mode):
        out.append(
            _finding(
                "bad_mode",
                f"非法 mode {mode!r}（允许 {', '.join(VALID_MODES)} 或 custom:<名>）",
                index,
            )
        )
    if is_advisory(case):
        out.append(
            _finding(
                "rule_like",
                "expected 读起来是需求规则而不是字面片段 ⇒ contains/exact 永远命不中，"
                '改成输出里真会出现的一段字，或加 "mode": "rule" 交评委',
                index,
            )
        )
    elif _is_hollow(case):
        out.append(
            _finding(
                "no_assertion",
                f"mode={mode} 而 expected 为空 ⇒ 这条根本不会执行任何比对"
                "（`check_assertion` 返回 None），白占一个用例位",
                index,
            )
        )
    if echoes_input(case):
        out.append(
            _finding(
                "echoes",
                "expected 就在本条 input 里 ⇒ 复读输入的输也能过："
                "它只测「别丢内容」，测不出任务质量",
                index,
            )
        )
    return out


def _stats(cases: list[dict[str, Any]], max_cases: int) -> dict[str, Any]:
    """四桶分区（veto / advisory / judge_only / no_assertion）必须不重不漏，加一条点名强判据。"""
    advisory_idx = {i for i, c in enumerate(cases, 1) if is_advisory(c)}
    deterministic = {i for i, c in enumerate(cases, 1) if has_assertion(c)}
    veto = sorted(deterministic - advisory_idx)
    veto_strong = [i for i in veto if not echoes_input(cases[i - 1])]
    return {
        "cases": len(cases),
        "max_cases": max_cases,
        "veto_capable": len(veto),
        "veto_strong": len(veto_strong),
        "advisory": len(advisory_idx),
        "judge_only": sum(1 for c in cases if mode_of(c) == RULE_MODE),
        "no_assertion": sum(1 for c in cases if _is_hollow(c)),
        "echo_only": len(veto) - len(veto_strong),
        "veto_strong_cases": veto_strong,
    }


def audit_cases(cases: list[dict[str, Any]], *, max_cases: int) -> dict[str, Any]:
    """静态体检：返回 {findings: [{code, case, detail}], stats: {...}}。零调用、零出网。"""
    findings: list[dict[str, Any]] = []
    n = len(cases)
    if n > max_cases:
        findings.append(
            _finding(
                "over_cap",
                f"{n} 条超过 --cases-file 的上限 {max_cases} 条 ⇒ 跑起来会被当场拦住（退出码 2），先筛选",
            )
        )

    seen: dict[str, int] = {}
    for i, case in enumerate(cases, 1):
        findings.extend(_case_findings(i, case))
        key = _normalized(str(case.get("input") or ""))
        if key and key in seen:
            findings.append(
                _finding(
                    "duplicate",
                    f"与第 {seen[key]} 条归一化后完全相同 ⇒ 同一件事数了两遍，样本量是假象",
                    i,
                )
            )
        elif key:
            seen[key] = i

    missing = missing_scenarios([str(c.get("scenario") or "") for c in cases], n)
    if missing:
        labeled = any(str(c.get("scenario") or "").strip() for c in cases)
        head = "标了 scenario 却缺格" if labeled else "整卷没标 scenario"
        findings.append(
            _finding(
                "scenario_gap",
                f"{head}：缺 {'、'.join(missing)}。⚠️ 这条闸在闭环里只管**模型生成**的用例"
                "（`pm/nodes/execute.py::_has_coverage`），自带用例集不受它约束 —— "
                "所以缺口不会拦住你，只会照样烧完整轮，而评分全落在最简单的路径上",
            )
        )

    return {"findings": findings, "stats": _stats(cases, max_cases)}
