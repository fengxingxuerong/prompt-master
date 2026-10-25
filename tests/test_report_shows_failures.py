"""用量表必须显示失败/重试（§三十·十八 的最后一段）。

以前失败的调用在台账上完全不存在，于是报告里「真的慢」与「一直失败」
长得一模一样——本会话已因此查错三轮。这一条钉住报告层不再把它藏起来。

判据设计：只看**报告文本里有没有那些数**，不看实现。
"""

from __future__ import annotations

from typing import Any

from pm.report import _usage_lines


def _state(usage: dict[str, Any]) -> dict[str, Any]:
    return {"llm_usage": usage}


def test_failure_table_appears_when_there_are_failures() -> None:
    usage = {
        "evaluator": {
            "calls": 16,
            "input_tokens": 116834,
            "output_tokens": 194284,
            "latency_ms": 2249300,
            "failed_calls": 2,
            "failed_latency_ms": 61000,
            "retry_requests": 3,
        },
    }
    text = "\n".join(_usage_lines(_state(usage)))

    assert "失败次数" in text, "失败表没渲染"
    # 真实数字必须原样出现，不能只渲染表头
    assert "| evaluator | 2 | 61.0s | 3 |" in text, f"失败那行不对：\n{text}"
    # 合计也要
    assert "| **合计** | 2 | 61.0s | 3 |" in text


def test_success_calls_are_not_inflated_by_failures() -> None:
    """失败不许并进「调用次数」——那会污染按成功调用算的速率口径。"""
    usage = {
        "evaluator": {
            "calls": 16,
            "input_tokens": 100,
            "output_tokens": 200,
            "latency_ms": 5000,
            "failed_calls": 7,
            "failed_latency_ms": 9000,
            "retry_requests": 0,
        },
    }
    text = "\n".join(_usage_lines(_state(usage)))

    # 成功表那行必须还是 16
    assert "| evaluator | 16 | 100 | 200 | 5.0s |" in text, f"成功表被失败污染了：\n{text}"


def test_warning_says_the_two_columns_must_not_be_added() -> None:
    """必须明说"不可相加"——否则读的人会把 16+2 当成 18 次调用。"""
    usage = {
        "evaluator": {
            "calls": 16,
            "input_tokens": 1,
            "output_tokens": 1,
            "latency_ms": 1,
            "failed_calls": 2,
            "failed_latency_ms": 1,
        },
    }
    text = "\n".join(_usage_lines(_state(usage)))
    assert "不可相加" in text, "没警示两列不可相加"
    assert "只数**成功**" in text, "没说清 calls 只数成功"


def test_retry_only_round_still_renders_the_table() -> None:
    """「重试过但没失败过」的轮次也必须渲染这张表（2026-10-31 修）。

    这类轮次的形状是：通道 A 传输层成功、解析失败 → 通道 B 重发。
    `failed_calls` 是 0（没有异常到端点），`retry_requests` 是 1。
    旧版只按 failed_calls 判渲染条件，于是"重发过一整发全价请求"这件事
    在报告里**整段消失**。
    """
    usage = {
        "evaluator": {
            "calls": 4,
            "input_tokens": 100,
            "output_tokens": 200,
            "latency_ms": 5000,
            "failed_calls": 0,
            "failed_latency_ms": 0,
            "retry_requests": 2,
        },
    }
    text = "\n".join(_usage_lines(_state(usage)))
    assert "重试请求数" in text, f"只有重试时这段被整段吞掉了：\n{text}"
    assert "| evaluator | 0 | 0.0s | 2 |" in text


def test_no_failure_table_when_everything_succeeded() -> None:
    """没有失败时**不要**渲染这张表——多余的表格是噪音。"""
    usage = {
        "evaluator": {"calls": 16, "input_tokens": 100, "output_tokens": 200, "latency_ms": 5000},
    }
    text = "\n".join(_usage_lines(_state(usage)))

    assert "失败次数" not in text, f"没有失败却渲染了失败表：\n{text}"
    assert "| evaluator | 16 | 100 | 200 | 5.0s |" in text


def test_old_archive_without_the_fields_renders_cleanly() -> None:
    """历史归档没有这三个字段 ⇒ 不能炸、也不能显示成 0 假装有数据。

    §三十·十八 之前的所有 run_*.json 都属这类。
    """
    usage = {
        "evaluator": {
            "calls": 25,
            "input_tokens": 171675,
            "output_tokens": 221375,
            "latency_ms": 2462000,
        }
    }
    lines = _usage_lines(_state(usage))
    text = "\n".join(lines)

    assert "失败次数" not in text, "缺字段时不该渲染失败表"
    assert "| evaluator | 25 | 171675 | 221375 | 2462.0s |" in text
