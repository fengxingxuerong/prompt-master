"""套件看门狗 .env 加载器回归（M3.3 统一鉴权开启姿势，2026-09-23）。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "watchdog_suite",
    Path(__file__).resolve().parents[1] / "releases" / "suite" / "watchdog_suite.py",
)
wd = importlib.util.module_from_spec(_spec)
sys.modules["watchdog_suite"] = wd
_spec.loader.exec_module(wd)


def test_parse_simple_and_quotes(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# 注释行\n"
        "\n"
        "TRIAGE_TOKEN=abc-123\n"
        'LOBSTER_TOKEN="quoted-value"\n'
        "TASKBOARD_TOKEN='sq-value'\n"
        "BAD_LINE_NO_EQUALS\n"
        "EMPTY=\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(wd, "ROOT", tmp_path / "releases")
    out = wd._load_dotenv_env()
    assert out["TRIAGE_TOKEN"] == "abc-123"
    assert out["LOBSTER_TOKEN"] == "quoted-value", "成对双引号应剥离"
    assert out["TASKBOARD_TOKEN"] == "sq-value", "成对单引号应剥离"
    assert "EMPTY" in out and out["EMPTY"] == ""
    assert all("=" not in k for k in out)


def test_existing_system_env_wins(tmp_path, monkeypatch):
    """系统环境 > .env 文件（setdefault 语义，不覆盖已有变量）。"""
    env_file = tmp_path / ".env"
    env_file.write_text("TRIAGE_TOKEN=from-file\n", encoding="utf-8")
    monkeypatch.setattr(wd, "ROOT", tmp_path / "releases")
    monkeypatch.setenv("TRIAGE_TOKEN", "from-system")
    merged = {**wd._load_dotenv_env(), **{"TRIAGE_TOKEN": "from-system"}}
    assert merged["TRIAGE_TOKEN"] == "from-system"


def test_missing_env_file_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(wd, "ROOT", tmp_path / "releases")  # .env 不存在
    assert wd._load_dotenv_env() == {}
