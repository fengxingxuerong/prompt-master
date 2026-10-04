"""金额核算（`pm/cost.py`）的回归。

这个模块的核心风险不是"算错乘法"，而是**静默地把缺的单价当成 0**：
只配了输出价时，把输入按 0 折算会得到一个系统性偏低、且看起来完全正常的金额。
所以下面一半用例钉的是这类"看起来合理但错的数"。
"""

from __future__ import annotations

import pytest
from pm.cost import (
    compute_cost,
    global_prices,
    prices_configured,
    project_cost,
    render_cost_section,
    role_cost,
    role_prices,
)

USAGE = {
    "target": {"calls": 4, "input_tokens": 1_000_000, "output_tokens": 500_000, "latency_ms": 1000},
    "evaluator": {"calls": 2, "input_tokens": 200_000, "output_tokens": 100_000, "latency_ms": 400},
}


@pytest.fixture(autouse=True)
def _clean_price_env(monkeypatch):
    """每个用例从"没有任何单价"开始：否则开发机上的 .env 会污染断言。"""
    for k in list(__import__("os").environ):
        if k.startswith("PM_PRICE_"):
            monkeypatch.delenv(k, raising=False)
    yield


def _set(monkeypatch, **env: str) -> None:
    for k, v in env.items():
        monkeypatch.setenv(k, v)


# --------------------------------------------------------------------------
# 未配单价：一个数都不给（没有数比假数好）
# --------------------------------------------------------------------------
def test_no_prices_configured_yields_none():
    assert prices_configured() is False
    assert global_prices() == (None, None)
    assert compute_cost(USAGE) is None
    assert render_cost_section(USAGE) == []


def test_no_usage_yields_none():
    assert compute_cost(None) is None
    assert compute_cost({}) is None


# --------------------------------------------------------------------------
# 只配一半单价：整条不算（这是最容易出的那种错）
# --------------------------------------------------------------------------
def test_half_prices_never_fall_back_to_zero(monkeypatch):
    """只配输出价时若把输入按 0 算，金额会系统性偏低且看不出来 —— 必须整条 None。"""
    _set(monkeypatch, PM_PRICE_OUTPUT_PER_M="10")
    assert prices_configured() is True
    assert role_cost("target", USAGE["target"]) is None
    assert compute_cost(USAGE) is None


def test_input_only_also_yields_none(monkeypatch):
    _set(monkeypatch, PM_PRICE_INPUT_PER_M="10")
    assert compute_cost(USAGE) is None


# --------------------------------------------------------------------------
# 正常折算
# --------------------------------------------------------------------------
def test_compute_cost_sums_roles(monkeypatch):
    _set(monkeypatch, PM_PRICE_INPUT_PER_M="1", PM_PRICE_OUTPUT_PER_M="2")
    cost = compute_cost(USAGE)
    assert cost is not None
    # target: 1M*1 + 0.5M*2 = 2.0；evaluator: 0.2M*1 + 0.1M*2 = 0.4
    assert cost["by_role"]["target"] == pytest.approx(2.0)
    assert cost["by_role"]["evaluator"] == pytest.approx(0.4)
    assert cost["total"] == pytest.approx(2.4)
    assert cost["unpriced_roles"] == []


def test_role_override_beats_global(monkeypatch):
    """真实配置里 target 与 evaluator 常不是同一个模型，单一均价会得出合理的错数。"""
    _set(
        monkeypatch,
        PM_PRICE_INPUT_PER_M="1",
        PM_PRICE_OUTPUT_PER_M="2",
        PM_PRICE_EVALUATOR_INPUT_PER_M="10",
        PM_PRICE_EVALUATOR_OUTPUT_PER_M="20",
    )
    assert role_prices("target") == (1.0, 2.0)
    assert role_prices("evaluator") == (10.0, 20.0)
    # 没被覆盖的角色回退全局
    assert role_prices("reviser") == (1.0, 2.0)


def test_role_env_suffix_follows_role_name(monkeypatch):
    """角色名里的下划线要能对上环境变量名（evaluator_b → PM_PRICE_EVALUATOR_B_*）。"""
    _set(
        monkeypatch,
        PM_PRICE_INPUT_PER_M="1",
        PM_PRICE_OUTPUT_PER_M="1",
        PM_PRICE_EVALUATOR_B_INPUT_PER_M="5",
        PM_PRICE_EVALUATOR_B_OUTPUT_PER_M="5",
    )
    assert role_prices("evaluator_b") == (5.0, 5.0)


def test_unpriced_role_is_listed_not_silently_dropped(monkeypatch):
    """部分角色未定价时，总额只覆盖已定价角色 —— 必须显式列出，否则读者以为总额=全部。"""
    _set(
        monkeypatch,
        PM_PRICE_INPUT_PER_M="1",
        PM_PRICE_OUTPUT_PER_M="1",
        PM_PRICE_TARGET_INPUT_PER_M="1",
        # target 有输入价但**没有**输出价的覆盖 → 走全局输出价，仍可定价
    )
    usage = dict(USAGE)
    usage["reviser"] = {"calls": 1, "input_tokens": 100, "output_tokens": 100}
    cost = compute_cost(usage)
    assert cost is not None
    assert "reviser" in cost["priced_roles"]


