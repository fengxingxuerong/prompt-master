"""`--cases-file` 的模板指引守卫（2026-10-22）。

用户视角（P2-10）：手上有用例集/想写一份，`run.py --help`、`run.py check --help`、
`run.py submit --help` **三处都没说模板在哪** —— 用户只能翻 README 的仓库地图才知道
有 `case_templates/`。修法是把指向收成一份常量（`support.CASES_FILE_HINT`），
三处共用；这三条守卫盯的就是"三处都接上、且真的印在帮助里"。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from pm.cli.support import CASES_FILE_HINT

ROOT = Path(__file__).resolve().parents[1]

_PARSER_SOURCES = ("pm/cli/main.py", "pm/cli/check.py", "pm/cli/agent_mode.py")


def test_the_hint_points_at_the_template_dir() -> None:
    assert "case_templates/" in CASES_FILE_HINT, "指引必须落到具体目录，否则等于没说"


@pytest.mark.parametrize("rel", _PARSER_SOURCES)
def test_every_parser_reuses_the_shared_hint(rel: str) -> None:
    """三处各写一份就会三处不同（2026-10-22 的实测状态：三处都没写）。"""
    src = (ROOT / rel).read_text(encoding="utf-8")
    assert "CASES_FILE_HINT" in src, f"{rel} 的 --cases-file 帮助没接上共用常量"


@pytest.mark.parametrize("argv", [["--help"], ["check", "--help"], ["submit", "--help"]])
def test_the_template_dir_is_visible_in_help(argv: list[str]) -> None:
    """静态接线之外再验一次**真的印出来**（帮助文本可能被别的地方覆盖掉）。"""
    proc = subprocess.run(
        [sys.executable, "run.py", *argv],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert "case_templates/" in (proc.stdout or ""), (
        f"`run.py {' '.join(argv)}` 里看不到模板目录 —— 用户找不到模板"
    )
