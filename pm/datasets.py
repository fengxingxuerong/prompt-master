"""Versioned local datasets for repeatable prompt evaluation."""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .assertions import parse_contains_any
from .casesaudit import mode_is_valid, mode_of
from .modelhub.filelock import _atomic_write, _process_lock

MAX_DATASET_CASES = 1000
_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")


class DatasetError(ValueError):
    """Invalid dataset reference or malformed dataset file."""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def default_dir() -> Path:
    raw = os.getenv("PM_DATASET_DIR", "").strip()
    return Path(raw).expanduser() if raw else Path.cwd() / "datasets"


def _validate_name(name: str) -> str:
    value = str(name or "").strip()
    if not _NAME_RE.fullmatch(value):
        raise DatasetError("数据集名称需为 1–64 位字母、数字、点、下划线或连字符，且以字母数字开头")
    return value


def parse_ref(reference: str) -> tuple[str, int | None]:
    value = str(reference or "").strip()
    if ":" in value:
        name, raw_version = value.rsplit(":", 1)
        if not raw_version.isdigit():
            raise DatasetError("数据集版本需为整数，例如 sales:2")
        return _validate_name(name), int(raw_version)
    return _validate_name(value), None


def _path(name: str) -> Path:
    return default_dir() / f"{_validate_name(name)}.json"


def _canonical(cases: list[Any]) -> str:
    return json.dumps(cases, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _normalize_cases(cases: Any) -> list[Any]:
    if not isinstance(cases, list) or not cases:
        raise DatasetError("数据集需为非空 JSON 数组")
    if len(cases) > MAX_DATASET_CASES:
        raise DatasetError(f"数据集最多 {MAX_DATASET_CASES} 条用例")
    normalized: list[Any] = []
    for index, item in enumerate(cases, 1):
        if isinstance(item, str):
            item = {"input": item}
        if not isinstance(item, dict) or not str(item.get("input") or "").strip():
            raise DatasetError(f"第 {index} 条用例缺少非空 input")
        if len(str(item.get("input") or "")) > 8000:
            raise DatasetError(f"第 {index} 条用例 input 超过 8000 字")
        expected = item.get("expected") or ""
        expected_text = (
            json.dumps(expected, ensure_ascii=False)
            if isinstance(expected, list)
            else str(expected)
        )
        if len(expected_text) > 8000:
            raise DatasetError(f"第 {index} 条用例 expected 超过 8000 字")
        normalized.append(item)
    return normalized


def _validate_modes(cases: list[Any]) -> None:
    for index, item in enumerate(cases, 1):
        case = item if isinstance(item, dict) else {"input": item}
        mode = mode_of(case)
        if not mode_is_valid(mode):
            raise DatasetError(f"第 {index} 条用例 mode 非法：{mode}")
        if mode == "contains-any" and parse_contains_any(case.get("expected")) is None:
            raise DatasetError(f"第 {index} 条用例 contains-any 的 expected 需为非空字符串数组")


def _read(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DatasetError(f"数据集文件无法读取：{path}（{exc}）") from exc
    if (
        not isinstance(data, dict)
        or data.get("schema") != 1
        or not isinstance(data.get("versions"), list)
    ):
        raise DatasetError(f"数据集文件形状非法：{path}")
    return data


def add_version(name: str, cases: list[Any], description: str = "") -> dict[str, Any]:
    dataset_name = _validate_name(name)
    normalized = _normalize_cases(cases)
    _validate_modes(normalized)
    path = _path(dataset_name)
    with _process_lock(path):
        if path.exists():
            data = _read(path)
        else:
            data = {
                "schema": 1,
                "name": dataset_name,
                "description": str(description or "").strip(),
                "created_at": _now(),
                "versions": [],
            }
        if description:
            data["description"] = str(description).strip()
        version = len(data["versions"]) + 1
        data["versions"].append(
            {
                "version": version,
                "created_at": _now(),
                "case_count": len(normalized),
                "sha256": hashlib.sha256(_canonical(normalized).encode("utf-8")).hexdigest(),
                "cases": normalized,
            }
        )
        _atomic_write(path, data)
        return {"name": dataset_name, "description": data["description"], **data["versions"][-1]}


def get_dataset(name: str, version: int | None = None) -> dict[str, Any]:
    dataset_name = _validate_name(name)
    data = _read(_path(dataset_name))
    versions = data["versions"]
    selected = (
        versions[-1]
        if version is None
        else next((v for v in versions if v.get("version") == version), None)
    )
    if selected is None:
        raise DatasetError(f"数据集 {dataset_name} 没有版本 {version}")
    return {
        "schema": 1,
        "name": dataset_name,
        "description": data.get("description", ""),
        "created_at": data.get("created_at", ""),
        "version": selected,
    }


def list_datasets() -> list[dict[str, Any]]:
    root = default_dir()
    if not root.exists():
        return []
    rows: list[dict[str, Any]] = []
    for path in sorted(root.glob("*.json")):
        try:
            data = _read(path)
            versions = data["versions"]
            latest = versions[-1] if versions else {}
            rows.append(
                {
                    "name": data.get("name") or path.stem,
                    "description": data.get("description", ""),
                    "versions": len(versions),
                    "latest_version": latest.get("version"),
                    "case_count": latest.get("case_count", 0),
                }
            )
        except DatasetError as exc:
            rows.append({"name": path.stem, "error": str(exc)})
    return rows


def diff_datasets(name: str, from_version: int, to_version: int) -> dict[str, Any]:
    before = get_dataset(name, from_version)["version"]["cases"]
    after = get_dataset(name, to_version)["version"]["cases"]
    added = after[len(before) :]
    removed = before[len(after) :]
    changed = [
        {"case": index, "before": old, "after": new}
        for index, (old, new) in enumerate(zip(before, after, strict=False), 1)
        if old != new
    ]
    return {
        "name": _validate_name(name),
        "from_version": from_version,
        "to_version": to_version,
        "added": added,
        "removed": removed,
        "changed": changed,
        "unchanged": max(0, min(len(before), len(after)) - len(changed)),
    }
