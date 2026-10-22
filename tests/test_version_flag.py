"""`--version` 是每个命令行用户的第一个探测动作（2026-10-22 补）。

此前 `run.py --version` 会被当成未知参数、打印 usage 并退 2；装包后的
`prompt-master --version` 同样不可用。版本号的唯一事实源仍是 `pm.__version__`，
这里只钉住三个入口都把它原样报出来。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from pm import __version__

ROOT = Path(__file__).resolve().parents[1]


def _run(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, *args],
        cwd=str(cwd or ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, "PYTHONPATH": str(ROOT), "PYTHONIOENCODING": "utf-8"},
        timeout=60,
    )


def test_run_py_version_prints_single_source_version() -> None:
    proc = _run(str(ROOT / "run.py"), "--version")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == f"run.py {__version__}"


def test_installed_console_module_version() -> None:
    """`pip install .` 后的命令入口与 run.py 共用 main()，版本也必须一致。"""
    proc = _run("-m", "pm.cli.console", "--version")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == f"console.py {__version__}"


def test_run_server_version_does_not_import_uvicorn() -> None:
    """服务启动脚本也要能先看版本；它不该为了报版本把 uvicorn 拉起来。"""
    proc = _run(str(ROOT / "run_server.py"), "--version")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == f"run_server.py {__version__}"
