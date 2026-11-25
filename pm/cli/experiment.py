"""Sequential prompt × model experiments over one immutable dataset version."""

from __future__ import annotations

import argparse
import json
import os
import re
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pm.datasets import DatasetError, get_dataset, parse_ref
from pm.modelhub.filelock import _atomic_write
from pm.state import SEED_PROMPT_MAX_CHARS, initial_state
from pm.textio import TextDecodeError, read_text_auto

from .support import (
    EXIT_CONFIG,
    EXIT_FAILED,
    log_dir,
    read_seed_prompt,
    save_artifacts,
    setup_logging,
)

MAX_PROMPTS = 8
MAX_MODELS = 8
MAX_CELLS = 20
_SAFE_ID = re.compile(r"[^A-Za-z0-9_.-]+")


class ExperimentError(ValueError):
    """Invalid experiment configuration."""


@dataclass(frozen=True)
class PromptSpec:
    id: str
    prompt: str


@dataclass(frozen=True)
class ModelSpec:
    id: str
    model: str
    provider: str
    base_url: str
    api_key_env: str


@dataclass(frozen=True)
class ExperimentSpec:
    name: str
    task: str
    context: str
    dataset_ref: str
    dataset_version: int
    dataset_sha256: str
    prompts: tuple[PromptSpec, ...]
    models: tuple[ModelSpec, ...]
    samples: int
    max_iter: int
    baseline: bool
    pairwise: bool