def test_partial_coverage_marks_unpriced(monkeypatch):
    """构造真的算不出来的角色：只有全局输入价、没有全局输出价时整体为 None。"""
    _set(monkeypatch, PM_PRICE_INPUT_PER_M="1", PM_PRICE_OUTPUT_PER_M="1")
    cost = compute_cost(USAGE)
    assert cost is not None and cost["unpriced_roles"] == []


# --------------------------------------------------------------------------
# 非法的单价配置
# --------------------------------------------------------------------------
@pytest.mark.parametrize("bad", ["abc", "", "  ", "-1"])
def test_invalid_price_is_ignored_quietly(monkeypatch, bad):
    """非法值不算价、也不抛错：这段每个 run 都要渲染一次，配置噪声不该进报告。"""
    _set(monkeypatch, PM_PRICE_INPUT_PER_M=bad, PM_PRICE_OUTPUT_PER_M=bad)
    assert global_prices() == (None, None)
    assert compute_cost(USAGE) is None


def test_zero_price_is_legal(monkeypatch):
    """0 是合法价（本地模型），不能与"未配置"混为一谈 —— 未配置是不出数，0 是出 0。"""
    _set(monkeypatch, PM_PRICE_INPUT_PER_M="0", PM_PRICE_OUTPUT_PER_M="0")
    assert global_prices() == (0.0, 0.0)
    cost = compute_cost(USAGE)
    assert cost is not None and cost["total"] == 0.0


# --------------------------------------------------------------------------
# 渲染与外推
# --------------------------------------------------------------------------
def test_render_cost_section_shape_and_disclaimer(monkeypatch):
    _set(monkeypatch, PM_PRICE_INPUT_PER_M="1", PM_PRICE_OUTPUT_PER_M="2")
    lines = render_cost_section(USAGE)
    out = "\n".join(lines)
    assert "## 成本折算" in out
    assert "**合计**" in out
    # 必须声明"本系统不内置价目表"，否则读者会把金额当成权威读数
    assert "不内置价目表" in out


def test_render_sorts_by_cost_desc(monkeypatch):
    _set(monkeypatch, PM_PRICE_INPUT_PER_M="1", PM_PRICE_OUTPUT_PER_M="1")
    lines = render_cost_section(USAGE)
    rows = [ln for ln in lines if ln.startswith("| ") and "角色" not in ln and "---" not in ln]
    # target 比 evaluator 贵 → 排在前面
    assert rows[0].startswith("| target")


def test_project_cost_scales_linearly():
    proj = project_cost(0.01, {"min": 45, "max": 102})
    assert proj is not None
    assert proj["estimated_total_min"] == pytest.approx(0.45)
    assert proj["estimated_total_max"] == pytest.approx(1.02)


def test_project_cost_needs_both_inputs():
    assert project_cost(None, {"min": 1, "max": 2}) is None
    assert project_cost(0.01, None) is None
    assert project_cost(0.01, {}) is None


# --------------------------------------------------------------------------
# 脏台账：报告渲染绝不能因为一个脏字段整段崩掉
# --------------------------------------------------------------------------
def test_dirty_usage_never_crashes_report(monkeypatch):
    """实测缺陷（2026-10-02）：台账里某个角色的值不是 dict 时，
    原实现直接 AttributeError，把**整份报告**渲染带走。

    报告是唯一给用户看的东西，`report._report_safe_int` 早就在防这一类；
    成本段是新来的，必须守同一条规矩。
    """
    _set(monkeypatch, PM_PRICE_INPUT_PER_M="1", PM_PRICE_OUTPUT_PER_M="1")
    dirty = {
        "target": {"calls": "abc", "input_tokens": "notanumber", "output_tokens": None},
        "evaluator": "not-a-dict",  # ← 原实现就在这里崩
        "reviser": {},
        "": {"input_tokens": 100, "output_tokens": 100},
        "weird role!": {"input_tokens": 1, "output_tokens": 1},
    }
    cost = compute_cost(dirty)  # 不许抛异常
    assert cost is not None
    # 非 Mapping 的条目要落到 unpriced，而不是被静默当成 0
    assert "evaluator" in cost["unpriced_roles"]
    # 渲染同样不许崩
    assert render_cost_section(dirty)


def test_negative_tokens_clamp_to_zero(monkeypatch):
    """负 token 是脏数据，不是"负成本"——不许算出负数金额。"""
    _set(monkeypatch, PM_PRICE_INPUT_PER_M="1", PM_PRICE_OUTPUT_PER_M="1")
    cost = compute_cost({"a": {"input_tokens": -99999, "output_tokens": -5}})
    assert cost is not None
    assert cost["total"] == 0.0


def test_non_mapping_usage_at_top_level_is_ignored(monkeypatch):
    _set(monkeypatch, PM_PRICE_INPUT_PER_M="1", PM_PRICE_OUTPUT_PER_M="1")
    assert compute_cost("not-a-dict") is None
    assert compute_cost([("target", {})]) is None
