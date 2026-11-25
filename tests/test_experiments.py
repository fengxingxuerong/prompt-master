"""实验矩阵的预算、逐格隔离与真实 target 角色切换测试。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from pm.cli.experiment import ExperimentError, experiment_command, load_spec
from pm.datasets import add_version


def _config(tmp_path: Path, **updates: Any) -> Path:
    payload: dict[str, Any] = {
        "name": "unit-matrix",
        "task": "请把给定文本整理成三条结论",
        "dataset": "qa:1",
        "prompts": [
            {"id": "short", "prompt": "请简洁地回答用户问题"},
            {"id": "strict", "prompt": "请严格依据事实回答问题"},
        ],
        "models": ["model-a", "model-b"],
        "samples": 1,
        "max_iter": 0,
        "baseline": False,
        "pairwise": False,
    }
    payload.update(updates)
    path = tmp_path / "matrix.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _dataset(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PM_DATASET_DIR", str(tmp_path / "datasets"))
    add_version("qa", [{"input": "文本甲", "expected": "甲", "mode": "contains"}])
    add_version("qa", [{"input": "文本乙", "expected": "乙", "mode": "contains"}])


def test_spec_pins_requested_dataset_version_and_rejects_plaintext_key(
    tmp_path: Path, monkeypatch
) -> None:
    _dataset(tmp_path, monkeypatch)
    spec = load_spec(_config(tmp_path))
    assert spec.dataset_ref == "qa:1" and spec.dataset_version == 1
    assert spec.models[0].model == "model-a"

    with pytest.raises(ExperimentError, match="不接受 api_key 明文"):
        load_spec(_config(tmp_path, models=[{"id": "x", "model": "x", "api_key": "secret"}]))


def test_dry_run_estimates_every_cell_without_running_or_writing(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    _dataset(tmp_path, monkeypatch)
    monkeypatch.setenv("PM_LOG_DIR", str(tmp_path / "logs"))
    import pm.cli.pipeline as pipeline

    monkeypatch.setattr(pipeline, "run_pipeline", lambda *_: pytest.fail("dry-run 调用了真实流程"))
    assert experiment_command([str(_config(tmp_path)), "--dry-run", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "experiment_dry_run" and payload["cells"] == 4
    assert payload["total"]["min"] == payload["per_cell"]["min"] * 4
    assert payload["total"]["max"] == payload["per_cell"]["max"] * 4
    assert not (tmp_path / "logs" / "experiments").exists()


def test_matrix_switches_real_target_config_and_restores_environment(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    _dataset(tmp_path, monkeypatch)
    monkeypatch.setenv("PM_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("MATRIX_UNIT_KEY", "unit-secret-value")
    monkeypatch.setenv("PM_TARGET_MODEL", "before-model")
    monkeypatch.setenv("PM_TARGET_BASE_URL", "https://before.invalid/v1")
    config = _config(
        tmp_path,
        models=[
            {
                "id": "alpha",
                "model": "actual-alpha",
                "provider": "openai",
                "base_url": "https://alpha.invalid/v1",
                "api_key_env": "MATRIX_UNIT_KEY",
            },
            {"id": "beta", "model": "actual-beta"},
        ],
    )
    seen: list[dict[str, Any]] = []
    import pm.cli.pipeline as pipeline
    from pm.llm.config import build_config

    def fake_run(args, init: dict[str, Any]) -> dict[str, Any]:
        target = build_config("target")
        seen.append(
            {
                "model": target.model,
                "base_url": target.base_url,
                "key": target.api_key,
                "seed_prompt": init["seed_prompt"],
                "case": init["seed_cases"][0]["input"],
            }
        )
        score = 9.0 if target.model == "actual-alpha" else 7.0
        return {
            "run_id": f"run-{len(seen)}",
            "status": "passed",
            "aggregate": {"avg_score": score},
            "baseline_aggregate": {"avg_score": 6.0},
            "pairwise": {"verdict": "tie"},
            "llm_calls": 3,
            "prompt_quality_issues": [],
            "final_report": "# report",
        }

    monkeypatch.setattr(pipeline, "run_pipeline", fake_run)
    assert experiment_command([str(config), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "completed" and len(payload["cells"]) == 4
    assert {row["model"] for row in seen} == {"actual-alpha", "actual-beta"}
    assert {row["case"] for row in seen} == {"文本甲"}, "矩阵没有固定在同一数据集版本"
    alpha = [row for row in seen if row["model"] == "actual-alpha"]
    assert alpha[0]["base_url"] == "https://alpha.invalid/v1"
    assert alpha[0]["key"] == "unit-secret-value"
    assert payload["ranking"][0]["avg"] == 9.0
    assert payload["ranking"][0]["delta"] == 3.0
    assert os.environ["PM_TARGET_MODEL"] == "before-model"
    assert os.environ["PM_TARGET_BASE_URL"] == "https://before.invalid/v1"
    result_dir = Path(payload["result_dir"])
    assert "unit-secret-value" not in (result_dir / "config.json").read_text(encoding="utf-8")
    assert "unit-secret-value" not in (result_dir / "summary.json").read_text(encoding="utf-8")
    replay = json.loads((result_dir / "config.json").read_text(encoding="utf-8"))
    assert replay["dataset"] == "qa:1" and replay["prompts"][0]["prompt"] == "请简洁地回答用户问题"


def test_one_failed_cell_does_not_abort_the_matrix(tmp_path: Path, monkeypatch, capsys) -> None:
    _dataset(tmp_path, monkeypatch)
    monkeypatch.setenv("PM_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("PM_API_KEY", "matrix-secret-value")
    import pm.cli.pipeline as pipeline

    calls = 0

    def fake_run(args, init: dict[str, Any]) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError(f"cell exploded {os.environ['PM_API_KEY']}")
        return {
            "run_id": f"run-{calls}",
            "status": "passed",
            "aggregate": {"avg_score": 8.0},
            "final_report": "# report",
        }

    monkeypatch.setattr(pipeline, "run_pipeline", fake_run)
    assert experiment_command([str(_config(tmp_path)), "--json"]) == 3
    payload = json.loads(capsys.readouterr().out)
    assert calls == 4 and payload["cell_failures"] == 1
    assert payload["status"] == "completed_with_failures"
    assert payload["cells"][0]["error"] == "RuntimeError: cell exploded ***"
    assert "matrix-secret-value" not in Path(payload["summary_path"]).read_text(encoding="utf-8")
    assert Path(payload["report_path"]).exists()


def test_experiment_real_entry_with_fake_backend(tmp_path: Path, monkeypatch) -> None:
    _dataset(tmp_path, monkeypatch)
    config = _config(tmp_path, prompts=["请依据事实简洁回答"], models=["fake-a", "fake-b"])
    root = Path(__file__).resolve().parents[1]
    env = {
        **os.environ,
        "PM_DATASET_DIR": str(tmp_path / "datasets"),
        "PM_LOG_DIR": str(tmp_path / "logs"),
        "PM_FAKE_BACKEND": "progress",
        "PYTHONIOENCODING": "utf-8",
    }
    result = subprocess.run(
        [sys.executable, str(root / "run.py"), "experiment", str(config), "--json"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    assert result.returncode == 0, (result.stdout[-1000:], result.stderr[-1000:])
    payload = json.loads(result.stdout)
    assert payload["status"] == "completed" and len(payload["cells"]) == 2
    assert {cell["status"] for cell in payload["cells"]} <= {
        "passed",
        "max_iterations",
        "early_stopped",
    }
    assert all(Path(cell["log_path"]).exists() for cell in payload["cells"])
    assert Path(payload["report_path"]).exists()


def test_matrix_size_and_duplicate_ids_are_rejected_before_spending(
    tmp_path: Path, monkeypatch
) -> None:
    _dataset(tmp_path, monkeypatch)
    with pytest.raises(ExperimentError, match="最多 20 格"):
        load_spec(
            _config(
                tmp_path,
                prompts=[f"prompt number {i}" for i in range(8)],
                models=[f"m{i}" for i in range(3)],
            )
        )
    with pytest.raises(ExperimentError, match="必须唯一"):
        load_spec(
            _config(tmp_path, models=[{"id": "same", "model": "a"}, {"id": "same", "model": "b"}])
        )