def _text(value: Any, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ExperimentError(f"{field} 需为非空字符串")
    result = value.strip()
    if len(result) > maximum:
        raise ExperimentError(f"{field} 超过 {maximum} 字")
    return result


def _boolean(value: Any, field: str, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ExperimentError(f"{field} 需为布尔值")
    return value


def _integer(value: Any, field: str, default: int, low: int, high: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ExperimentError(f"{field} 需为 {low}-{high} 的整数")
    return value


def _load_prompt(item: Any, index: int, base: Path) -> PromptSpec:
    raw_id: Any
    prompt: Any
    source: Any
    if isinstance(item, str):
        raw_id, prompt, source = f"p{index}", item, None
    elif isinstance(item, dict):
        raw_id = item.get("id") or f"p{index}"
        prompt = item.get("prompt")
        source = item.get("file")
        if prompt is not None and source is not None:
            raise ExperimentError(f"prompts[{index}] 的 prompt 与 file 互斥")
        if source is not None:
            path = Path(_text(source, f"prompts[{index}].file", 1000))
            if not path.is_absolute():
                path = base / path
            try:
                prompt = read_text_auto(path).text
            except (OSError, TextDecodeError) as exc:
                raise ExperimentError(f"prompts[{index}].file 读取失败：{exc}") from exc
    else:
        raise ExperimentError(f"prompts[{index}] 需为字符串或对象")
    identifier = _SAFE_ID.sub("-", _text(raw_id, f"prompts[{index}].id", 64)).strip("-.")
    if not identifier:
        raise ExperimentError(f"prompts[{index}].id 没有可用字符")
    value = _text(prompt, f"prompts[{index}]", SEED_PROMPT_MAX_CHARS)
    if len(value) < 8:
        raise ExperimentError(f"prompts[{index}] 至少 8 字；需求描述请放在 task")
    return PromptSpec(identifier, value)


def _load_model(item: Any, index: int) -> ModelSpec:
    identifier: Any
    model: Any
    provider: Any
    base_url: Any
    api_key_env: Any
    if isinstance(item, str):
        identifier = model = item
        provider = base_url = api_key_env = ""
    elif isinstance(item, dict):
        identifier = item.get("id") or item.get("model")
        model = item.get("model") or identifier
        provider = item.get("provider") or ""
        base_url = item.get("base_url") or ""
        api_key_env = item.get("api_key_env") or ""
        if "api_key" in item:
            raise ExperimentError("models[] 不接受 api_key 明文；请用 api_key_env 引用环境变量")
    else:
        raise ExperimentError(f"models[{index}] 需为字符串或对象")
    safe_id = _SAFE_ID.sub("-", _text(identifier, f"models[{index}].id", 64)).strip("-.")
    if not safe_id:
        raise ExperimentError(f"models[{index}].id 没有可用字符")
    key_name = _text(api_key_env, f"models[{index}].api_key_env", 120) if api_key_env else ""
    if key_name and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key_name):
        raise ExperimentError(f"models[{index}].api_key_env 需为合法环境变量名")
    if key_name and not os.getenv(key_name, "").strip():
        raise ExperimentError(f"models[{index}].api_key_env 指向的 {key_name} 未设置")
    return ModelSpec(
        safe_id,
        _text(model, f"models[{index}].model", 200),
        _text(provider, f"models[{index}].provider", 40) if provider else "",
        _text(base_url, f"models[{index}].base_url", 1000) if base_url else "",
        key_name,
    )


def load_spec(path: Path) -> ExperimentSpec:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ExperimentError(f"实验配置无法读取：{path}（{exc}）") from exc
    if not isinstance(raw, dict):
        raise ExperimentError("实验配置顶层需为 JSON 对象")
    task = _text(raw.get("task"), "task", 8000)
    if len(task) < 4:
        raise ExperimentError("task 至少 4 字")
    dataset_ref = _text(raw.get("dataset"), "dataset", 100)
    try:
        name, version = parse_ref(dataset_ref)
        dataset = get_dataset(name, version)
        selected = dataset["version"]
    except DatasetError as exc:
        raise ExperimentError(str(exc)) from exc
    raw_prompts = raw.get("prompts")
    raw_models = raw.get("models")
    if not isinstance(raw_prompts, list) or not 1 <= len(raw_prompts) <= MAX_PROMPTS:
        raise ExperimentError(f"prompts 需为 1-{MAX_PROMPTS} 项")
    if not isinstance(raw_models, list) or not 1 <= len(raw_models) <= MAX_MODELS:
        raise ExperimentError(f"models 需为 1-{MAX_MODELS} 项")
    prompts = tuple(_load_prompt(item, i, path.parent) for i, item in enumerate(raw_prompts, 1))
    if len({item.id for item in prompts}) != len(prompts):
        raise ExperimentError("prompts[].id 必须唯一")
    models = tuple(_load_model(item, i) for i, item in enumerate(raw_models, 1))
    if len({item.id for item in models}) != len(models):
        raise ExperimentError("models[].id 必须唯一")
    if len(prompts) * len(models) > MAX_CELLS:
        raise ExperimentError(f"矩阵最多 {MAX_CELLS} 格（{len(prompts)} × {len(models)}）")
    version = int(selected["version"])
    return ExperimentSpec(
        name=_text(raw.get("name") or path.stem, "name", 120),
        task=task,
        context=_text(raw.get("context") or "", "context", 8000) if raw.get("context") else "",
        dataset_ref=f"{dataset['name']}:{version}",
        dataset_version=version,
        dataset_sha256=str(selected["sha256"]),
        prompts=prompts,
        models=models,
        samples=_integer(raw.get("samples"), "samples", 2, 1, 5),
        max_iter=_integer(raw.get("max_iter"), "max_iter", 3, 0, 10),
        baseline=_boolean(raw.get("baseline"), "baseline", True),
        pairwise=_boolean(raw.get("pairwise"), "pairwise", False),
    )


def _public_spec(spec: ExperimentSpec) -> dict[str, Any]:
    return {
        "name": spec.name,
        "task": spec.task,
        "context": spec.context,
        "dataset": spec.dataset_ref,
        "dataset_version": spec.dataset_version,
        "dataset_sha256": spec.dataset_sha256,
        "prompts": [{"id": item.id, "chars": len(item.prompt)} for item in spec.prompts],
        "models": [
            {
                "id": item.id,
                "model": item.model,
                "provider": item.provider,
                "base_url": item.base_url,
                "api_key_env": item.api_key_env,
            }
            for item in spec.models
        ],
        "samples": spec.samples,
        "max_iter": spec.max_iter,
        "baseline": spec.baseline,
        "pairwise": spec.pairwise,
    }


@contextmanager
def _measurement_env(spec: ExperimentSpec) -> Iterator[None]:
    keys = ("PM_SAMPLES_PER_CASE", "PM_BASELINE", "PM_PAIRWISE")
    previous = {key: os.environ.get(key) for key in keys}
    os.environ["PM_SAMPLES_PER_CASE"] = str(spec.samples)
    os.environ["PM_BASELINE"] = "1" if spec.baseline else "0"
    os.environ["PM_PAIRWISE"] = "1" if spec.pairwise else "0"
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@contextmanager
def _model_env(model: ModelSpec) -> Iterator[None]:
    values = {"PM_TARGET_MODEL": model.model}
    if model.provider:
        values["PM_TARGET_PROVIDER"] = model.provider
    if model.base_url:
        values["PM_TARGET_BASE_URL"] = model.base_url
    if model.api_key_env:
        values["PM_TARGET_API_KEY"] = os.environ[model.api_key_env]
    previous = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _cell_args(
    spec: ExperimentSpec, prompt: PromptSpec, model: ModelSpec, report: Path
) -> argparse.Namespace:
    from .main import _build_parser

    argv = [
        "--task",
        spec.task,
        "--dataset",
        spec.dataset_ref,
        "--target-model",
        model.model,
        "--prompt",
        prompt.prompt,
        "--samples",
        str(spec.samples),
        "--max-iter",
        str(spec.max_iter),
        "--yes",
        "--json",
        "--out",
        str(report),
    ]
    if spec.context:
        argv.extend(["--context", spec.context])
    if not spec.baseline:
        argv.append("--no-baseline")
    if not spec.pairwise:
        argv.append("--no-pairwise")
    return _build_parser("run.py").parse_args(argv)


def _estimate(spec: ExperimentSpec, report: Path) -> dict[str, Any]:
    from .main import _apply_measurement_flags, _build_parser, _estimate_calls, _load_seed_cases

    with _measurement_env(spec), _model_env(spec.models[0]):
        parser = _build_parser("run.py")
        args = _cell_args(spec, spec.prompts[0], spec.models[0], report)
        _apply_measurement_flags(parser, args)
        estimate = _estimate_calls(args, _load_seed_cases(parser, args))
    count = len(spec.prompts) * len(spec.models)
    return {
        "cells": count,
        "per_cell": estimate["estimated_llm_calls"],
        "total": {key: value * count for key, value in estimate["estimated_llm_calls"].items()},
        "notes": estimate["notes"],
    }


def _score(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _cell_summary(
    final: dict[str, Any], prompt: PromptSpec, model: ModelSpec, log: Path, report: Path
) -> dict[str, Any]:
    aggregate = final.get("aggregate") or {}
    baseline = final.get("baseline_aggregate") or {}
    avg = _score(aggregate.get("avg_score"))
    base_avg = _score(baseline.get("avg_score"))
    return {
        "prompt_id": prompt.id,
        "model": model.id,
        "target_model": model.model,
        "run_id": final.get("run_id"),
        "status": final.get("status"),
        "avg": avg,
        "baseline_avg": base_avg,
        "delta": round(avg - base_avg, 3) if avg is not None and base_avg is not None else None,
        "pairwise": final.get("pairwise"),
        "llm_calls": final.get("llm_calls", 0),
        "quality_issues": len(final.get("prompt_quality_issues") or []),
        "log_path": str(log),
        "report_path": str(report),
    }


def _run_cell(
    spec: ExperimentSpec, prompt: PromptSpec, model: ModelSpec, root: Path
) -> dict[str, Any]:
    from .main import (
        _apply_measurement_flags,
        _build_parser,
        _load_seed_cases,
        _require_task,
    )
    from .pipeline import run_pipeline

    slug = _SAFE_ID.sub("-", model.id).strip("-.") or "model"
    report = root / f"report_{prompt.id}_{slug}.md"
    parser = _build_parser("run.py")
    args = _cell_args(spec, prompt, model, report)
    _apply_measurement_flags(parser, args)
    task = _require_task(parser, args)
    seed_cases = _load_seed_cases(parser, args)
    seed_prompt = read_seed_prompt(parser, args)
    init = initial_state(
        task=task,
        context=args.context,
        target_model=model.model,
        n_test_cases=args.cases,
        max_iterations=args.max_iter,
        auto_clarify=True,
        seed_cases=seed_cases,
        assertion_mode=args.assert_mode,
        seed_prompt=seed_prompt,
    )
    with _model_env(model):
        final = run_pipeline(args, init)
    log, saved_report = save_artifacts(final, report)
    return _cell_summary(final, prompt, model, log, saved_report)


def _descending(cell: dict[str, Any], field: str) -> float:
    value = _score(cell.get(field))
    return -(value if value is not None else float("-inf"))


def _rank(cells: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        cells,
        key=lambda cell: (
            cell.get("avg") is None,
            _descending(cell, "avg"),
            cell.get("delta") is None,
            _descending(cell, "delta"),
            str(cell.get("prompt_id")),
            str(cell.get("model")),
        ),
    )


def _md(value: Any) -> str:
    return str(value if value is not None else "-").replace("|", "\\|").replace("\n", " ")


def _render_markdown(summary: dict[str, Any]) -> str:
    lines = [
        f"# 实验矩阵：{summary['name']}",
        "",
        f"- 数据集：`{summary['config']['dataset']}`（SHA256 `{summary['config']['dataset_sha256']}`）",
        f"- 规模：{len(summary['config']['prompts'])} 个提示词 × {len(summary['config']['models'])} 个模型",
        f"- 状态：{summary['status']}；失败格：{summary['cell_failures']}",
        "",
        "| 排名 | 提示词 | 模型 | 均分 | 基线 | Δ | 盲评 | 状态 | 调用 |",
        "| ---: | --- | --- | ---: | ---: | ---: | --- | --- | ---: |",
    ]
    for index, cell in enumerate(summary["ranking"], 1):
        pairwise = (
            (cell.get("pairwise") or {}).get("verdict")
            if isinstance(cell.get("pairwise"), dict)
            else None
        )
        values = (
            index,
            cell.get("prompt_id"),
            cell.get("model"),
            cell.get("avg"),
            cell.get("baseline_avg"),
            cell.get("delta"),
            pairwise or "-",
            cell.get("status") or "failed",
            cell.get("llm_calls", 0),
        )
        lines.append("| " + " | ".join(_md(value) for value in values) + " |")
    lines.extend(
        [
            "",
            "分数与 Δ 仅作同数据集下的方向参考；没有显著性检验，不把排名当成统计结论。",
            "逐格报告与完整状态见实验目录。",
        ]
    )
    return "\n".join(lines) + "\n"


def _replay_config(spec: ExperimentSpec) -> dict[str, Any]:
    return {
        "name": spec.name,
        "task": spec.task,
        "context": spec.context,
        "dataset": spec.dataset_ref,
        "prompts": [{"id": item.id, "prompt": item.prompt} for item in spec.prompts],
        "models": [
            {
                "id": item.id,
                "model": item.model,
                "provider": item.provider,
                "base_url": item.base_url,
                "api_key_env": item.api_key_env,
            }
            for item in spec.models
        ],
        "samples": spec.samples,
        "max_iter": spec.max_iter,
        "baseline": spec.baseline,
        "pairwise": spec.pairwise,
    }


def _error_text(exc: Exception) -> str:
    text = f"{type(exc).__name__}: {exc}"
    for name, value in os.environ.items():
        upper = name.upper()
        if upper.endswith("_API_KEY") or upper == "PM_API_KEYS":
            for secret in value.split(","):
                if len(secret.strip()) >= 8:
                    text = text.replace(secret.strip(), "***")
    return text


def _execute(spec: ExperimentSpec) -> dict[str, Any]:
    experiment_id = uuid.uuid4().hex[:12]
    directory = log_dir() / "experiments" / experiment_id
    directory.mkdir(parents=True, exist_ok=False)
    summary: dict[str, Any] = {
        "experiment_id": experiment_id,
        "name": spec.name,
        "status": "running",
        "config": _public_spec(spec),
        "cells": [],
        "cell_failures": 0,
        "result_dir": str(directory),
    }
    summary_path = directory / "summary.json"
    _atomic_write(directory / "config.json", _replay_config(spec))
    _atomic_write(summary_path, summary)
    with _measurement_env(spec):
        for prompt in spec.prompts:
            for model in spec.models:
                try:
                    cell = _run_cell(spec, prompt, model, directory)
                except Exception as exc:  # noqa: BLE001 - 逐格隔离，失败证据必须落盘
                    cell = {
                        "prompt_id": prompt.id,
                        "model": model.id,
                        "target_model": model.model,
                        "status": "failed",
                        "error": _error_text(exc),
                    }
                    summary["cell_failures"] += 1
                else:
                    if cell.get("status") == "failed":
                        summary["cell_failures"] += 1
                summary["cells"].append(cell)
                summary["ranking"] = _rank(summary["cells"])
                _atomic_write(summary_path, summary)
    summary["status"] = "completed" if not summary["cell_failures"] else "completed_with_failures"
    summary["ranking"] = _rank(summary["cells"])
    report_path = directory / "summary.md"
    report_path.write_text(_render_markdown(summary), encoding="utf-8")
    summary["summary_path"] = str(summary_path)
    summary["report_path"] = str(report_path)
    _atomic_write(summary_path, summary)
    return summary


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run.py experiment", description="同数据集多提示词多模型实验矩阵"
    )
    parser.add_argument("config", help="JSON 配置路径")
    parser.add_argument("--dry-run", action="store_true", help="只估算调用数，不运行、不落盘")
    parser.add_argument("--json", action="store_true", help="stdout 只输出汇总 JSON")
    return parser


def experiment_command(argv: list[str]) -> int:
    args = _build_parser().parse_args(argv)
    try:
        spec = load_spec(Path(args.config))
        with _measurement_env(spec):
            estimate = _estimate(spec, Path("experiment-report.md"))
        if args.dry_run:
            payload = {"mode": "experiment_dry_run", "config": _public_spec(spec), **estimate}
        else:
            setup_logging(False)
            payload = _execute(spec)
    except (ExperimentError, DatasetError) as exc:
        print(f"实验配置错误：{exc}")
        return EXIT_CONFIG
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    elif args.dry_run:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(
            f"实验 {payload['experiment_id']}：{payload['status']}，失败 {payload['cell_failures']} 格"
        )
        print(f"汇总：{payload['summary_path']}")
        print(f"报告：{payload['report_path']}")
        for cell in payload["ranking"]:
            print(
                f"  {cell.get('prompt_id')} × {cell.get('model')}  "
                f"均分 {cell.get('avg')}  Δ {cell.get('delta')}  {cell.get('status')}"
            )
    return EXIT_FAILED if payload.get("cell_failures") else 0
