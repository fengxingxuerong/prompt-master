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
        RUN + args,
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=timeout,
        # errors="replace"：子进程偶尔按宿主码页吐字节（本机 cp936），默认严格解码会让
        # CompletedProcess.stdout 变成 **None**（不是空串），于是断言里 `r.stdout + r.stderr`
        # 自己先 TypeError，把真实失败原因整个遮住。宁可看到替换符，也不要 None。
        encoding="utf-8",
        errors="replace",
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


# ---------------------------------------------------------------------------
# 无 Key 入门路径（run.py 文档 §1b 承诺的那条）
#
# 这组用例必须打 subprocess：闸门在 main() 里，而仓库既有的假后端测试全部直调
# run_pipeline，绕过了 main() —— 于是"文档写着无 Key 能跑、实测 exit 2"长期无人发现。
# PM_LOG_DIR 指向 tmp_path：演示模式照常落产物，但不往真实 logs/ 里堆 run_*.json。
# ---------------------------------------------------------------------------
_NO_KEY = {"PM_API_KEY": "", "PM_TARGET_API_KEY": ""}


def _run_nokey(
    args: list[str], tmp_path: Path, extra_env: dict | None = None
) -> subprocess.CompletedProcess:
    return subprocess.run(
        RUN + args,
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=180,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, **_NO_KEY, "PM_LOG_DIR": str(tmp_path), **(extra_env or {})},
    )


def test_no_key_without_fake_backend_is_config_error(tmp_path: Path):
    """没 Key 又没指定假后端：必须停在配置错误（2），且提示里要有可执行的下一步。"""
    r = _run_nokey(["--task", "让 AI 分析销售数据"], tmp_path)
    assert r.returncode == 2
    assert "未检测到 PM_API_KEY" in (r.stderr + r.stdout)
    assert "PM_FAKE_BACKEND" in r.stderr, "闸门提示里必须给出无 Key 的替代路径"


def test_no_key_with_illegal_scenario_still_blocked(tmp_path: Path):
    """场景名非法时不许放行：闸门不能被"PM_FAKE_BACKEND 非空"糊过去（否则后面真调端点）。"""
    r = _run_nokey(["--task", "让 AI 分析销售数据"], tmp_path, {"PM_FAKE_BACKEND": "bogus"})
    assert r.returncode == 2
    assert "未检测到 PM_API_KEY" in (r.stderr + r.stdout)


def test_no_key_fake_backend_demo_runs(tmp_path: Path):
    """文档承诺的无 Key 演示：跑完整流程、退出码是结果码而非 2、stdout 仍是恰好一个 JSON。"""
    r = _run_nokey(
        ["--task", "让 AI 分析销售数据", "--fast", "--json"],
        tmp_path,
        {"PM_FAKE_BACKEND": "progress"},
    )
    assert r.returncode != 2, f"演示模式仍被 Key 闸门拦下：{r.stderr}"
    assert "未检测到 PM_API_KEY" not in r.stderr
    payload = json.loads(r.stdout)  # stdout 混入任何人类可读行都会在这里炸
    assert payload["status"] in {"passed", "max_iterations", "early_stopped"}
    assert payload["llm_calls"] > 0
    assert "演示模式" in r.stderr and "演示模式" not in r.stdout


def test_help_lists_every_subcommand():
    """`--help` 必须列出全部子命令。

    子命令在 argparse 之前用裸字符串分发，不写进 help 就一个都看不见，
    而 docs/agent-cli-guide.md 自称"标准学习入口就是 --help"（2026-09-25 实测计数 0）。
    """
    out = _run(["--help"]).stdout
    for cmd in ("submit", "status", "report", "wait", "history", "calibrate", "library"):
        assert cmd in out, f"--help 漏了子命令 {cmd}"
    assert "PM_FAKE_BACKEND" in out and "--selftest" in out, "无 Key 的两条路径也要写进 --help"


def test_each_subcommand_has_its_own_help():
    """`run.py <子命令> --help` 必须可用：--help 只给一行摘要，细节要能在原地查到。"""
    for cmd in ("submit", "status", "report", "wait", "history", "calibrate", "library"):
        r = _run([cmd, "--help"])
        assert r.returncode == 0, f"{cmd} --help 失败：{r.stdout}{r.stderr}"
        assert f"run.py {cmd}" in r.stdout, f"{cmd} --help 的 prog 名不对"


def test_unknown_subcommand_points_at_the_list():
    r = _run(["subbmit", "--task", "让 AI 分析销售数据"])
    assert r.returncode == 2
    out = (r.stdout or "") + (r.stderr or "")
    assert "未知子命令" in out and "submit" in out, "打错子命令时要当场把清单列出来"


def test_fake_backend_writes_nothing_outside_log_dir(tmp_path: Path):
    """演示模式的产物只落在 PM_LOG_DIR 里（缓存被 CallHook.disable_cache 关掉）。"""
    _run_nokey(
        ["--task", "让 AI 分析销售数据", "--fast"], tmp_path, {"PM_FAKE_BACKEND": "progress"}
    )
    produced = sorted(p.name for p in tmp_path.glob("*"))
    assert any(n.startswith("run_") and n.endswith(".json") for n in produced), produced
    assert not any("cache" in n for n in produced), f"演示模式污染了缓存文件：{produced}"


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
        errors="replace",
        env={**os.environ, **(env or {})},
    )
    assert "EXIT 0" in (r.stdout or ""), (r.stdout or "") + (r.stderr or "")
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
        errors="replace",
    )
    assert "EXIT 0" in (r.stdout or ""), (r.stdout or "") + (r.stderr or "")
