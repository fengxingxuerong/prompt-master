"""「成功但空」必须被披露，且要带上它烧掉的 token（§三十·十九）。

真实证据 run_41a833fb9b41：那轮 optimizer 花掉 **32850 输出 token / 207 秒**，
台账（`llm_usage`）每个字段都正常——因为传输层是 HTTP 200，token 照记。
可最终提示词是 **0 字符**，`prompt_versions` 0 版，`early_stop_reason` 写着
「优化器返回空内容（端点截断/思考耗尽）」。

⇒ 只看用量表，这轮看起来完全正常。§三十·十八 记的"失败"也抓不到它，
   因为它不是失败。**台账按成功/失败二分，"成功但空"两类都不是。**

这一节把 trace 的 `*_empty` 事件和台账对上，在报告里显出来。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pm.report import _usage_lines

LOGS = Path(__file__).resolve().parents[1] / "logs"
RUN_ID = "41a833fb9b41"


def _state(usage: dict[str, Any], trace: list[dict[str, Any]]) -> dict[str, Any]:
    return {"llm_usage": usage, "trace": trace}


def test_real_run_had_a_big_wasted_delivery() -> None:
    """钉住这次翻案的原始事实：台账正常，交付为空。"""
    p = LOGS / f"run_{RUN_ID}.json"
    if not p.exists():
        pytest.skip("归档不在本机")
    d = json.loads(p.read_text(encoding="utf-8"))

    assert len(str(d.get("prompt") or "")) == 0, "归档变了：提示词不为空，本条的推理前提失效"
    assert not (d.get("prompt_versions") or [])

    opt = (d.get("llm_usage") or {}).get("optimizer") or {}
    assert int(opt.get("output_tokens") or 0) > 10000, (
        f"那轮 optimizer 实际只花了 {opt.get('output_tokens')} token，"
        "『白烧 3 万 token』这个说法要改"
    )
    # 关键：台账上它是一次**成功**调用（没有失败计数）
    assert int(opt.get("calls") or 0) >= 1
    assert int(opt.get("failed_calls") or 0) == 0, (
        "若它已被记成失败，那『成功但空』这个缺口就不存在，本节要重写"
    )


def test_wasted_delivery_is_disclosed_with_its_cost() -> None:
    """有 `*_empty` 事件时，报告要写出事件，并带上该角色烧掉的量。"""
    usage = {
        "optimizer": {
            "calls": 2,
            "input_tokens": 2827,
            "output_tokens": 32850,
            "latency_ms": 207139,
        },
    }
    trace = [{"node": "optimize", "event": "optimize_empty", "calls": 2}]
    text = "\n".join(_usage_lines(_state(usage, trace)))

    assert "成功但交付为空" in text, "没披露成功但空"
    assert "optimize_empty" in text, "没点出具体事件"
    # 必须带上真实数字，否则读的人还是不知道白烧了多少
    assert "32850" in text, f"没带上烧掉的 token：\n{text}"
    assert "207" in text, f"没带上烧掉的耗时：\n{text}"


def test_no_disclosure_when_nothing_is_empty() -> None:
    """正常轮（无 *_empty）不得渲染这段——多余的告警是噪音。"""
    usage = {
        "evaluator": {
            "calls": 16,
            "input_tokens": 116834,
            "output_tokens": 194284,
            "latency_ms": 2249300,
        },
    }
    trace = [{"node": "evaluate", "event": "evaluate_done", "avg": 5.96}]
    text = "\n".join(_usage_lines(_state(usage, trace)))

    assert "成功但交付为空" not in text, f"正常轮不该告警：\n{text}"
    assert "| evaluator | 16 | 116834 | 194284 | 2249.3s |" in text


def test_evaluate_empty_maps_to_the_right_role() -> None:
    """node=evaluate 要映射到 evaluator，不能静默丢掉金额。"""
    usage = {
        "evaluator": {"calls": 8, "input_tokens": 1, "output_tokens": 5000, "latency_ms": 60000},
    }
    trace = [{"node": "evaluate", "event": "evaluate_empty"}]
    text = "\n".join(_usage_lines(_state(usage, trace)))

    assert "evaluate_empty" in text
    assert "5000" in text, f"evaluate_empty 没带出金额：\n{text}"


def test_unknown_node_does_not_invent_a_number() -> None:
    """映射不出来的 node 只报事件，**不编金额**。

    ⚠️ 判据只能看**披露那一行**：`999` 本身会出现在上面的用量表里
    （那是真的用量），所以不能断言全文没有它——第一版就是这么写错的，
    被真实输出打脸（用量表里的 999 合法）。
    """
    usage = {"optimizer": {"calls": 1, "input_tokens": 1, "output_tokens": 999, "latency_ms": 1000}}
    trace = [{"node": "some_future_node", "event": "some_future_node_empty"}]
    text = "\n".join(_usage_lines(_state(usage, trace)))

    assert "some_future_node_empty" in text
    line = next(ln for ln in text.splitlines() if "some_future_node_empty" in ln)
    assert "token" not in line, f"未知 node 不该借别的角色的金额：{line}"
    assert "已花" not in line, f"未知 node 不该报金额：{line}"


def test_missing_trace_key_is_tolerated() -> None:
    """没有 trace 字段的旧归档/单测 state 不许炸。"""
    text = "\n".join(_usage_lines({"llm_usage": {"evaluator": {"calls": 1}}}))
    assert "| evaluator | 1 | 0 | 0 | 0.0s |" in text
    assert "成功但交付为空" not in text


import pytest  # noqa: E402 - 放在后面是为了让上面的常量区更醒目
