"""金额核算：把已有的 token 台账折算成钱，并按本次运行的真实单价分布外推。

竞品依据（2026-10-02 调研，见 docs/iteration/competitor-analysis-prompt.md）：
LangSmith / Helicone / FutureAGI / LiteLLM 都把**成本**与质量并列成一等指标，
而"单轮优化跑掉多少钱"正是本产品最该回答却一直没回答的问题——报告里有 token、
有调用次数、有耗时，唯独没有金额。读者只能自己开计算器乘单价。

本模块的立场（与本仓库其余部分一致）：
1. **绝不假装知道价目表**。模型单价天天变，写死在代码里的价目表第二天就是错的，
   而且错误方向无从察觉（金额看起来永远合理）。所以单价必须由用户显式给：
   `PM_PRICE_INPUT_PER_M` / `PM_PRICE_OUTPUT_PER_M`（每百万 token 的计价单位）。
   没给就返回 None，报告里那一段直接不出现——**没有数比假数好**。
2. **按角色分开定价**（可选）：`PM_PRICE_<角色>_INPUT_PER_M` 覆盖全局值。
   真实配置里 target 与 evaluator 常常不是同一个模型（本项目默认就是多端点），
   用单一均价折算会得出一个"看起来很合理"的错数。
3. 折算口径写进报告：金额 = Σ(输入tokens×入价 + 输出tokens×出价) / 1_000_000，
   单位随用户给价的单位走（一般是美元）。不换算币种、不加汇率。
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from typing import Any

# 环境变量名里的角色段：转成大写、非字母数字换成下划线，
# 使 `PM_PRICE_EVALUATOR_B_INPUT_PER_M` 能对应角色 `evaluator_b`。
_ROLE_RE = re.compile(r"[^A-Z0-9]+")


def _env_price(name: str) -> float | None:
    """读一个单价。未设/空/非法/负数一律返回 None（= 不折算），不抛错。

    非法值**不告警**是有意的：这段会在每个 run 的报告里渲染一次，
    把配置噪声刷进报告只会淹没真正的问题；而 `run.py cost --check-prices`
    提供了显式自检入口，想核对配置的人有地方核对。
    """
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return None
    try:
        val = float(raw)
    except ValueError:
        return None
    return val if val >= 0 else None


def global_prices() -> tuple[float | None, float | None]:
    """(input_per_m, output_per_m) 全局单价；任一未设则该项为 None。"""
    return (
        _env_price("PM_PRICE_INPUT_PER_M"),
        _env_price("PM_PRICE_OUTPUT_PER_M"),
    )


def _role_env_suffix(role: str) -> str:
    return _ROLE_RE.sub("_", (role or "").upper()).strip("_")


def role_prices(role: str) -> tuple[float | None, float | None]:
    """某角色的有效单价：优先 `PM_PRICE_<角色>_*`，回退全局，两者都没有则 None。"""
    gi, go = global_prices()
    suffix = _role_env_suffix(role)
    ri = _env_price(f"PM_PRICE_{suffix}_INPUT_PER_M") if suffix else None
    ro = _env_price(f"PM_PRICE_{suffix}_OUTPUT_PER_M") if suffix else None
    return (ri if ri is not None else gi, ro if ro is not None else go)


def prices_configured() -> bool:
    """是否有任何可用单价（决定报告里出不出那一段）。"""
    gi, go = global_prices()
    return gi is not None or go is not None


def _safe_int(value: Any) -> int:
    """尽力取非负整数，取不到就是 0。

    与 `report._report_safe_int` 同一动机：state 里的字段类型不可信
    （旧 checkpoint、外部构造、被手改过的 run_*.json），`int(None)` / `int("abc")`
    会让**报告渲染**整段崩掉——而报告是唯一给用户看的东西。
    """
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def role_cost(role: str, usage_row: Any) -> float | None:
    """单个角色的金额；单价不全时返回 None（**不把缺的那半当 0**）。

    这是本模块最容易出大错的地方：只配了输出价就把输入按 0 算，
    得到的金额会系统性偏低且看起来完全正常。缺一半就整条不算。

    `usage_row` 故意标成 Any：台账来自 state，可能是任何东西（实测喂一个字符串进来
    就会让整份报告崩掉）。非 Mapping 一律当"算不出来"，不抛错。
    """
    pi, po = role_prices(role)
    if pi is None or po is None:
        return None
    if not isinstance(usage_row, Mapping):
        return None
    inp = _safe_int(usage_row.get("input_tokens", 0))
    out = _safe_int(usage_row.get("output_tokens", 0))
    return (inp * pi + out * po) / 1_000_000.0


def compute_cost(llm_usage: dict[str, Any] | None) -> dict[str, Any] | None:
    """按角色台账折算总成本；无单价或无台账时返回 None。

    返回结构（供报告与 `--json` 共用）：
        {"total": 1.234, "currency": "price_unit", "by_role": {...},
         "priced_roles": [...], "unpriced_roles": [...], "note": "..."}

    `unpriced_roles` 必须单独列出来：总金额只覆盖被定价的角色，
    漏报会让读者以为"总额 = 全部开销"。
    """
    usage = llm_usage or {}
    if not isinstance(usage, Mapping):
        return None
    if not usage or not prices_configured():
        return None

    by_role: dict[str, float] = {}
    priced: list[str] = []
    unpriced: list[str] = []
    for role in sorted(usage, key=str):
        c = role_cost(str(role), usage.get(role))
        if c is None:
            unpriced.append(str(role))
            continue
        by_role[str(role)] = round(c, 6)
        priced.append(str(role))

    if not priced:
        return None

    total = round(sum(by_role.values()), 6)
    note = "按角色单价折算；单位随 PM_PRICE_*_PER_M 的单位（一般是美元）"
    if unpriced:
        note += f"。⚠️ {'、'.join(unpriced)} 未定价（缺输入或输出单价/台账不可用），未计入总额"
    return {
        "total": total,
        "currency": "price_unit",
        "by_role": by_role,
        "priced_roles": priced,
        "unpriced_roles": unpriced,
        "note": note,
    }


def render_cost_section(llm_usage: dict[str, Any] | None) -> list[str]:
    """报告的「成本折算」段（Markdown 行）；未配置单价时返回空列表。"""
    cost = compute_cost(llm_usage)
    if not cost:
        return []
    lines = [
        "## 成本折算",
        "",
        "| 角色 | 金额 |",
        "|---|---|",
    ]
    for role, amount in sorted(cost["by_role"].items(), key=lambda kv: -kv[1]):
        lines.append(f"| {role} | {amount:.4f} |")
    lines.append(f"| **合计** | **{cost['total']:.4f}** |")
    lines.append("")
    lines.append(f"> {cost['note']}。单价来自 `PM_PRICE_*_PER_M`，本系统不内置价目表——")
    lines.append("> 模型单价会变，写死的价目表第二天就是错的，而且错得看不出来。")
    lines.append("")
    return lines


def project_cost(
    unit_cost: float | None, n_calls_estimate: dict[str, int] | None
) -> dict[str, Any] | None:
    """把「本次运行的真实均价」外推到预算区间（submit 前的钱决策）。

    为什么用本次运行的真实分布而不是价目表：`llm_usage` 里有逐角色的
    token 实测值，算出的是**这次任务**的单次调用均价，比任何通用估算都准。
    """
    if unit_cost is None or not n_calls_estimate:
        return None
    lo = int(n_calls_estimate.get("min", 0) or 0)
    hi = int(n_calls_estimate.get("max", 0) or 0)
    return {
        "per_call": round(unit_cost, 6),
        "estimated_total_min": round(unit_cost * lo, 6),
        "estimated_total_max": round(unit_cost * hi, 6),
        "basis_calls": {"min": lo, "max": hi},
    }
