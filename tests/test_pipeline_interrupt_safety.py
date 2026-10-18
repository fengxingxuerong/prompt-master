"""同步流水线（`pm/cli/pipeline.py`）中断时必须留证。

## 事故原型（2026-10-14，可复核）

R4 单变量对照那一轮：`logs/live_run_support_triage_20261014_r4.err` 最后一行停在
`02:25:13 [reviser] 轮换到 Key 池第 0 个 Key 重试`，`.json` 是 **0 字节**，
`logs/` 下也没有那两小时的 `run_*.json`（同日 20:20 / 21:08 / 23:16 / 23:17 完成或早停的
四轮都有落盘 ⇒ 不是"从来不落盘"，是"只有跑完才落盘"）。

后果：那一轮烧掉的真端点调用没有留下任何可复核的产物，而它要裁定的正是
"任务命题 vs 提示词骨架"这个归因 —— 判据（成对盲评）随进程一起没了。

根因：`run_pipeline` 只在正常返回时把 state 交回 `main.py:245` 的 `save_artifacts`；
Ctrl+C / 终端关闭抛的是 `KeyboardInterrupt`（`BaseException`），当前代码既不抓也不落盘。
而 langgraph 默认带 MemorySaver，**每个节点之后的状态本来就在内存里**，只是没人去取。

## 这三条判据守的边界

1. 中断要落盘，且必须**明说这份是中断快照**（`artifact_interrupted=true`）——
   半份产物长得像完整报告，比没有产物更危险（README §三 反复吃过"看起来是证据"的亏）。
2. 正常路径一个字节都不许多写：落盘时机变了会污染 `--json` 契约与运营目录闸。
3. 抢救失败不许吞掉原始异常：落盘是尽力而为，把 `KeyboardInterrupt` 变成
   `OSError` 会让调用方以为任务本身坏了。
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from pm import cli
from pm.cli import pipeline


class _Ledger:
    def snapshot(self) -> dict:
        return {"calls": 7, "prompt_tokens": 100, "completion_tokens": 50}


class _FakeApp:
    """只在 `invoke` 时按指示失败；`get_state` 返回"已完成到评测节点"的状态。"""

    def __init__(self, values: dict, fail: BaseException | None = None, state_raises: bool = False):
        self._values = values
        self._fail = fail
        self._state_raises = state_raises

    def invoke(self, *args, **kwargs):
        if self._fail is not None:
            raise self._fail
        return

    def get_state(self, config):
        if self._state_raises:
            raise RuntimeError("checkpointer 也坏了")
        return SimpleNamespace(values=dict(self._values), tasks=[])


def _make_args(run_id: str) -> argparse.Namespace:
    return argparse.Namespace(
        checkpoint=None,
        thread_id=None,
        max_iter=3,
        interactive=False,
        out=None,
    )


@pytest.fixture
def env(tmp_path, monkeypatch):
    log_dir = tmp_path / "logs"
    monkeypatch.setenv("PM_LOG_DIR", str(log_dir))
    monkeypatch.setattr(pipeline, "usage_scope", lambda: contextlib.nullcontext(_Ledger()))
    return log_dir


def _install(
    monkeypatch, values: dict, fail: BaseException | None = None, state_raises: bool = False
):
    app = _FakeApp(values, fail=fail, state_raises=state_raises)
    monkeypatch.setattr(pipeline, "build_app", lambda *a, **k: app)
    return app


def test_interrupt_saves_the_state_that_was_already_paid_for(monkeypatch, env):
    """Ctrl+C 之后，盘上要有那一轮跑出来的东西，不能是 0 字节。"""
    run_id = "r4rescue0001"
    values = {
        "run_id": run_id,
        "evaluations": [{"test_case_index": 0, "weighted_score": 9.1}],
        "baseline_aggregate": {"avg": 8.77},
    }
    _install(monkeypatch, values, fail=KeyboardInterrupt())

    with pytest.raises(KeyboardInterrupt):
        pipeline.run_pipeline(_make_args(run_id), dict(values))

    artifact = env / f"run_{run_id}.json"
    assert artifact.exists(), "中断后一个字节都没留下（R4 那次就是这样）"
    saved = json.loads(artifact.read_text(encoding="utf-8"))
    assert saved["artifact_interrupted"] is True
    assert saved["evaluations"] == values["evaluations"]
    assert saved["baseline_aggregate"] == values["baseline_aggregate"]
    assert saved["llm_usage"]["calls"] == 7, "台账快照要一起留下，否则中断的那一轮又查不到耗时"


def test_interrupted_artifact_is_never_mistaken_for_a_finished_one(monkeypatch, env):
    """正常跑完的产物不许带 interrupted 标记：半份产物冒充完整报告比没有更坏。"""
    run_id = "r4rescue0002"
    values = {"run_id": run_id, "status": "passed"}
    _install(monkeypatch, values, fail=None)

    final = pipeline.run_pipeline(_make_args(run_id), dict(values))

    assert "artifact_interrupted" not in final
    assert not (env / f"run_{run_id}.json").exists(), "正常路径不该由 pipeline 落盘"


def test_rescue_failure_does_not_mask_the_original_error(monkeypatch, env):
    """抢救失败就老实让原始异常抛出：把 KeyboardInterrupt 变成别的错会误导排障。"""
    run_id = "r4rescue0003"
    _install(monkeypatch, {"run_id": run_id}, fail=KeyboardInterrupt(), state_raises=True)

    with pytest.raises(KeyboardInterrupt):
        pipeline.run_pipeline(_make_args(run_id), {"run_id": run_id})

    assert not (env / f"run_{run_id}.json").exists()


def test_empty_state_is_not_written(monkeypatch, env):
    """空状态不落盘：R4 那份 0 字节的 .json 正是"文件在、信息不在"的坏形状。"""
    run_id = "r4rescue0004"
    _install(monkeypatch, {}, fail=KeyboardInterrupt())

    with pytest.raises(KeyboardInterrupt):
        pipeline.run_pipeline(_make_args(run_id), {"run_id": run_id})

    assert not (env / f"run_{run_id}.json").exists()


def test_log_dir_is_resolved_late(monkeypatch, env):
    """落盘位置必须走 `pm.paths.log_dir()`（晚绑定），不许写死 `<repo>/logs`。"""
    run_id = "r4rescue0005"
    _install(monkeypatch, {"run_id": run_id, "status": "max_iterations"}, fail=KeyboardInterrupt())

    with pytest.raises(KeyboardInterrupt):
        pipeline.run_pipeline(_make_args(run_id), {"run_id": run_id})

    assert (env / f"run_{run_id}.json").exists()
    assert cli.pipeline.__file__  # 模块可达性占位，真正的判据在上面那条 exists


def test_repo_logs_dir_is_untouched(monkeypatch, env):
    """测试期间不许写脏仓库里的运营目录（conftest 那只闸只查 `run_*.json`）。"""
    repo_logs = Path(__file__).resolve().parents[1] / "logs"
    before = {p.name for p in repo_logs.glob("run_*.json")} if repo_logs.exists() else set()
    run_id = "r4rescue0006"
    _install(monkeypatch, {"run_id": run_id, "evaluations": []}, fail=KeyboardInterrupt())

    with pytest.raises(KeyboardInterrupt):
        pipeline.run_pipeline(_make_args(run_id), {"run_id": run_id})

    assert os.environ.get("PM_LOG_DIR") == str(env)
    after = {p.name for p in repo_logs.glob("run_*.json")} if repo_logs.exists() else set()
    assert after == before
