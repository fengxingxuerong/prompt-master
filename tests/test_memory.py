"""记忆层（pm/memory.py）回归：相似资产检索 + 生成链路注入。"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import pytest
from pm import memory
from pm.memory import find_similar_asset, render_memory_hint
from pm.state import initial_state


def _write_run(
    log_dir: Path,
    run_id: str,
    task: str,
    status: str = "passed",
    demo: bool = False,
    avg: float = 9.0,
    base: float | None = 6.0,
    prompt: str = "[角色] 测试提示词",
) -> None:
    trace_channels = ["fake"] if demo else ["json_fallback"]
    d = {
        "run_id": run_id,
        "task": task,
        "status": status,
        "prompt": prompt,
        "aggregate": {"avg_score": avg, "ci_lower": avg - 0.2},
        "baseline_aggregate": {"avg_score": base} if base is not None else None,
        "trace": [{"node": "test", "event": "test_done", "channel": c} for c in trace_channels],
    }
    (log_dir / f"run_{run_id}.json").write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def log_dir(tmp_path: Path) -> Path:
    d = tmp_path / "logs"
    d.mkdir()
    return d


def test_find_similar_hits_passed_and_delta_positive(log_dir):
    _write_run(log_dir, "good1", "对电商客服对话做分类分诊，提取订单号", avg=9.5, base=5.0)
    hit = find_similar_asset("对电商客服对话做分类分诊，提取退款诉求", log_dir=log_dir)
    assert hit and hit["run_id"] == "good1"
    assert hit["similarity"] >= memory._SIM_MIN


def test_find_similar_skips_failed_and_negative_delta(log_dir):
    _write_run(log_dir, "failed1", "对客服对话做分类分诊，提取订单号", status="max_iterations")
    _write_run(log_dir, "neg1", "对客服对话做分类分诊，提取订单号", avg=5.0, base=7.0)
    assert find_similar_asset("给外卖评论做分类并提取退款诉求", log_dir=log_dir) is None, (
        "未达标交付与跑输基线的运行都没有借鉴价值"
    )


def test_find_similar_skips_demo_and_self(log_dir):
    _write_run(log_dir, "demo1", "对客服对话做分类分诊，提取订单号", demo=True)
    _write_run(log_dir, "self1", "对客服对话做分类分诊，提取订单号")
    assert (
        find_similar_asset(
            "对客服对话做分类分诊，提取订单号", exclude_run_id="self1", log_dir=log_dir
        )
        is None
    )


def test_find_similar_respects_memory_hint_off(log_dir, monkeypatch):
    _write_run(log_dir, "good1", "对客服对话做分类分诊，提取订单号")
    monkeypatch.setenv("PM_MEMORY_HINT", "0")
    assert find_similar_asset("对客服对话做分类分诊", log_dir=log_dir) is None


def test_render_memory_hint_contains_guard_note(log_dir):
    _write_run(log_dir, "good1", "对客服对话做分类分诊")
    hit = find_similar_asset("对客服对话做分类分诊", log_dir=log_dir)
    hint = render_memory_hint(hit)
    assert "不是标准答案" in hint and "禁止照搬参考的任务语义" in hint, (
        "参考块必须自带防误用声明（含照抄任务语义的实测警示）"
    )


def test_unrelated_task_returns_none(log_dir):
    _write_run(log_dir, "good1", "对电商客服对话做分类分诊，提取订单号")
    assert find_similar_asset("写一首关于春天的诗", log_dir=log_dir) is None, (
        "低于相似度门槛不该硬塞参考"
    )


def test_optimize_node_injects_memory_hint(log_dir, monkeypatch):
    """生成链路端到端：命中资产时 trace 落 memory_hint，且 user 段带参考块。"""
    from pm import testing
    from pm.nodes import optimize_node

    _write_run(log_dir, "good1", "给外卖评论做分类并提取退款诉求", avg=9.5, base=6.0)
    monkeypatch.setattr(
        "pm.nodes.optimize.find_similar_asset",
        lambda task, exclude_run_id=None: {
            "run_id": "good1",
            "similarity": 0.3,
            "avg_score": 9.5,
            "task": "给外卖评论做分类并提取退款诉求",
            "prompt_excerpt": "[角色] 历史高分提示词",
        },
    )
    monkeypatch.setenv("PM_MEMORY_HINT", "1")
    state = initial_state(task="给外卖评论做分类并提取退款", target_model="fake")
    state["run_id"] = "current"
    with testing.fake_backend("progress"):
        out = optimize_node(state)  # type: ignore[arg-type]
    ev = [e for e in out["trace"] if e["event"] == "optimize_done"][-1]
    assert ev.get("memory_hint", {}).get("run_id") == "good1", "命中资产必须写进 trace"


# ---------------------------------------------------------------------------
# 扫描上限（PM_MEMORY_SCAN_LIMIT）：截断必须"看得见"，且改上限能差分复现
# ---------------------------------------------------------------------------
_T0 = 1_700_000_000  # 固定基准时间戳；mtime 只用来定"最近 N 个"，必须显式设置避免同秒抖动


def _age(log_dir: Path, run_id: str, age_seconds: int) -> None:
    t = _T0 + age_seconds
    os.utime(log_dir / f"run_{run_id}.json", (t, t))


def _seed_two_runs_with_different_ages(log_dir: Path) -> str:
    """造两条记录：完全匹配的那条刻意最旧，较匹配的那条最新。

    这样"扫描上限"就有了可观测的后果：上限=1 只会看到最新那条，
    返回的 run_id 直接暴露它有没有把旧记录砍掉。
    """
    exact = "对电商客服对话做分类分诊，提取订单号"
    _write_run(log_dir, "exact-but-old", exact)
    _write_run(log_dir, "close-and-new", "对电商客服对话做分类分诊，提取退款单号")
    _age(log_dir, "exact-but-old", 0)
    _age(log_dir, "close-and-new", 600)
    return exact


def test_scan_limit_truncation_is_disclosed(log_dir, monkeypatch, caplog):
    """被截断时必须留一条说清"扫了多少/共多少"的告警 —— 静默少扫与"没有同类任务"长一样。"""
    exact = _seed_two_runs_with_different_ages(log_dir)
    monkeypatch.setenv("PM_MEMORY_SCAN_LIMIT", "1")
    with caplog.at_level(logging.WARNING, logger="pm.memory"):
        hit = find_similar_asset(exact, log_dir=log_dir)
    assert hit and hit["run_id"] == "close-and-new"
    assert "截断" in caplog.text, caplog.text
    assert "1" in caplog.text and "2" in caplog.text, f"告警要写清扫了几条/共几条：{caplog.text}"


def test_raising_scan_limit_recovers_the_older_asset(log_dir, monkeypatch):
    """差分断言：同一份历史，上限放到 2 就能命中被截掉的那条 —— 证明截断是唯一的因。"""
    exact = _seed_two_runs_with_different_ages(log_dir)
    monkeypatch.setenv("PM_MEMORY_SCAN_LIMIT", "2")
    assert find_similar_asset(exact, log_dir=log_dir)["run_id"] == "exact-but-old"


def test_no_warning_when_history_fits_in_the_limit(log_dir, monkeypatch, caplog):
    """没截断就不许告警：否则这条日志很快被当成噪音忽略掉。"""
    exact = _seed_two_runs_with_different_ages(log_dir)
    monkeypatch.setenv("PM_MEMORY_SCAN_LIMIT", "500")
    with caplog.at_level(logging.WARNING, logger="pm.memory"):
        find_similar_asset(exact, log_dir=log_dir)
    assert "截断" not in caplog.text


@pytest.mark.parametrize("raw", ["abc", "0", "-5"])
def test_illegal_scan_limit_falls_back_with_warning(log_dir, monkeypatch, caplog, raw):
    """非法上限回退默认并告警，绝不能读成 0（那等于把记忆层静默关掉）。"""
    exact = _seed_two_runs_with_different_ages(log_dir)
    monkeypatch.setenv("PM_MEMORY_SCAN_LIMIT", raw)
    with caplog.at_level(logging.WARNING, logger="pm.memory"):
        hit = find_similar_asset(exact, log_dir=log_dir)
    assert hit and hit["run_id"] == "exact-but-old", f"回退到 200 后两条都该可见：{hit}"
    assert "PM_MEMORY_SCAN_LIMIT" in caplog.text
