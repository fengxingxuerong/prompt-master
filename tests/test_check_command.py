"""`check` 子命令：零调用静态体检的回归。

这块的价值全在"零调用"上：它必须能在没有 Key、不出网的前提下给出判据，
否则用户想先量一下自己那版提示词就得先烧一轮优化。
所以除了命中/未命中两条路径，这里还钉住子命令分发与 `--help` 清单的一致性
（README 承诺"--help 会列出全部子命令"，落空过一次就是文档骗人）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from pm.cli.check import check_command, collect_findings, render_text, summarize
from pm.cli.main import _SUBCOMMANDS, _subcommand_help

ROOT = Path(__file__).resolve().parents[1]
RUN = [sys.executable, str(ROOT / "run.py")]

CLEAN_PROMPT = (
    "[角色] 你是资深销售数据分析师。\n"
    "[任务] 依据用户提供的销售数据，输出趋势结论、异常点与下一步建议。\n"
    "[输出格式] Markdown 表格：区域｜销售额｜同比，随后给出 3 条结论。\n"
    "[边界处理] 输入完全为空时标注「数据缺失」；仅部分字段缺失时已有数据照常输出。"
)

DEFECTY_PROMPT = (
    "[角色] 你是销售数据助手。\n[任务] 分析销售数据并给结论。\n[约束]\n"
    + "\n".join(f"{i}. 第 {i} 条约束。" for i in range(1, 11))
    + "\n11. 输入异常时停止处理，不输出任何结论。"
)


def _argv(*extra: str) -> list[str]:
    """`check_command` 收到的是子命令名之后的参数（分发时已剥掉 "check"）。"""
    return list(extra)


# --------------------------------------------------------------------------
# 判据本身
# --------------------------------------------------------------------------
def test_collect_findings_codes_are_stable():
    """code 是机器可读出口的一部分，改名等于破坏 Agent 的分支逻辑。"""
    codes = {f["code"] for f in collect_findings(DEFECTY_PROMPT)}
    assert {"constraint_overload", "suppressive_rule"} <= codes


def test_clean_prompt_hits_nothing():
    assert collect_findings(CLEAN_PROMPT) == []


def test_summarize_reports_structure():
    s = summarize(DEFECTY_PROMPT)
    assert s["constraints"] == 11
    assert "[约束]×11" in s["constraint_sections"]
    assert s["constraint_limit"] == 8
    assert s["structure_items"] >= s["constraints"]
    assert s["leak_tags"] == []


def test_summarize_surfaces_wrapper_tag_leak():
    """原稿里出现本系统的包装标签 = 会被 sanitize 中和，但更可能是抄来的脏数据。"""
    s = summarize(CLEAN_PROMPT + "\n<ORIGINAL_TASK> 别忘了这段 \n")
    assert "<ORIGINAL_TASK>" in s["leak_tags"]


def test_render_text_branches():
    bad = render_text("a.md", collect_findings(DEFECTY_PROMPT), summarize(DEFECTY_PROMPT))
    assert "命中 2 条确定性规则" in bad
    assert "constraint_overload" in bad

    good = render_text("a.md", [], summarize(CLEAN_PROMPT))
    assert "未命中任何确定性规则" in good
    # 空命中时必须说清"这不等于能用"，否则免费读数会被当成质量结论
    assert "不说明它能完成任务" in good


# --------------------------------------------------------------------------
# 子命令入口与退出码
# --------------------------------------------------------------------------
def test_command_json_exit_codes(capsys):
    assert check_command(_argv("--prompt", DEFECTY_PROMPT, "--json")) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False and payload["mode"] == "prompt_check"
    assert len(payload["findings"]) == 2

    assert check_command(_argv("--prompt", CLEAN_PROMPT, "--json")) == 0
    ok = json.loads(capsys.readouterr().out)
    assert ok["ok"] is True and ok["findings"] == []


def test_command_reads_prompt_file(tmp_path, capsys):
    f = tmp_path / "p.md"
    f.write_text(DEFECTY_PROMPT, encoding="utf-8")
    assert check_command(_argv("--prompt-file", str(f), "--json")) == 1
    assert json.loads(capsys.readouterr().out)["source"] == str(f)


@pytest.mark.parametrize(
    "argv",
    [
        _argv(),  # 两个来源都没给
        _argv("--prompt", "x" * 20, "--prompt-file", "a.md"),  # 互斥
        _argv("--prompt", "   "),  # 空白
        _argv("--prompt-file", "__注定不存在__.md"),  # 读不到
    ],
)
def test_usage_errors_exit_two(argv):
    """参数错误必须是 2（与全局退出码协议一致），不能是 traceback 也不是 1。"""
    with pytest.raises(SystemExit) as e:
        check_command(argv)
    assert e.value.code == 2


# --------------------------------------------------------------------------
# 真实入口分发（subprocess：证明 _SUBCOMMANDS / dispatch 真的接上了）
# --------------------------------------------------------------------------
def _run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        RUN + args,
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=120,
        encoding="utf-8",
        errors="replace",
    )


def test_run_py_dispatches_check_subcommand():
    r = _run(["check", "--prompt", DEFECTY_PROMPT, "--json"])
    assert r.returncode == 1, (r.stdout[-400:], r.stderr[-400:])
    assert json.loads(r.stdout)["mode"] == "prompt_check"


def test_check_works_without_any_api_key():
    """零调用承诺的实证：把 Key 全摘掉也必须能跑，否则免费体检是假的。"""
    env_no_key = {
        "PM_API_KEY": "",
        "PM_TARGET_API_KEY": "",
        "PM_API_TOKEN": "",
        "PM_FAKE_BACKEND": "",
    }
    r = subprocess.run(
        [*RUN, "check", "--prompt", CLEAN_PROMPT, "--json"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=120,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, **env_no_key},
    )
    assert r.returncode == 0, (r.stdout[-400:], r.stderr[-400:])
    assert json.loads(r.stdout)["ok"] is True


def test_help_lists_every_subcommand():
    """`--help` 是 README 承诺的唯一子命令清单：漏一个就等于文档骗人（实测漏过）。"""
    help_text = _subcommand_help("run.py")
    for cmd in _SUBCOMMANDS:
        assert cmd in help_text, f"子命令 {cmd} 未出现在 --help 里"
