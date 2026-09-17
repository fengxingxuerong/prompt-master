"""记忆层（pm/memory.py）回归：相似资产检索 + 生成链路注入。"""

from __future__ import annotations

import json
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
