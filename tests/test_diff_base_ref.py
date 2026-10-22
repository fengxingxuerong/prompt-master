"""`diff --base-ref`：用户仓库里的提示词 CI 门禁（2026-10-22 新增）。

竞品对照：promptfoo / PromptLayer 把"提示词进版本库、每次 PR 跑增量断言"当作核心用法。
本产品原有 `diff A B` 需要用户自己 `git show > /tmp/base.md` 再比，多一步就少一半人用。
这里钉住三件事：
1. 基线取自 **git 历史**，不是工作区另一份文件；
2. 退出码仍是"有新引入才 1"（修好不判失败）；
3. 新文件 / 坏 ref / 参数写错都退 2 并给人话，不能静默放行。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
from pm.cli.diffcmd import diff_command, resolve_git_base
from pm.promptdiff import DiffSourceError

ROOT = Path(__file__).resolve().parents[1]

CLEAN = (
    "[角色] 你是资深销售数据分析师。\n"
    "[任务] 依据用户提供的销售数据，输出趋势结论、异常点与下一步建议。\n"
    "[输出格式] Markdown 表格：区域｜销售额｜同比，随后给出 3 条结论。\n"
    "[边界处理] 输入完全为空时标注「数据缺失」；仅部分字段缺失时已有数据照常输出。"
)

REGRESSED = (
    "[角色] 你是资深销售数据分析师，负责把区域销售数据整理成可决策的结论。\n"
    "[任务] 依据用户提供的销售数据，输出趋势结论、异常点与下一步建议三部分内容。\n"
    "[输出格式] Markdown 表格：区域｜销售额｜同比，随后给出 3 条结论。\n"
    "[判断规则] 所有结论必须引用输入中的具体数值，禁止只给定性描述。\n"
    "[边界处理] 输入异常时停止处理，不输出任何结论。\n"
    "[补充说明] 结论按重要性排序，每条不超过 50 字，避免重复表述同一现象。"
)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )


def _cli(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(ROOT / "run.py"), "diff", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "user-repo"
    (repo / "prompts").mkdir(parents=True)
    _git(repo.parent, "init", "-q", str(repo))
    _git(repo, "config", "user.email", "ci@example.invalid")
    _git(repo, "config", "user.name", "CI")
    (repo / "prompts" / "p.md").write_text(CLEAN, encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    return repo


def test_uncommitted_regression_against_head_is_exit_1(tmp_path) -> None:
    repo = _repo(tmp_path)
    (repo / "prompts" / "p.md").write_text(REGRESSED, encoding="utf-8")

    proc = _cli(repo, "--base-ref", "HEAD", "prompts/p.md", "--json")
    assert proc.returncode == 1, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["mode"] == "prompt_diff"
    assert payload["ok"] is False and payload["regressed"] is True
    assert payload["base_ref"] == "HEAD"
    assert payload["a"] == "HEAD:prompts/p.md"
    assert "suppressive_rule" in payload["rules"]["introduced"]


def test_fixed_version_against_base_is_exit_0(tmp_path) -> None:
    repo = _repo(tmp_path)
    # 先提交一版脏的，再在工作区改干净：相对 HEAD 是"修好"，不是回归
    (repo / "prompts" / "p.md").write_text(REGRESSED, encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "dirty")
    (repo / "prompts" / "p.md").write_text(CLEAN, encoding="utf-8")

    proc = _cli(repo, "--base-ref", "HEAD", "prompts/p.md", "--json")
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["ok"] is True
    assert payload["rules"]["fixed"], "修好的规则要如实列出，但不得据此判失败"


def test_new_file_has_no_base_and_must_not_fail_the_gate(tmp_path) -> None:
    """新文件没有旧版本可比：退 2 并建议先 check，不能把'新增'当'回归'判红。"""
    repo = _repo(tmp_path)
    (repo / "prompts" / "new.md").write_text(REGRESSED, encoding="utf-8")

    proc = _cli(repo, "--base-ref", "HEAD", "prompts/new.md")
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "没有可对比的旧版本" in proc.stderr or "没有" in proc.stderr


def test_bad_ref_and_usage_errors_are_exit_2(tmp_path) -> None:
    repo = _repo(tmp_path)
    (repo / "prompts" / "p.md").write_text(REGRESSED, encoding="utf-8")

    bad_ref = _cli(repo, "--base-ref", "does-not-exist", "prompts/p.md")
    assert bad_ref.returncode == 2
    assert "解析不出来" in bad_ref.stderr

    two_files = _cli(repo, "--base-ref", "HEAD", "prompts/p.md", "prompts/p.md")
    assert two_files.returncode == 2
    assert "只需要一个文件参数" in two_files.stderr

    no_file = _cli(repo, "--base-ref", "HEAD")
    assert no_file.returncode == 2


# --------------------------------------------------------------------------
# 进程内单测：上面用 subprocess 打真实入口（测退出码契约）；这里直接调函数，
# 把每个"给用户的可操作报错"分支都走到（否则覆盖率只看见 happy path）。
# --------------------------------------------------------------------------
def test_resolve_git_base_reads_the_committed_text(tmp_path) -> None:
    repo = _repo(tmp_path)
    src = resolve_git_base(str(repo / "prompts" / "p.md"), "HEAD")
    assert src.kind == "git"
    assert src.label == "HEAD:prompts/p.md"
    assert src.text == CLEAN


def test_resolve_git_base_rejects_empty_ref_and_non_file(tmp_path) -> None:
    repo = _repo(tmp_path)
    with pytest.raises(DiffSourceError, match="不能为空"):
        resolve_git_base(str(repo / "prompts" / "p.md"), "   ")
    with pytest.raises(DiffSourceError, match="不是可读文件"):
        resolve_git_base(str(repo / "prompts" / "nope.md"), "HEAD")


def _dir_outside_any_repo() -> Path:
    """返回一个**本身不在任何 git 工作树里**的可写父目录。

    检出目录的父目录优先（CI 与开发机上都在仓库之外），退到系统临时目录。
    """
    for base in (ROOT.parent, Path(tempfile.gettempdir())):
        if not any((p / ".git").exists() for p in (base, *base.parents)):
            return base
    raise AssertionError("找不到一个仓库外的可写目录")


def test_resolve_git_base_rejects_files_outside_a_repository() -> None:
    # ⚠️ 这里**不能**用 tmp_path：本仓根 conftest 在 Windows 上把 pytest 临时根**首选**
    #    放进检出目录里的 `.pytest_tmp/`（绕开宿主 %TEMP% 的坏软链），于是 tmp_path 可能
    #    落在仓库内 —— 那样 `git rev-parse` 会成功，走的就不是"仓库外"这条分支。
    #    CI Windows 实测踩中：报的是"基线 HEAD 里没有"而不是"不在 git 仓库里"。
    scratch = _dir_outside_any_repo() / f"pm-loose-diffbase-{os.getpid()}"
    scratch.mkdir(parents=True, exist_ok=True)
    try:
        loose = scratch / "loose.md"
        loose.write_text(CLEAN, encoding="utf-8")
        with pytest.raises(DiffSourceError, match="不在 git 仓库里"):
            resolve_git_base(str(loose), "HEAD")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def test_resolve_git_base_rejects_missing_and_empty_base_file(tmp_path) -> None:
    repo = _repo(tmp_path)
    new = repo / "prompts" / "new.md"
    new.write_text(REGRESSED, encoding="utf-8")
    with pytest.raises(DiffSourceError, match="没有可对比的旧版本"):
        resolve_git_base(str(new), "HEAD")

    empty = repo / "prompts" / "empty.md"
    empty.write_text("\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "empty")
    with pytest.raises(DiffSourceError, match="空文件"):
        resolve_git_base(str(empty), "HEAD")


def test_diff_command_base_ref_in_process_json_and_usage(tmp_path, capsys) -> None:
    repo = _repo(tmp_path)
    target = repo / "prompts" / "p.md"
    target.write_text(REGRESSED, encoding="utf-8")

    assert diff_command(["--base-ref", "HEAD", str(target), "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["base_ref"] == "HEAD" and payload["regressed"] is True

    with pytest.raises(SystemExit) as e:
        diff_command([])
    assert e.value.code == 2
    assert "两个来源" in capsys.readouterr().err
