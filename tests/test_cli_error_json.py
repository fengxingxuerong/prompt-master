"""`--json` 的错误出口也必须是一个 JSON 对象（不只是成功时）。

实测缺陷（2026-10-31 审计）：README 的契约是"`--json` 时 stdout 恰好一个 JSON 对象"，
而 `calibrate` 在样本加载失败 / 锚点全失败、`library --export` 在找不到运行时
只往 stderr 打一句、stdout 为空、rc=2。调用方（MCP 的 `_run_cli_json`、桌面端）
**无条件** `json.loads(stdout)`，拿到的却是 `Expecting value: line 1 column 1` ——
一个与真因毫无关系的报错，而真因只在 stderr 里，被 `capture_output` 收走后没人看。

这里从 CLI 入口真跑（不是断言某个助手函数），因为要钉的正是"进程级 stdout 契约"。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUN = [sys.executable, str(ROOT / "run.py")]


def _run(args: list[str], tmp: Path) -> tuple[int, str]:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PM_LOG_DIR": str(tmp)}
    proc = subprocess.run(
        [*RUN, *args],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
        env=env,
    )
    return proc.returncode, proc.stdout


def test_calibrate_json_error_is_a_json_object(tmp_path: Path) -> None:
    rc, out = _run(["calibrate", "--json", "--samples", str(tmp_path / "nope.json")], tmp_path)
    assert rc == 2
    payload = json.loads(out)  # 空串会在这里抛 JSONDecodeError，正是要防的
    assert "样本加载失败" in payload["error"]


def test_calibrate_json_empty_samples_is_a_json_object(tmp_path: Path) -> None:
    empty = tmp_path / "empty.json"
    empty.write_text("[]", encoding="utf-8")
    rc, out = _run(["calibrate", "--json", "--samples", str(empty)], tmp_path)
    assert rc == 2
    assert "error" in json.loads(out)


def test_library_export_missing_run_is_a_json_object(tmp_path: Path) -> None:
    rc, out = _run(["library", "--export", "no_such_run", "--json"], tmp_path)
    assert rc == 2
    payload = json.loads(out)
    assert "找不到运行" in payload["error"]
    assert payload["exit_code"] == 2, "JSON 里要给出退出码，调用方不必再猜"
