"""pip 安装态（非检出态）行为的守卫（2026-10-23 修安装态断裂）。

背景（2026-10-22 发布评审实测）：`pip install .` 之后 `pm.cliforward.ROOT`
解析到 site-packages —— run.py 不存在（面板/桌面/MCP/历史的 subprocess 转发
整条断掉）、case_templates 不存在（模板白名单恒为空）、logs 落进 site-packages。
修复形态：入口退到 `python -m pm.cli.console`、模板退到 `sys.prefix/case_templates`
（pyproject data-files 的落点）、产物目录退到 `<cwd>/logs`。

本文件用 monkeypatch 把"安装态"在检出里模拟出来（不需要真装包），
再用一次真实 subprocess 验证退路的入口形状真的能跑。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pm.cliforward as fwd
import pm.paths as paths_mod
import pytest
from pm import __version__

ROOT = Path(__file__).resolve().parents[1]


def test_cli_entry_prefers_repo_run_py_in_checkout() -> None:
    """检出态：打仓库根的 run.py、cwd=仓库根 —— 与旧版行为逐字一致。"""
    argv, cwd = fwd.cli_entry()
    assert argv[0] == sys.executable
    assert argv[1] == str(ROOT / "run.py")
    assert cwd == str(ROOT)


def test_cli_entry_falls_back_to_console_module_when_installed(monkeypatch) -> None:
    """安装态（run.py 不在包旁）：退到 `python -m pm.cli.console`、cwd=继承调用方。

    console 与 run.py 共用同一套启动引导与 main()（pm/cli/console.py），
    .env 的发现顺序本来就是"按当前工作目录向上找"——cwd=None 正是它的前提。
    """
    monkeypatch.setattr(fwd, "RUN_PY", Path("Z:/nonexistent/run.py"))
    argv, cwd = fwd.cli_entry()
    assert argv == [sys.executable, "-m", "pm.cli.console"]
    assert cwd is None


def test_templates_fall_back_to_sys_prefix(monkeypatch, tmp_path) -> None:
    """安装态模板目录：<sys.prefix>/case_templates（data-files 的落点）。

    检出态目录不存在时按名单顺序退到安装前缀，白名单仍然"当场列举"——
    遍历（../../.env）在这条退路上同样被挡。
    """
    repo_side = tmp_path / "repo" / "case_templates"
    prefix_side = tmp_path / "prefix" / "case_templates"
    repo_side.mkdir(parents=True)
    prefix_side.mkdir(parents=True)
    (repo_side / "repo_only.json").write_text("[]", encoding="utf-8")
    (prefix_side / "installed.json").write_text("[]", encoding="utf-8")

    # 只有安装侧可用 → 名单来自安装前缀
    monkeypatch.setattr(fwd, "TEMPLATE_DIR", tmp_path / "repo-missing" / "case_templates")
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "prefix"))
    assert fwd.template_names() == ["installed.json"]
    assert fwd.template_path("installed.json").parent == prefix_side

    # 两侧都在 → 合并去重，解析按"仓库优先"
    monkeypatch.setattr(fwd, "TEMPLATE_DIR", repo_side)
    assert fwd.template_names() == ["installed.json", "repo_only.json"]
    assert fwd.template_path("repo_only.json").parent == repo_side

    # 白名单纪律不因退路放松：不存在的名字与穿越路径一律 TemplateNotFound
    with pytest.raises(fwd.TemplateNotFound):
        fwd.template_path("../../.env")


def test_log_dir_falls_back_to_cwd_when_installed(monkeypatch, tmp_path) -> None:
    """安装态产物目录：<cwd>/logs —— site-packages 可能不可写，更不该当产物目录。"""
    monkeypatch.delenv("PM_LOG_DIR", raising=False)
    fake_pkg = tmp_path / "site-packages" / "pm"
    fake_pkg.mkdir(parents=True)
    monkeypatch.setattr(paths_mod, "__file__", str(fake_pkg / "paths.py"))
    monkeypatch.chdir(tmp_path)
    assert paths_mod.log_dir() == tmp_path / "logs"


def test_log_dir_stays_repo_root_in_checkout(monkeypatch) -> None:
    """检出态：仓库根/logs —— 旧版行为逐字不变（这条不许被安装态修复顺手改掉）。"""
    monkeypatch.delenv("PM_LOG_DIR", raising=False)
    assert paths_mod.log_dir() == ROOT / "logs"


def test_console_module_entry_actually_runs() -> None:
    """退路入口形状的实证：`python -m pm.cli.console` 真能当 CLI 用。

    这里在检出里跑（pm 可导入），验证的是**入口形状**本身——装包态与它的差别
    只在 sys.path 里有没有 pm，代码路径完全相同。"""
    proc = subprocess.run(
        [sys.executable, "-m", "pm.cli.console", "--version"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(ROOT),
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-300:]
    assert __version__ in proc.stdout
