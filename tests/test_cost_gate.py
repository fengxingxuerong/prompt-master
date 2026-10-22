"""真实运行的**成本闸门**（2026-10-22 新增）：交互终端下先报价、再要一句显式确认。

事故形状：`.env` 里有真 Key 的人敲下 `run.py --task …` 就直接开跑几十次计费调用，
而 README 只在一句括号里写着"先 --preflight"。桌面端早有"提交前把预估区间写进确认框"，
CLI 没有——这里把 CLI 那份补齐。

本文件钉三件事：
1. **只在"能问人"时拦**（TTY 且非 `--json` 且非假后端）；非交互一律直接放行，
   否则会把 Agent / CI / 管道卡死在没人能回答的提示上。
2. **报价与 `--dry-run` 同源**（同一个 `_estimate_calls`），不各算一份。
3. **Key 闸门在成本闸门之前**：没 Key 的人不该先被问钱。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from pm.cli.main import (
    _KEY_ROLES,
    _build_parser,
    _confirm_expensive_run,
    _dry_run_exit,
    _estimate_calls,
    _run_preconditions,
)

ROOT = Path(__file__).resolve().parents[1]


def _args(*extra: str) -> argparse.Namespace:
    return _build_parser("run.py").parse_args(["--task", "让 AI 分析销售数据", *extra])


def _force_tty(monkeypatch: pytest.MonkeyPatch, tty: bool) -> None:
    """只改 isatty 的返回值，不换 sys.stdin/stdout 对象（换对象会打断 capsys 的捕获）。"""
    monkeypatch.setattr(sys.stdin, "isatty", lambda: tty)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: tty)


def _clear_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("PM_API_KEY", "PM_API_KEYS", "PM_FAKE_BACKEND"):
        monkeypatch.delenv(name, raising=False)
    for role in _KEY_ROLES:
        monkeypatch.delenv(f"PM_{role.upper()}_API_KEY", raising=False)


# ---------------------------------------------------------------------------
# 判据一：报价与 --dry-run 同一个事实源
# ---------------------------------------------------------------------------
def test_estimate_is_the_single_source_for_dry_run(capsys: pytest.CaptureFixture[str]) -> None:
    args = _args()
    assert _dry_run_exit(args, []) == 0
    printed = json.loads(capsys.readouterr().out)
    live = _estimate_calls(args, [])
    assert printed["estimated_llm_calls"] == live["estimated_llm_calls"]
    assert printed["cases"] == live["cases"] == 3


def test_seed_cases_change_the_estimate() -> None:
    """给了 --cases-file 就不跑 mockgen：报价必须跟着变，否则报价是假的。"""
    args = _args()
    without = _estimate_calls(args, [])["estimated_llm_calls"]["min"]
    with_seed = _estimate_calls(args, [{"input": "a"}])["estimated_llm_calls"]["min"]
    assert with_seed == without - 1


# ---------------------------------------------------------------------------
# 判据二：只在"能问人"时拦
# ---------------------------------------------------------------------------
def test_non_tty_passes_without_prompting(monkeypatch: pytest.MonkeyPatch) -> None:
    _force_tty(monkeypatch, False)
    monkeypatch.setattr("builtins.input", lambda *a, **k: pytest.fail("非 TTY 不该问"))
    assert _confirm_expensive_run(_args(), []) is True


def test_json_agent_mode_passes_without_prompting(monkeypatch: pytest.MonkeyPatch) -> None:
    _force_tty(monkeypatch, True)
    monkeypatch.setattr("builtins.input", lambda *a, **k: pytest.fail("--json 不该问"))
    assert _confirm_expensive_run(_args("--json"), []) is True


def test_yes_flag_passes_without_prompting(monkeypatch: pytest.MonkeyPatch) -> None:
    _force_tty(monkeypatch, True)
    monkeypatch.setattr("builtins.input", lambda *a, **k: pytest.fail("--yes 不该问"))
    assert _confirm_expensive_run(_args("--yes"), []) is True


def test_fake_backend_passes_without_prompting(monkeypatch: pytest.MonkeyPatch) -> None:
    """假后端整轮不出网、零成本 —— 没什么可确认的。"""
    _force_tty(monkeypatch, True)
    monkeypatch.setenv("PM_FAKE_BACKEND", "progress")
    monkeypatch.setattr("builtins.input", lambda *a, **k: pytest.fail("假后端不该问"))
    assert _confirm_expensive_run(_args(), []) is True


# ---------------------------------------------------------------------------
# 判据三：TTY 下的三态（确认 / 取消 / EOF）
# ---------------------------------------------------------------------------
def test_tty_quotes_then_accepts(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    _force_tty(monkeypatch, True)
    monkeypatch.setattr("builtins.input", lambda *a, **k: "y")
    assert _confirm_expensive_run(_args(), []) is True
    err = capsys.readouterr().err
    assert "真实运行" in err and "LLM 调用" in err, err
    assert "--dry-run" in err and "--yes" in err, "报价里要给出退路与跳过开关"


def test_tty_cancel_is_a_clean_refusal(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    _force_tty(monkeypatch, True)
    monkeypatch.setattr("builtins.input", lambda *a, **k: "")
    assert _confirm_expensive_run(_args(), []) is False
    assert "已取消" in capsys.readouterr().err


def test_tty_eof_counts_as_cancel(monkeypatch: pytest.MonkeyPatch) -> None:
    """管道里读到 EOF（没人回答）按取消处理，不能默认开跑。"""

    def _raise(*a: object, **k: object) -> str:
        raise EOFError

    _force_tty(monkeypatch, True)
    monkeypatch.setattr("builtins.input", _raise)
    assert _confirm_expensive_run(_args(), []) is False


# ---------------------------------------------------------------------------
# 判据四：Key 闸门在前，成本闸门在后
# ---------------------------------------------------------------------------
def test_key_gate_runs_before_the_cost_gate(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """没 Key 的人不该先被问钱：先报"没 Key"，再由有 Key 的人回答"要不要花钱"。"""
    _clear_keys(monkeypatch)
    _force_tty(monkeypatch, True)
    monkeypatch.setattr("builtins.input", lambda *a, **k: pytest.fail("没 Key 就不该问成本"))
    p = _build_parser("run.py")
    assert _run_preconditions(p, _args(), []) is False
    assert "未检测到 PM_API_KEY" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# 端到端：非交互路径**行为零变化**（这条错了会把 Agent / CI 卡死）
# ---------------------------------------------------------------------------
def test_cli_non_interactive_is_unchanged() -> None:
    """管道调用 run.py：不弹确认、直接走到 Key 闸门并退 2（与加闸门之前逐字一致）。"""
    env = {
        **os.environ,
        "PYTHONIOENCODING": "utf-8",
        "PM_API_KEY": "",
        "PM_API_KEYS": "",
        "PM_BASE_URL": "http://127.0.0.1:9/v1",
    }
    for role in _KEY_ROLES:
        env[f"PM_{role.upper()}_API_KEY"] = ""
    proc = subprocess.run(
        [sys.executable, "run.py", "--task", "让 AI 分析销售数据"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=120,
    )
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "确认开始" not in (proc.stderr or ""), "非交互不该出现确认提示"
    assert "未检测到 PM_API_KEY" in (proc.stderr or "")
