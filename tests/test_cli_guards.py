"""CLI 入参护栏回归（subprocess 打真实入口，不做函数级 mock）。

覆盖：长度护栏（4-8000）、用例数/轮次区间、断言模式白名单、--fast 快速档、
损坏/空用例文件。全部在解析阶段拒绝，不会烧到任何一次 LLM 调用。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUN = [sys.executable, str(ROOT / "run.py")]


def _run(args: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(
        RUN + args, cwd=str(ROOT), capture_output=True, text=True, timeout=timeout, encoding="utf-8"
    )


def test_task_too_short_rejected():
    r = _run(["--task", "短"])
    assert r.returncode != 0
    assert "4-8000" in (r.stderr + r.stdout)


def test_task_too_long_rejected():
    r = _run(["--task", "任" * 9000])
    assert r.returncode != 0
    assert "4-8000" in (r.stderr + r.stdout)


def test_cases_out_of_range_rejected():
    assert _run(["--task", "让 AI 分析销售数据", "--cases", "0"]).returncode != 0
    assert _run(["--task", "让 AI 分析销售数据", "--cases", "99"]).returncode != 0


def test_max_iter_out_of_range_rejected():
    assert _run(["--task", "让 AI 分析销售数据", "--max-iter", "99"]).returncode != 0


def test_unknown_assert_mode_rejected():
    r = _run(["--task", "让 AI 分析销售数据", "--assert-mode", "bogus"])
    assert r.returncode != 0
    assert "未知断言模式" in (r.stderr + r.stdout)


def test_broken_cases_file_rejected(tmp_path: Path):
    f = tmp_path / "broken.json"
    f.write_text("{ not json", encoding="utf-8")
    r = _run(["--task", "让 AI 分析销售数据", "--cases-file", str(f)])
    assert r.returncode != 0
    assert "cases-file" in (r.stderr + r.stdout)


def test_empty_cases_file_rejected(tmp_path: Path):
    f = tmp_path / "empty.json"
    f.write_text("[]", encoding="utf-8")
    r = _run(["--task", "让 AI 分析销售数据", "--cases-file", str(f)])
    assert r.returncode != 0
    assert "非空 JSON 数组" in (r.stderr + r.stdout)


def test_missing_task_rejected():
    assert _run([]).returncode != 0


def _probe(extra: list[str], tmp_path: Path, tag: str, env: dict | None = None) -> str:
    """在解析阶段之后、任何 LLM 调用之前（--mermaid）退出，并打印生效的环境变量。"""
    probe = tmp_path / f"probe_{tag}.py"
    probe.write_text(
        "import os,sys,runpy\n"
        f"sys.argv=['run.py','--task','让 AI 分析销售数据','--mermaid']+{extra!r}\n"
        "try:\n"
        "    runpy.run_path('run.py', run_name='__main__')\n"
        "except SystemExit as e:\n"
        "    print('EXIT', e.code or 0)\n"
        "print('ENV', os.environ.get('PM_BASELINE'), os.environ.get('PM_PAIRWISE'),"
        " os.environ.get('PM_SAMPLES_PER_CASE'))\n",
        encoding="utf-8",
    )
    r = subprocess.run(
        [sys.executable, str(probe)],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=60,
        encoding="utf-8",
        env={**os.environ, **(env or {})},
    )
    assert "EXIT 0" in r.stdout, r.stdout + r.stderr
    return r.stdout


def test_fast_forces_cheap_config(tmp_path: Path):
    """--fast 必须保证：不跑基线/盲评、单采样（无论 .env 怎么写）。"""
    out = _probe(["--fast"], tmp_path, "fast")
    assert "ENV 0 0 1" in out, f"--fast 未生效：{out}"


def test_fast_yields_to_explicit_samples(tmp_path: Path):
    """显式参数优先：--fast --samples 2 时采样仍为 2。"""
    out = _probe(["--fast", "--samples", "2"], tmp_path, "fast2")
    assert "ENV 0 0 2" in out, f"显式 --samples 应覆盖 --fast：{out}"


def test_default_keeps_baseline_on(tmp_path: Path):
    """不带 --fast 时不许偷偷关基线：测量层的开关只能由 .env 或显式参数决定。"""
    out = _probe([], tmp_path, "plain", env={"PM_BASELINE": "1", "PM_PAIRWISE": "1"})
    assert "ENV 1 1" in out, f"默认档不应改基线开关：{out}"


def test_fast_overrides_enabled_baseline(tmp_path: Path):
    """即使 .env/环境把基线开着，--fast 也必须把它关掉（快速档的语义保证）。"""
    out = _probe(["--fast"], tmp_path, "fast_on", env={"PM_BASELINE": "1", "PM_PAIRWISE": "1"})
    assert "ENV 0 0 1" in out, f"--fast 应覆盖已开启的基线：{out}"


def test_cases_file_with_expected_is_accepted(tmp_path: Path):
    """合法用例集不应被护栏误杀（护栏只在解析阶段，不进 LLM）。"""
    f = tmp_path / "ok.json"
    f.write_text(
        json.dumps(
            [{"input": "华东,120", "expected": "华东", "mode": "contains"}], ensure_ascii=False
        ),
        encoding="utf-8",
    )
    # 用 --mermaid 在解析后立刻退出，验证用例集解析通过
    probe = tmp_path / "probe2.py"
    probe.write_text(
        "import sys,runpy\n"
        f"sys.argv=['run.py','--task','让 AI 抽取城市与金额','--cases-file',{str(f)!r},'--mermaid']\n"
        "try:\n"
        "    runpy.run_path('run.py', run_name='__main__')\n"
        "except SystemExit as e:\n"
        "    print('EXIT', e.code or 0)\n",
        encoding="utf-8",
    )
    r = subprocess.run(
        [sys.executable, str(probe)],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=60,
        encoding="utf-8",
    )
    assert "EXIT 0" in r.stdout, r.stdout + r.stderr
