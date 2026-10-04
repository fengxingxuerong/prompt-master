"""pm/cli/ 子包函数级测试（进程内直调）。

test_cli_guards.py 用 subprocess 打真实入口，慢且 coverage 统计不到子进程；
这里直接调 pm.cli.* 的函数，补齐解析、分发、聚合与产物落盘的核心分支。
护栏的"验收语义"仍由 guards 文件负责，本文件不重复其断言。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

# __init__ 把 main 函数导出为 pm.cli.main 属性，遮蔽同名子模块——显式从 sys.modules 取模块对象
import pm.cli.main  # noqa: F401
import pytest
from pm.cli import history as cli_history
from pm.cli import library as cli_library
from pm.cli import support as cli_support

cli_main = sys.modules["pm.cli.main"]


# ---------------------------------------------------------------------------
# 基建
# ---------------------------------------------------------------------------
def _patch_log_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """产物目录换到 tmp：只需要设一个环境变量。

    以前这里要逐模块 patch `LOG_DIR` 引用（support/history/library）外加 calibrate 的
    `_CALIB_HISTORY` 常量 —— 四个绑定点，漏一个就写进真实 `logs/`。改成晚绑定的
    `support.log_dir()` 之后，"设 PM_LOG_DIR"就是唯一口径：代码能生效，测试才能只设一个变量。
    """
    monkeypatch.setenv("PM_LOG_DIR", str(tmp_path))


def _write_run(logs: Path, run_id: str, mtime: float | None = None, **fields: Any) -> Path:
    d: dict[str, Any] = {
        "run_id": run_id,
        "task": fields.pop("task", "让 AI 分析销售数据"),
        **fields,
    }
    p = logs / f"run_{run_id}.json"
    p.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    if mtime is not None:
        os.utime(p, (mtime, mtime))
    return p


def _run_main(monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> int:
    monkeypatch.setattr(sys, "argv", ["run.py", *argv])
    return cli_main.main()


def _main_error(monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> None:
    """argparse 护栏路径：p.error 以 SystemExit(2) 退出。"""
    monkeypatch.setattr(sys, "argv", ["run.py", *argv])
    with pytest.raises(SystemExit) as ei:
        cli_main.main()
    assert ei.value.code == 2


# ---------------------------------------------------------------------------
# support.py
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("raw", "ok", "want"),
    [
        ("exact", True, "exact"),
        (" contains ", True, "contains"),
        ("rule", True, "rule"),
        ("custom:my_assert", True, "custom:my_assert"),
        ("custom:_x", False, ""),
        ("custom:9x", False, ""),
        ("bogus", False, ""),
        ("", False, ""),
    ],
)
def test_assert_mode_arg(raw: str, ok: bool, want: str) -> None:
    if ok:
        assert cli_support.assert_mode_arg(raw) == want
    else:
        with pytest.raises(argparse.ArgumentTypeError):
            cli_support.assert_mode_arg(raw)


def test_resolve_case_mode_defaults_and_own_mode() -> None:
    p = argparse.ArgumentParser()
    assert cli_support._resolve_case_mode({}, "rule", 0, p) == "rule"
    assert cli_support._resolve_case_mode({"mode": "regex"}, "rule", 0, p) == "regex"
    assert cli_support._resolve_case_mode({"assert_mode": "exact"}, "rule", 0, p) == "exact"


def test_resolve_case_mode_invalid_exits() -> None:
    p = argparse.ArgumentParser()
    with pytest.raises(SystemExit):
        cli_support._resolve_case_mode({"mode": "bogus"}, "contains", 2, p)


@pytest.mark.parametrize(
    ("iters", "want"),
    [(0, 52), (3, 88), (30, 412), (-1, 52)],  # max(40, 4*(3*(iters+1)+10))
)
def test_recursion_budget(iters: int, want: int) -> None:
    assert cli_support.recursion_budget(iters) == want


def test_save_artifacts_writes_log_and_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_log_dir(monkeypatch, tmp_path)
    state = {"run_id": "r1", "final_report": "# 报告", "status": "passed"}
    log_path, report_path = cli_support.save_artifacts(state, None)
    assert '"run_id": "r1"' in log_path.read_text(encoding="utf-8")
    assert report_path == tmp_path / "report_r1.md"
    assert report_path.read_text(encoding="utf-8") == "# 报告"


def test_save_artifacts_custom_out(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_log_dir(monkeypatch, tmp_path)
    out = tmp_path / "custom" / "out.md"
    _, report_path = cli_support.save_artifacts({"run_id": "r2", "final_report": "R"}, str(out))
    assert report_path == out
    assert out.read_text(encoding="utf-8") == "R"


def test_skill_md_written_only_when_passed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from pm.skillmd import SkillGateError

    _patch_log_dir(monkeypatch, tmp_path)
    report = tmp_path / "rep.md"
    report.write_text("报告", encoding="utf-8")
    monkeypatch.setattr("pm.skillmd.render_skill_md", lambda state: "--- SKILL ---")

    # 未达标不写
    assert cli_support._maybe_write_skill_md({"status": "max_iterations"}, report) is None
    assert not (tmp_path / "rep.SKILL.md").exists()

    # 达标写出
    got = cli_support._maybe_write_skill_md({"status": "passed"}, report)
    assert got == tmp_path / "rep.SKILL.md"
    assert got is not None and "SKILL" in got.read_text(encoding="utf-8")

    # 门禁拦截：落 .blocked 说明文件而不是半成品
    def _gate(state: dict[str, Any]) -> str:
        raise SkillGateError("注入")

    monkeypatch.setattr("pm.skillmd.render_skill_md", _gate)
    blocked = cli_support._maybe_write_skill_md({"status": "passed"}, report)
    assert blocked == tmp_path / "rep.SKILL.md.blocked"

    # 普通渲染失败：只告警不中断，返回 None
    def _boom(state: dict[str, Any]) -> str:
        raise ValueError("x")

    monkeypatch.setattr("pm.skillmd.render_skill_md", _boom)
    assert cli_support._maybe_write_skill_md({"status": "passed"}, report) is None


def test_emit_json_result_fields(capsys: pytest.CaptureFixture[str]) -> None:
    final = {
        "run_id": "r9",
        "status": "passed",
        "aggregate": {"avg_score": 8.5},
        "iteration": 2,
        "max_iterations": 3,
        "llm_calls": 17,
        "prompt": "最佳提示词",
        "prompt_versions": [{"v": 1}, {"v": 2}],
        "unresolved_questions": [],
    }
    cli_support.emit_json_result(final, Path("L"), Path("R"))
    payload = json.loads(capsys.readouterr().out)
    assert payload["run_id"] == "r9"
    assert payload["status"] == "passed"
    assert payload["best_prompt"] == "最佳提示词"
    assert payload["n_versions"] == 2
    assert payload["report_path"] == "R"
    # 聚合缺失时不编造结构
    cli_support.emit_json_result({"run_id": "r10"}, Path("L"), Path("R"))
    p2 = json.loads(capsys.readouterr().out)
    assert p2["aggregate"] is None and p2["status"] is None and p2["n_versions"] == 0


@pytest.mark.parametrize(
    ("status", "want"),
    [("passed", 0), ("max_iterations", 1), ("early_stopped", 1), ("failed", 3), (None, 3)],
)
def test_result_exit_code(status: str | None, want: int) -> None:
    assert cli_support._result_exit_code(status) == want


def test_setup_logging_verbose_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    """只断言传参（pytest 已给 root 挂 handler，basicConfig 会 no-op，不能看全局状态）。"""
    calls: dict[str, Any] = {}

    def fake_basic_config(**kw: Any) -> None:
        calls.update(kw)

    monkeypatch.setattr(cli_support.logging, "basicConfig", fake_basic_config)
    cli_support.setup_logging(False)
    assert calls["level"] == logging.INFO
    cli_support.setup_logging(True)
    assert calls["level"] == logging.DEBUG


# ---------------------------------------------------------------------------
# main.py：分发与解析（护栏语义由 test_cli_guards 负责）
# ---------------------------------------------------------------------------
def test_main_mermaid(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    assert _run_main(monkeypatch, ["--mermaid"]) == 0
    assert "graph" in capsys.readouterr().out.lower()


def test_main_selftest_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[int] = []
    # main.py 的 selftest 延迟到分支内 import，patch 真实来源模块
    monkeypatch.setattr("pm.cli.selftest.selftest", lambda: called.append(1) or 0)
    assert _run_main(monkeypatch, ["--selftest"]) == 0
    assert called == [1]


def test_main_preflight_dispatch(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("pm.preflight.run_preflight", lambda: SimpleNamespace(all_ok=True))
    monkeypatch.setattr("pm.preflight.render_preflight", lambda report: "OK")
    assert _run_main(monkeypatch, ["--preflight"]) == 0
    assert "OK" in capsys.readouterr().out
    # 预检有失败项 → 退出码 1（花大钱前发现问题）
    monkeypatch.setattr("pm.preflight.run_preflight", lambda: SimpleNamespace(all_ok=False))
    assert _run_main(monkeypatch, ["--preflight"]) == 1


def test_main_json_interactive_mutually_exclusive(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _main_error(monkeypatch, ["--task", "让 AI 分析销售数据", "--json", "--interactive"])
    assert "互斥" in capsys.readouterr().err


def test_main_missing_task_exits(monkeypatch: pytest.MonkeyPatch) -> None:
    _main_error(monkeypatch, [])


def test_main_empty_task_file_exits(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    f = tmp_path / "empty.txt"
    f.write_text("   ", encoding="utf-8")
    _main_error(monkeypatch, ["--task-file", str(f)])


def test_main_task_file_ok(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """--task-file 读取后走 dry-run 出口，验证读取链路（不联网）。"""
    f = tmp_path / "task.txt"
    f.write_text("让 AI 分析销售数据并输出结论", encoding="utf-8")
    assert _run_main(monkeypatch, ["--task-file", str(f), "--dry-run"]) == 0


def test_main_no_api_key_returns_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("PM_API_KEY", raising=False)
    monkeypatch.delenv("PM_TARGET_API_KEY", raising=False)
    assert _run_main(monkeypatch, ["--task", "让 AI 分析销售数据"]) == 2
    assert "PM_API_KEY" in capsys.readouterr().err


def test_main_dry_run_shape(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run_main(monkeypatch, ["--task", "让 AI 分析销售数据", "--dry-run"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "dry_run"
    est = payload["estimated_llm_calls"]
    assert est["max"] >= est["min"] > 0
    # conftest 固定的旧口径：单采样、关基线、关盲评
    assert payload["samples"] == 1
    assert payload["baseline_enabled"] is False
    assert payload["pairwise_enabled"] is False


def test_main_fast_caps_max_iter(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """--fast 未显式给 --max-iter 时压到 1 轮：dry-run 的 max_iterations 应为 1。"""
    assert _run_main(monkeypatch, ["--fast", "--task", "让 AI 分析销售数据", "--dry-run"]) == 0
    assert json.loads(capsys.readouterr().out)["max_iterations"] == 1


def _cases_file(tmp_path: Path, data: list[dict[str, Any]]) -> str:
    f = tmp_path / "cases.json"
    f.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return str(f)


def test_main_dry_run_with_cases_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cf = _cases_file(
        tmp_path,
        [
            {"input": "华东,120", "expected": "华东"},
            {"input": "华北,80", "expected": "输出必须包含华北并解释口径差异", "mode": "rule"},
            {
                "input": "华南,60",
                "expected": "华南",
                "scenario": "main_path",
                "hijack_marker": "注入短语",
            },
        ],
    )
    assert (
        _run_main(monkeypatch, ["--task", "抽取城市与金额", "--cases-file", cf, "--dry-run"]) == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["cases"] == 3  # 用例数以用例集为准


def test_main_cases_file_over_8_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cf = _cases_file(tmp_path, [{"input": f"输入{i}"} for i in range(9)])
    _main_error(monkeypatch, ["--task", "让 AI 分析销售数据", "--cases-file", cf])


def test_main_cases_file_missing_input_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cf = _cases_file(tmp_path, [{"expected": "没有输入"}])
    _main_error(monkeypatch, ["--task", "让 AI 分析销售数据", "--cases-file", cf])


def test_main_cases_file_rule_like_contains_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """contains 模式配上规则式 expected：跑 20 次计费调用前就该拦住。"""
    cf = _cases_file(
        tmp_path,
        [{"input": "华东,120", "expected": "输出必须包含城市字段，并且要解释口径"}],
    )
    _main_error(monkeypatch, ["--task", "抽取城市与金额", "--cases-file", cf])
    assert "mode" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# main() 拆成函数之后露出来的那些分支（2026-10-04 补）
#
# 它们原来挤在一条 346 行的直线里，覆盖率把缺口抹成了一个数字；拆细之后每一段
# 都必须自己证明自己被执行过。这里补的全是"错了会骗到调用方"的那几类：
# 开关没落进 env、外推编出金额、机器可读出口多打了一行。
# ---------------------------------------------------------------------------


def test_no_baseline_and_no_pairwise_reach_the_pipeline_via_env(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """CLI 的 --no-baseline/--no-pairwise 只能靠环境变量传给流水线（API 没这两个参数）。

    写没写进去在 CLI 里看不出来，只有 dry-run 的 enabled 两栏会露 —— 所以 env 与 payload
    要一起断言，两边不一致就等于"参数被静默吃掉"。
    """
    monkeypatch.delenv("PM_BASELINE", raising=False)
    monkeypatch.delenv("PM_PAIRWISE", raising=False)
    rc = _run_main(
        monkeypatch,
        ["--task", "让 AI 分析销售数据", "--no-baseline", "--no-pairwise", "--dry-run"],
    )
    assert rc == 0
    assert os.environ["PM_BASELINE"] == "0"
    assert os.environ["PM_PAIRWISE"] == "0"
    payload = json.loads(capsys.readouterr().out)
    assert payload["baseline_enabled"] is False
    assert payload["pairwise_enabled"] is False


def test_cases_file_bare_strings_become_full_cases(
    tmp_path: Path,
) -> None:
    """用例集允许写裸字符串；补齐 expected/mode 之后才是流水线认的形状。"""
    p = cli_main._build_parser("run.py")
    args = p.parse_args(["--task", "抽取城市", "--cases-file", _cases_file(tmp_path, ["甲", "乙"])])
    cases = cli_main._load_seed_cases(p, args)
    assert cases == [
        {"input": "甲", "expected": "", "mode": "contains"},
        {"input": "乙", "expected": "", "mode": "contains"},
    ]
    assert args.cases == 2, "用例数以用例集为准（达标口径按它做完整性校验）"


def test_cases_file_that_cannot_be_parsed_exits_2_naming_the_reason(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = tmp_path / "cases.json"
    bad.write_text("{ not json", encoding="utf-8")
    _main_error(monkeypatch, ["--task", "让 AI 分析销售数据", "--cases-file", str(bad)])
    assert "读取失败" in capsys.readouterr().err


def test_last_run_usage_tolerates_history_pointing_at_a_missing_artifact(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """台账说有过这轮、产物文件却不在了（日志被清/换机器）：读不到就返回 None。

    这条不能抛 —— `--dry-run` 是"要不要花这笔钱"的决策入口，它自己崩掉等于把增强信息
    的读取失败升级成整个入口不可用。
    """
    _patch_log_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(
        cli_history, "_load_run_history", lambda n, include_demo=False: [{"run_id": "ghost"}]
    )
    assert cli_main._last_run_usage() == (None, "ghost")


@pytest.mark.parametrize(
    "usage,run_id",
    [
        (None, ""),  # 没有可比历史
        ({"target": {"calls": 3, "input_tokens": 100, "output_tokens": 20}}, "r1"),  # 没配单价
    ],
)
def test_dry_run_refuses_to_invent_a_cost(
    monkeypatch: pytest.MonkeyPatch, usage: dict[str, Any] | None, run_id: str
) -> None:
    """没有价目表也没有历史时，整段金额必须不出现（"没有数"比"看起来很合理的假数"安全）。"""
    for k in ("PM_PRICE_INPUT_PER_M", "PM_PRICE_OUTPUT_PER_M"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(cli_main, "_last_run_usage", lambda: (usage, run_id))
    payload: dict[str, Any] = {"mode": "dry_run"}
    cli_main._attach_estimated_cost(payload, 10, 20)
    assert "estimated_cost" not in payload


def test_dry_run_cost_extrapolation_needs_a_nonzero_call_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """单价有了、历史调用数却是 0：均价无从算起，此时也不能给金额（除零那一侧的守卫）。"""
    monkeypatch.setenv("PM_PRICE_INPUT_PER_M", "1")
    monkeypatch.setenv("PM_PRICE_OUTPUT_PER_M", "2")
    monkeypatch.setattr(
        cli_main, "_last_run_usage", lambda: ({"target": {"calls": 0, "input_tokens": 1000}}, "r1")
    )
    payload: dict[str, Any] = {"mode": "dry_run"}
    cli_main._attach_estimated_cost(payload, 10, 20)
    assert "estimated_cost" not in payload


def test_dry_run_cost_extrapolation_names_its_basis(monkeypatch: pytest.MonkeyPatch) -> None:
    """正对照：给足了单价与历史，金额必须出现，而且自曝是按哪一轮外推的。"""
    monkeypatch.setenv("PM_PRICE_INPUT_PER_M", "1")
    monkeypatch.setenv("PM_PRICE_OUTPUT_PER_M", "2")
    monkeypatch.setattr(
        cli_main,
        "_last_run_usage",
        lambda: ({"target": {"calls": 4, "input_tokens": 1_000_000, "output_tokens": 0}}, "r7"),
    )
    payload: dict[str, Any] = {"mode": "dry_run"}
    cli_main._attach_estimated_cost(payload, 10, 20)
    assert payload["estimated_cost"]["basis"] == "按最近一次运行（r7）的实测逐角色均价外推"


def test_print_run_outcome_json_branch_emits_exactly_one_json_object(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """入口的机器可读出口：stdout 必须恰好一个 JSON 对象（Agent 直接 loads 它）。

    `emit_json_result` 自己在 test_emit_json_result_fields 里已有断言；这条钉的是
    **main 那条线真的走到了它**，否则"函数是对的、入口没接上"这一类会全绿溜过去。
    """
    cli_main._print_run_outcome(
        SimpleNamespace(json=True), {"run_id": "r1", "status": "passed"}, Path("L"), Path("R")
    )
    out = capsys.readouterr().out
    assert json.loads(out)["run_id"] == "r1"


def test_print_run_outcome_human_branch_shows_report_and_paths(
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli_main._print_run_outcome(
        SimpleNamespace(json=False), {"final_report": "REPORT-BODY"}, Path("L"), Path("R")
    )
    out = capsys.readouterr().out
    assert "REPORT-BODY" in out
    assert "完整运行日志：L" in out and "报告已保存：R" in out


def test_main_fast_sets_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # --fast 与 --mermaid 组合：fast 落完 env 后由 mermaid 出口返回
    monkeypatch.setenv("PM_BASELINE", "1")
    monkeypatch.setenv("PM_PAIRWISE", "1")
    monkeypatch.delenv("PM_SAMPLES_PER_CASE", raising=False)
    assert _run_main(monkeypatch, ["--fast", "--mermaid"]) == 0
    assert os.environ["PM_SAMPLES_PER_CASE"] == "1"
    assert os.environ["PM_BASELINE"] == "0"
    assert os.environ["PM_PAIRWISE"] == "0"


def test_main_explicit_samples_wins_over_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PM_SAMPLES_PER_CASE", raising=False)
    assert _run_main(monkeypatch, ["--fast", "--samples", "2", "--mermaid"]) == 0
    assert os.environ["PM_SAMPLES_PER_CASE"] == "2"


# ---------------------------------------------------------------------------
# history.py
# ---------------------------------------------------------------------------
def test_load_run_history_filters_and_delta(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_log_dir(monkeypatch, tmp_path)
    _write_run(
        tmp_path,
        "ok1",
        mtime=1000.0,
        status="passed",
        aggregate={"avg_score": 8.0, "ci_lower": 7.0, "noise": 0.1},
        baseline_aggregate={"avg_score": 6.0},
        llm_calls=12,
    )
    _write_run(
        tmp_path,
        "demo1",
        mtime=2000.0,
        status="passed",
        aggregate={"avg_score": 9.0},
        trace=[{"node": "test", "channel": "fake"}],
    )
    # 缺聚合字段的 run：delta 如实为 None 而不是抛异常
    _write_run(tmp_path, "noagg", mtime=3000.0, status="failed")
    (tmp_path / "run_broken.json").write_text("{ not json", encoding="utf-8")

    rows = cli_history._load_run_history(10, include_demo=False)
    assert [r["run_id"] for r in rows] == ["noagg", "ok1"]  # 按新到旧（mtime 3000 > 1000）
    assert rows[0]["delta"] is None
    assert rows[1]["delta"] == 2.0
    assert rows[1]["demo"] is False and rows[1]["llm_calls"] == 12

    rows_all = cli_history._load_run_history(10, include_demo=True)
    assert [r["run_id"] for r in rows_all] == ["noagg", "demo1", "ok1"]


def test_load_run_history_last_window(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_log_dir(monkeypatch, tmp_path)
    for i, rid in enumerate(["old1", "mid2", "new3"]):
        _write_run(tmp_path, rid, mtime=1000.0 + i * 100, status="passed")
    rows = cli_history._load_run_history(2, include_demo=False)
    assert [r["run_id"] for r in rows] == ["new3", "mid2"]


def test_history_command_json_significance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_log_dir(monkeypatch, tmp_path)
    # 同一任务 3 次 run，delta 1/2/3：mean=2, se=sqrt(1/3)≈0.577, CI≈[0.87, 3.13] → 显著
    for i, d in enumerate((1.0, 2.0, 3.0)):
        _write_run(
            tmp_path,
            f"sig{i}",
            task="同一需求",
            status="passed",
            aggregate={"avg_score": 7.0 + d},
            baseline_aggregate={"avg_score": 7.0},
        )
    assert cli_history._history_command(["--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["n_runs"] == 3
    groups = payload["task_groups"]
    assert len(groups) == 1 and groups[0]["n_runs"] == 3
    assert groups[0]["significant"] is True
    assert groups[0]["delta_ci95"][0] > 0


def test_history_command_insufficient_sample_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_log_dir(monkeypatch, tmp_path)
    # delta [1, -1]：CI 恰好跨 0 → 不显著；n<5 附观察提示
    for i, score in enumerate((8.0, 6.0)):
        _write_run(
            tmp_path,
            f"few{i}",
            task="小样本任务",
            status="passed",
            aggregate={"avg_score": score},
            baseline_aggregate={"avg_score": 7.0},
        )
    assert cli_history._history_command(["--json"]) == 0
    g = json.loads(capsys.readouterr().out)["task_groups"][0]
    assert g["significant"] is False and g["note"] == "样本少，结论仅供观察"


def test_history_command_table_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_log_dir(monkeypatch, tmp_path)
    _write_run(tmp_path, "t1", status="passed", aggregate={"avg_score": 8.0})
    assert cli_history._history_command([]) == 0
    out = capsys.readouterr().out
    assert "最近 1 条运行" in out and "暂无同任务" in out


# ---------------------------------------------------------------------------
# library.py
# ---------------------------------------------------------------------------
def test_text_bigrams_and_dice() -> None:
    assert cli_library._text_bigrams("") == set()
    assert cli_library._text_bigrams("销") == {"销"}
    assert cli_library._text_bigrams("销售数据") == {"销售", "售数", "数据"}
    empty: set[str] = set()
    assert cli_library._dice(empty, {"a"}) == 0.0
    assert cli_library._dice({"销售", "数据"}, {"销售", "数据"}) == 1.0
    assert 0.0 < cli_library._dice({"销售"}, {"销售", "数据"}) < 1.0


def test_task_sim_grams_strips_template_noise() -> None:
    """模板前缀剥离后应与裸任务高度重合（不剥则被"帮我写一个 prompt"淹没）。"""
    plain = cli_library._task_sim_grams("分析销售数据")
    raw = cli_library._task_sim_grams("帮我写一个 prompt 让 AI 分析销售数据")
    assert cli_library._dice(raw, plain) >= 0.8
    # 对照：噪声不剥时（模板 bigram 稀释分母）相似度显著走低
    unstripped = cli_library._text_bigrams("帮我写一个prompt让ai分析销售数据")
    assert cli_library._dice(unstripped, plain) < 0.6


def _asset_run(logs: Path, run_id: str, task: str, score: float, status: str = "passed") -> None:
    _write_run(
        logs,
        run_id,
        task=task,
        status=status,
        prompt=f"# {task} 的最佳提示词正文",
        aggregate={"avg_score": score},
        trace=[{"node": "test", "channel": "real"}] if status == "passed" else [],
    )


def test_library_recommend_ranks_and_dedups(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_log_dir(monkeypatch, tmp_path)
    _asset_run(tmp_path, "lo", "分析销售数据并给出改进建议", 7.0)
    # 同一任务两轮：相似度必然相同，去重后必须保留分数最高的 hi
    _asset_run(tmp_path, "hi", "帮我写一个 prompt 让 AI 分析销售数据", 9.0)
    _asset_run(tmp_path, "hi2", "帮我写一个 prompt 让 AI 分析销售数据", 8.5)
    # demo run（fake 通道）必须排除
    _write_run(
        tmp_path,
        "demo",
        task="分析销售数据",
        status="passed",
        prompt="演示提示词",
        aggregate={"avg_score": 9.9},
        trace=[{"node": "test", "channel": "fake"}],
    )

    ns = argparse.Namespace(task_text="分析销售数据的需求描述", json=True)
    assert cli_library._library_recommend(ns) == 0
    payload = json.loads(capsys.readouterr().out)
    recs = payload["recommendations"]
    assert [r["run_id"] for r in recs] == ["hi", "lo"], (
        f"同任务保留最高分+排除 demo+按相似度排序：{recs}"
    )
    assert recs[0]["similarity"] > 0.05 and recs[0]["avg_score"] == 9.0


def test_library_recommend_no_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_log_dir(monkeypatch, tmp_path)
    _asset_run(tmp_path, "far", "翻译英文学术论文摘要", 9.0)
    ns = argparse.Namespace(task_text="量子物理实验数据处理", json=True)
    assert cli_library._library_recommend(ns) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["recommendations"] == [] and "没有相似" in payload["note"]


def test_library_recommend_requires_task_text() -> None:
    with pytest.raises(SystemExit):
        cli_library._library_command(["--recommend"])


def test_library_list_json_and_query(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_log_dir(monkeypatch, tmp_path)
    _asset_run(tmp_path, "a1", "分析销售数据", 8.0)
    _asset_run(tmp_path, "a2", "分析销售数据", 7.5, status="max_iterations")
    _asset_run(tmp_path, "a3", "翻译论文", 6.0, status="max_iterations")
    # 显式锚定 mtime：连续写入落在同一时间粒度内时排序会抖
    os.utime(tmp_path / "run_a1.json", (1000.0, 1000.0))
    os.utime(tmp_path / "run_a2.json", (2000.0, 2000.0))

    # 默认只列 passed
    assert cli_library._library_command(["--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [a["run_id"] for a in payload["assets"]] == ["a1"]

    # --all 包含未达标交付；--query 关键词过滤（a2 后写，mtime 更新，排前）
    assert cli_library._library_command(["--all", "--json", "--query", "销售"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [a["run_id"] for a in payload["assets"]] == ["a2", "a1"]
    assert payload["assets"][1]["delta_vs_baseline"] is None


def test_library_list_empty_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_log_dir(monkeypatch, tmp_path)
    assert cli_library._library_command([]) == 0
    assert "没有匹配" in capsys.readouterr().out


def test_library_export_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_log_dir(monkeypatch, tmp_path)
    _asset_run(tmp_path, "exp1", "分析销售数据", 8.0)
    out = tmp_path / "custom.prompt.md"
    assert cli_library._library_command(["--export", "exp1", "--out", str(out)]) == 0
    text = out.read_text(encoding="utf-8")
    assert "run_id: exp1" in text and "最佳提示词正文" in text
    assert json.loads(capsys.readouterr().out)["exported"] == str(out)


def test_library_export_default_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_log_dir(monkeypatch, tmp_path)
    _asset_run(tmp_path, "exp2", "分析销售数据", 8.0)
    assert cli_library._library_command(["--export", "exp2"]) == 0
    assert (tmp_path / "exports" / "exp2.prompt.md").exists()


def test_library_export_missing_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_log_dir(monkeypatch, tmp_path)
    assert cli_library._library_command(["--export", "ghost"]) == cli_support.EXIT_CONFIG
    assert "找不到运行" in capsys.readouterr().err


def test_library_export_empty_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_log_dir(monkeypatch, tmp_path)
    _write_run(tmp_path, "noprompt", status="passed", prompt="   ")
    assert cli_library._library_command(["--export", "noprompt"]) == cli_support.EXIT_FAILED
    assert "没有可导出" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# 子命令分发（2026-10-02 补：diff / gate 接入后，分发表变长但没人逐个走过）
# ---------------------------------------------------------------------------
def test_dispatch_routes_every_subcommand(monkeypatch: pytest.MonkeyPatch) -> None:
    """分发函数必须把**每一个** `_SUBCOMMANDS` 成员路由到一个真实实现。

    这张表在长（10 个），而"新加一个子命令却忘了接上分发"是本项目真出过的形态
    （`_SUBCOMMANDS` 是 help 与分发的唯一事实源）。这里用替身记录调用，
    不真跑任何子命令 —— 真跑那些会联网/耗时。
    """
    import pm.cli.agent_mode as am
    import pm.cli.calibrate as cal
    import pm.cli.check as chk
    import pm.cli.diffcmd as dcmd
    import pm.cli.gatecmd as gcmd
    import pm.cli.history as hist
    import pm.cli.library as lib

    seen: list[str] = []

    def spy(name: str) -> Any:
        # 真实签名是 (argv) 或 (cmd, argv)，所以用 *args 兜住两种形态
        def _f(*args: Any, **kwargs: Any) -> int:
            seen.append(name)
            return 0

        return _f

    monkeypatch.setattr(am, "agent_subcommand", spy("agent"))
    monkeypatch.setattr(hist, "_history_command", spy("history"))
    monkeypatch.setattr(cal, "_calibrate_command", spy("calibrate"))
    monkeypatch.setattr(chk, "check_command", spy("check"))
    monkeypatch.setattr(dcmd, "diff_command", spy("diff"))
    monkeypatch.setattr(gcmd, "gate_command", spy("gate"))
    monkeypatch.setattr(lib, "_library_command", spy("library"))

    # 分发函数用的是模块级名字，替换后要重新加载才生效
    import importlib

    fresh = importlib.reload(cli_main)
    try:
        assert fresh._dispatch_subcommand("submit", []) == 0
        assert fresh._dispatch_subcommand("status", []) == 0
        assert fresh._dispatch_subcommand("report", []) == 0
        assert fresh._dispatch_subcommand("wait", []) == 0
        for cmd in ("history", "calibrate", "check", "diff", "gate", "library"):
            assert fresh._dispatch_subcommand(cmd, []) == 0
    finally:
        importlib.reload(cli_main)

    assert sorted(set(seen)) == [
        "agent",
        "calibrate",
        "check",
        "diff",
        "gate",
        "history",
        "library",
    ], f"有子命令没走到实现：{seen}"
    # submit/status/report/wait 四个共用 agent 分支
    assert seen.count("agent") == 4, f"agent 分支应被调用 4 次，实得 {seen.count('agent')}"


def test_subcommands_tuple_is_the_single_source_of_help_and_dispatch() -> None:
    """`_SUBCOMMANDS` 必须同时被 help 与分发消费 —— 只改一处就是空头承诺。"""
    help_text = cli_main._subcommand_help("run.py")
    for cmd in cli_main._SUBCOMMANDS:
        assert cmd in help_text, f"{cmd} 在分发表里但 help 里没有"


def test_unknown_subcommand_lists_the_options(monkeypatch, capsys) -> None:
    """打错子命令要把清单当场列出来，而不是丢一句 argparse 的 unrecognized。"""
    monkeypatch.setattr(sys, "argv", ["run.py", "__打错的子命令__"])
    rc = cli_main.main()
    err = capsys.readouterr().err
    assert rc == cli_main.EXIT_CONFIG
    assert "未知子命令" in err
    for cmd in cli_main._SUBCOMMANDS:
        assert cmd in err, f"清单里少了 {cmd}"


# ---------------------------------------------------------------------------
# 入参护栏（越界参数必须在花钱之前拦住）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("argv", [["--cases", "0"], ["--cases", "9"], ["--cases", "-1"]])
def test_cases_out_of_range_exits_2(monkeypatch, capsys, argv) -> None:
    monkeypatch.setattr(sys, "argv", ["run.py", "--task", "让AI分析销售数据", *argv])
    with pytest.raises(SystemExit) as e:
        cli_main.main()
    assert e.value.code == 2
    assert "--cases" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [["--max-iter", "-1"], ["--max-iter", "11"]])
def test_max_iter_out_of_range_exits_2(monkeypatch, capsys, argv) -> None:
    monkeypatch.setattr(sys, "argv", ["run.py", "--task", "让AI分析销售数据", *argv])
    with pytest.raises(SystemExit) as e:
        cli_main.main()
    assert e.value.code == 2
    assert "--max-iter" in capsys.readouterr().err


def test_task_too_short_exits_2(monkeypatch, capsys) -> None:
    """需求短于 4 字 = 大概率粘错了参数，当场拦（别烧一轮调用才发现）。"""
    monkeypatch.setattr(sys, "argv", ["run.py", "--task", "abc"])
    with pytest.raises(SystemExit) as e:
        cli_main.main()
    assert e.value.code == 2
    assert "4-8000" in capsys.readouterr().err


def test_task_too_long_exits_2(monkeypatch, capsys) -> None:
    monkeypatch.setattr(sys, "argv", ["run.py", "--task", "x" * 8001])
    with pytest.raises(SystemExit) as e:
        cli_main.main()
    assert e.value.code == 2
    assert "8000" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# 装包后的入口与包级纠错（对应 pyproject 的 [project.scripts]）
# ---------------------------------------------------------------------------
def test_console_entry_delegates_to_main(monkeypatch: pytest.MonkeyPatch) -> None:
    """`prompt-master` 命令的入口必须真的转到 `pm.cli.main.main`。

    这是装包态的唯一入口：它坏了，`pip install .` 出来的命令就是个哑炮，
    而"哑炮"在测试态看不出来（大家都用 `python run.py`）。
    """
    import pm.cli.console as console

    called: list[str] = []
    monkeypatch.setattr(console, "prepare_console", lambda: called.append("prepare"))
    monkeypatch.setattr(cli_main, "main", lambda: called.append("main") or 0)
    monkeypatch.setitem(sys.modules, "pm.cli.main", cli_main)

    rc = console.main()
    assert rc == 0
    # 顺序是行为的一部分：prepare_console 必须早于 main（它负责 dotenv/utf8/stdout）
    assert called == ["prepare", "main"], f"引导顺序不对：{called}"


def test_bootstrap_is_shared_with_run_py() -> None:
    """两个入口必须共用同一套引导，否则会出现"一个入口能跑、另一个乱码"。"""
    import pm.cli.console as console
    import run as run_module

    assert console.prepare_console is run_module.prepare_console


def test_package_level_getattr_rejects_ambiguous_names() -> None:
    """`from pm.cli import main` 曾因惰性导出变成"看导入顺序定含义"，已移除。

    误用时必须给出**可执行的纠正**（告诉你去哪个子模块取），而不是静默返回一个
    含义随导入顺序变化的对象 —— 那种歧义会让门禁结果不可复现。
    """
    import pm.cli as pkg

    for name, sub in (("main", "main"), ("selftest", "selftest"), ("run_pipeline", "pipeline")):
        with pytest.raises(AttributeError) as e:
            pkg.__getattr__(name)
        msg = str(e.value)
        assert f"from pm.cli.{sub} import {name}" in msg, f"{name} 的纠正提示不具体：{msg}"
        assert "导入顺序" in msg, "没解释原因，读者会以为是 bug"


def test_package_level_getattr_rejects_unknown_attribute() -> None:
    with pytest.raises(AttributeError, match="has no attribute"):
        import pm.cli as pkg

        pkg.__getattr__("__完全不存在__")


def test_package_all_matches_real_exports() -> None:
    """`__all__` 里列的每个名字都必须真的取得到（写错就是文档骗人）。"""
    import pm.cli as pkg

    for name in pkg.__all__:
        assert hasattr(pkg, name), f"__all__ 列了 {name} 但取不到"


# ---------------------------------------------------------------------------
# `--dry-run` 的金额外推（2026-10-02 新增能力，此前无覆盖）
# ---------------------------------------------------------------------------
def test_dry_run_without_prices_omits_cost(monkeypatch, capsys, tmp_path: Path) -> None:
    """没配单价时**不能**出现 `estimated_cost` —— 本系统不内置价目表。

    编一个"看起来合理"的单价比不显示危险得多：它会一路被当成真实读数。
    """
    for k in list(os.environ):
        if k.startswith("PM_PRICE_"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PM_LOG_DIR", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["run.py", "--dry-run", "--task", "让AI分析销售数据"])
    assert cli_main.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "dry_run"
    assert "estimated_cost" not in payload


def test_dry_run_projects_cost_from_last_run(monkeypatch, capsys, tmp_path: Path) -> None:
    """配了单价 + 有历史台账时，按**最近一次运行的实测均价**外推金额区间。

    这里连"没有历史"的分支一起证明：先跑一次没有台账的（应无金额），
    再放一份带 llm_usage 的运行记录（应出金额，且区间随调用数线性放大）。
    """
    monkeypatch.setenv("PM_PRICE_INPUT_PER_M", "1")
    monkeypatch.setenv("PM_PRICE_OUTPUT_PER_M", "2")
    monkeypatch.setenv("PM_LOG_DIR", str(tmp_path))
    argv = ["run.py", "--dry-run", "--task", "让AI分析销售数据"]
    monkeypatch.setattr(sys, "argv", argv)

    # ① 没有任何历史运行 → 拿不到均价，不出金额
    assert cli_main.main() == 0
    first = json.loads(capsys.readouterr().out)
    assert "estimated_cost" not in first, "没有历史台账时不该编出金额"

    # ② 放一份真实形态的运行记录（10 次调用、各角色若干 token）
    run = {
        "run_id": "abcdef123456",
        "task": "让AI分析销售数据",
        "status": "passed",
        "llm_usage": {
            "target": {"calls": 6, "input_tokens": 100_000, "output_tokens": 50_000},
            "evaluator": {"calls": 4, "input_tokens": 80_000, "output_tokens": 20_000},
        },
        "trace": [],  # 无 fake 通道 → 会被 history 当作真实运行
    }
    (tmp_path / "run_abcdef123456.json").write_text(
        json.dumps(run, ensure_ascii=False), encoding="utf-8"
    )

    assert cli_main.main() == 0
    second = json.loads(capsys.readouterr().out)
    cost = second.get("estimated_cost")
    assert cost, "有历史台账 + 有单价时必须给出金额外推"
    assert cost["per_call"] > 0
    # 区间下界 ≤ 上界，且与调用数同比例
    assert cost["estimated_total_min"] <= cost["estimated_total_max"]
    calls = second["estimated_llm_calls"]
    assert cost["basis_calls"] == {"min": calls["min"], "max": calls["max"]}
    assert "最近一次运行" in cost["basis"], "没说明外推依据是哪一次运行"


def test_dry_run_survives_corrupt_history_file(monkeypatch, capsys, tmp_path: Path) -> None:
    """历史记录损坏时**不许**把 `--dry-run` 整体搞崩。

    金额外推是增强信息：它拿不到就该安静地不出，而不是让"提交前的预算决策"
    这个入口直接失败（那会逼人绕过 dry-run 直接提交）。
    """
    monkeypatch.setenv("PM_PRICE_INPUT_PER_M", "1")
    monkeypatch.setenv("PM_PRICE_OUTPUT_PER_M", "1")
    monkeypatch.setenv("PM_LOG_DIR", str(tmp_path))
    (tmp_path / "run_broken1234.json").write_text("{ 这不是 JSON", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["run.py", "--dry-run", "--task", "让AI分析销售数据"])
    assert cli_main.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "dry_run"
    assert payload["estimated_llm_calls"]["min"] > 0


def test_mermaid_returns_empty_string_when_drawing_fails(monkeypatch, caplog) -> None:
    """`mermaid()` 的失败兜底：画不出来就返回空串，**不许**把调用方炸掉。

    `--mermaid` 是文档里的"看拓扑"入口（`docs/agent-cli-guide.md` 与 README 都提）。
    它挂在 langgraph 的 `draw_mermaid()` 上，而那条链路依赖可选的可视化组件；
    拿不到时应该给出空串并留下告警日志，而不是让一个"看一眼图"的动作抛异常。
    """
    import logging

    import pm.graph as graph_mod

    def boom(*args: object, **kwargs: object) -> object:
        raise RuntimeError("draw_mermaid 不可用")

    class _FakeGraph:
        def draw_mermaid(self) -> str:
            return boom()

    class _FakeCompiled:
        def get_graph(self) -> _FakeGraph:
            return _FakeGraph()

    monkeypatch.setattr(graph_mod, "build_graph", lambda checkpointer=None: _FakeCompiled())

    with caplog.at_level(logging.WARNING, logger="pm.graph"):
        out = graph_mod.mermaid()

    assert out == "", "失败时必须返回空串（调用方按「没图」处理，不是崩）"
    assert any("生成 mermaid 失败" in str(r.message) for r in caplog.records), (
        "失败要留下告警日志，否则「图是空的」与「模板里真没内容」分不开"
    )


def test_mermaid_happy_path_returns_diagram(monkeypatch) -> None:
    """正常路径返回图描述（与失败分支配成一对，防止"永远返回空串"也算通过）。"""
    import pm.graph as graph_mod

    class _FakeGraph:
        def draw_mermaid(self) -> str:
            return "graph TD; START-->clarify"

    class _FakeCompiled:
        def get_graph(self) -> _FakeGraph:
            return _FakeGraph()

    monkeypatch.setattr(graph_mod, "build_graph", lambda checkpointer=None: _FakeCompiled())
    assert graph_mod.mermaid() == "graph TD; START-->clarify"


def test_mermaid_real_build_is_stable() -> None:
    """不打桩，真跑一次：证明真实依赖链能产出非空图（桩测不出依赖缺失）。"""
    import pm.graph as graph_mod

    out = graph_mod.mermaid()
    assert out.strip(), "真实构建返回了空图 —— 说明 draw_mermaid 链路在当前环境不可用"
    for node in ("clarify", "optimize", "evaluate", "report"):
        assert node in out, f"图里缺少节点 {node}"


# ---------------------------------------------------------------------------
# library 的文本出口与损坏文件跳过（2026-10-02 补：此前只测了 JSON 出口）
# ---------------------------------------------------------------------------
def test_library_recommend_text_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--recommend` 不带 `--json` 时走人读分支：相似度 / 分数 / run_id / 任务都要在。

    这条分支此前零覆盖，而它正是**人手动跑**时看到的东西
    （`--json` 是给 Agent 的）。只测 JSON 出口 = 只测了一半用户。
    """
    _patch_log_dir(monkeypatch, tmp_path)
    _asset_run(tmp_path, "hi", "帮我写一个 prompt 让 AI 分析销售数据", 9.0)

    ns = argparse.Namespace(task_text="分析销售数据的需求描述", json=False)
    assert cli_library._library_recommend(ns) == 0
    out = capsys.readouterr().out
    assert "新任务：分析销售数据的需求描述" in out
    assert "相似资产 top-" in out
    assert "hi" in out and "9.0" in out
    # 相似度必须真的打出来（否则读者无法判断"为什么推荐这个"）
    assert "相似度" in out


def test_library_recommend_text_without_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """人读分支的"没有相似资产"也必须可读（空列表不能只打印一个空块）。"""
    _patch_log_dir(monkeypatch, tmp_path)
    _asset_run(tmp_path, "far", "翻译英文学术论文摘要", 9.0)
    ns = argparse.Namespace(task_text="量子物理实验数据处理", json=False)
    assert cli_library._library_recommend(ns) == 0
    out = capsys.readouterr().out
    # 无命中时走**提前返回**分支（不是打印一个 top-0 空列表）：
    # 直接给一句可执行的结论，人读到就知道该按全新需求跑。
    assert "没有相似" in out
    assert "全新需求" in out


def test_library_list_text_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`library` 不带 `--json` 的列表输出：状态 / 分数 / Δ基线 / 调用数 / 导出提示。

    Δ 基线为 None 时要打 `-` 而不是崩（这是最容易出错的一处：
    没有基线臂的运行 delta_vs_baseline 就是 None）。
    """
    _patch_log_dir(monkeypatch, tmp_path)
    _asset_run(tmp_path, "withbase", "分析销售数据并给出改进建议", 9.0)
    # 一条没有基线的资产：Δ 列必须走 None 分支
    _write_run(
        tmp_path,
        "nobase",
        task="分析销售数据并给出改进建议",
        status="passed",
        prompt="# 无基线版本",
        aggregate={"avg_score": 8.0},
        trace=[{"node": "test", "channel": "real"}],
    )

    assert cli_library._library_command([]) == 0
    out = capsys.readouterr().out
    assert "提示词资产" in out and "条" in out
    assert "withbase" in out and "nobase" in out
    assert "Δ基线" in out and "次调用" in out
    assert "导出成品" in out, "列表末尾要给导出用法，否则读者不知道下一步怎么用"


def test_library_list_text_mentions_all_flag_when_undelivered_hidden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """默认只列达标资产时，必须提示"加 --all 才显示未达标"。

    不提示的话，用户会以为历史里根本没有未达标的运行 ——
    而那些恰恰是最需要回头看的东西。
    """
    _patch_log_dir(monkeypatch, tmp_path)
    _asset_run(tmp_path, "ok", "分析销售数据并给出改进建议", 9.0)
    _asset_run(tmp_path, "undelivered", "分析销售数据并给出改进建议", 5.0, status="max_iterations")

    assert cli_library._library_command([]) == 0
    out = capsys.readouterr().out
    assert "--all" in out, "隐藏了未达标交付却没告诉读者怎么看到它们"


def test_library_list_with_all_flag_shows_undelivered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """加了 `--all` 要真的列出来（否则那个提示是空头承诺）。"""
    _patch_log_dir(monkeypatch, tmp_path)
    _asset_run("ok") if False else _asset_run(tmp_path, "ok", "分析销售数据并给出改进建议", 9.0)
    _asset_run(tmp_path, "undelivered", "分析销售数据并给出改进建议", 5.0, status="max_iterations")

    assert cli_library._library_command(["--all"]) == 0
    out = capsys.readouterr().out
    assert "undelivered" in out
    # 加了 --all 之后就不该再提示"加 --all 才显示"
    assert "加 --all 才显示" not in out


def test_library_list_empty_text_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """空资产库的人读出口要给出下一步（而不是只打印一个空标题）。"""
    _patch_log_dir(monkeypatch, tmp_path)
    assert cli_library._library_command([]) == 0
    out = capsys.readouterr().out
    # 空库同样走提前返回：给一句"跑几个真实任务后会积累起来"，
    # 而不是打印一个 "0 条：" 的空标题（后者让人以为命令坏了）。
    assert "没有匹配的提示词" in out
    assert "积累" in out


@pytest.mark.parametrize("bad_name", ["run_broken1.json", "run_broken2.json"])
def test_library_survives_corrupt_run_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    bad_name: str,
) -> None:
    """单个 run 文件损坏时**跳过它**，不能让整条命令崩掉。

    仓库文档明确记着"不能让一颗坏牙毁掉整份体检"（`history` 同款处理）。
    三个扫描点（recommend / export / list）各自都有这个 try/except，此处一并覆盖：
    损坏文件 + 一个正常资产同存时，正常资产仍要被列出来。
    """
    _patch_log_dir(monkeypatch, tmp_path)
    (tmp_path / bad_name).write_text("{ 这不是合法 JSON", encoding="utf-8")
    # 另一种坏法：能读但 JSON 结构不对（顶层不是对象）
    (tmp_path / "run_broken3.json").write_text('["数组不是对象"]', encoding="utf-8")
    _asset_run(tmp_path, "healthy", "分析销售数据并给出改进建议", 9.0)

    assert cli_library._library_command([]) == 0
    out = capsys.readouterr().out
    assert "healthy" in out, "坏文件把正常资产也一起带没了"


def test_library_export_skips_corrupt_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--export` 扫描时同样要跳过坏文件（第三处 try/except）。"""
    _patch_log_dir(monkeypatch, tmp_path)
    (tmp_path / "run_badbadbad.json").write_text("不是 JSON", encoding="utf-8")
    _asset_run(tmp_path, "goodone", "分析销售数据并给出改进建议", 9.0)
    out_path = tmp_path / "exported.md"
    assert cli_library._library_command(["--export", "goodone", "--out", str(out_path)]) == 0
    assert out_path.exists(), "坏文件挡在了导出前面"
    assert out_path.read_text(encoding="utf-8").strip()


def test_library_recommend_skips_corrupt_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--recommend` 扫描时跳过坏文件（第一处 try/except），正常资产仍被推荐。"""
    _patch_log_dir(monkeypatch, tmp_path)
    (tmp_path / "run_badbadbad.json").write_text("不是 JSON", encoding="utf-8")
    _asset_run(tmp_path, "goodrec", "帮我写一个 prompt 让 AI 分析销售数据", 9.0)
    ns = argparse.Namespace(task_text="分析销售数据的需求描述", json=True)
    assert cli_library._library_recommend(ns) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [r["run_id"] for r in payload["recommendations"]] == ["goodrec"]


def test_library_command_routes_recommend_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--recommend` 必须经**子命令入口**分发到推荐实现（不只是直接调内部函数）。

    我前面几条推荐测试都是直接调 `_library_recommend`，所以"入口那一行分发"
    一直没被走到 —— 而它坏了的话，用户敲 `library --recommend` 会静默落到
    默认的列表分支（看到一堆资产列表，而不是推荐结果），且不报任何错。
    """
    _patch_log_dir(monkeypatch, tmp_path)
    _asset_run(tmp_path, "viaroute", "帮我写一个 prompt 让 AI 分析销售数据", 9.0)
    assert (
        cli_library._library_command(["--recommend", "--task-text", "分析销售数据", "--json"]) == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert [r["run_id"] for r in payload["recommendations"]] == ["viaroute"]


def test_library_command_routes_export_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--export` 同样要经入口分发（且坏文件不挡路）。"""
    _patch_log_dir(monkeypatch, tmp_path)
    # 坏文件放在前面（按 mtime 排序会先扫到它）→ 覆盖 export 扫描里的跳过分支
    (tmp_path / "run_zzz_broken.json").write_text('{"status": "passed"', encoding="utf-8")
    _asset_run(tmp_path, "viaroute2", "分析销售数据并给出改进建议", 9.0)
    out_path = tmp_path / "o.md"
    assert cli_library._library_command(["--export", "viaroute2", "--out", str(out_path)]) == 0
    assert out_path.exists()


def test_library_list_skips_corrupt_files_but_keeps_valid_ones(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """列表扫描遇到坏文件要跳过（覆盖 list 那一处的 `continue`），正常资产照常列出。"""
    _patch_log_dir(monkeypatch, tmp_path)
    (tmp_path / "run_brk_a.json").write_text("不是 JSON", encoding="utf-8")
    (tmp_path / "run_brk_b.json").write_text('"字符串也是合法 JSON"', encoding="utf-8")
    _asset_run(tmp_path, "survivor", "分析销售数据并给出改进建议", 9.0)
    assert cli_library._library_command([]) == 0
    out = capsys.readouterr().out
    assert "survivor" in out


def test_load_run_file_rejects_every_bad_shape(tmp_path: Path) -> None:
    """`_load_run_file` 的三种坏法都要返回 None（这是三处扫描点共用的唯一判据）。

    写成表驱动的单点测试：以后有人扩了"什么算坏文件"，这里是唯一需要加用例的地方。
    """
    good = tmp_path / "good.json"
    good.write_text('{"status": "passed"}', encoding="utf-8")
    assert cli_library._load_run_file(good) == {"status": "passed"}

    missing = tmp_path / "__不存在__.json"
    assert cli_library._load_run_file(missing) is None

    not_json = tmp_path / "not_json.json"
    not_json.write_text("{ 这不是 JSON", encoding="utf-8")
    assert cli_library._load_run_file(not_json) is None

    # JSON 合法但顶层不是对象 —— 这一种此前会让整个命令崩掉（实测）
    wrong_shape = tmp_path / "wrong_shape.json"
    wrong_shape.write_text('["数组不是对象"]', encoding="utf-8")
    assert cli_library._load_run_file(wrong_shape) is None

    scalar = tmp_path / "scalar.json"
    scalar.write_text("12345", encoding="utf-8")
    assert cli_library._load_run_file(scalar) is None

    # 非 UTF-8 字节：读的时候就抛 UnicodeDecodeError（同样要算坏文件）
    bad_bytes = tmp_path / "bad_bytes.json"
    bad_bytes.write_bytes(b"\xff\xfe\x00\x01not-utf8")
    assert cli_library._load_run_file(bad_bytes) is None


def test_library_recommend_filters_non_passed_and_empty_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """推荐扫描的两个过滤闸：非 passed、无正文。

    它们都必须**真的挡住**对应资产，否则推荐里会出现"未达标输出"或
    "没有正文的空壳" —— 前者借鉴了会带偏，后者根本无从借鉴。
    与 `test_library_recommend_ranks_and_dedups` 里那条 demo 排除（fake 通道）
    合起来，正好把推荐扫描的三道前置闸各走一遍。
    """
    _patch_log_dir(monkeypatch, tmp_path)
    _asset_run(tmp_path, "good", "帮我写一个 prompt 让 AI 分析销售数据", 9.0)
    # 闸①：非 passed（未达标交付不做参考）
    _asset_run(
        tmp_path,
        "undelivered",
        "帮我写一个 prompt 让 AI 分析销售数据",
        9.5,
        status="max_iterations",
    )
    # 闸②：passed 但正文为空（空壳，无从借鉴）
    _write_run(
        tmp_path,
        "emptyprompt",
        task="帮我写一个 prompt 让 AI 分析销售数据",
        status="passed",
        prompt="   ",
        aggregate={"avg_score": 9.9},
        trace=[{"node": "test", "channel": "real"}],
    )

    ns = argparse.Namespace(task_text="分析销售数据的需求描述", json=True)
    assert cli_library._library_recommend(ns) == 0
    payload = json.loads(capsys.readouterr().out)
    ids = [r["run_id"] for r in payload["recommendations"]]
    assert ids == ["good"], f"未达标/空正文的资产混进了推荐：{ids}"


def test_library_list_skips_non_terminal_and_empty_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """列表扫描的准入闸：非终态状态、空正文，都要被挡在外面。

    运行中途失败的记录（status=running）与空壳交付混进资产库，
    会让人误以为"历史里有这版提示词可用"。
    """
    _patch_log_dir(monkeypatch, tmp_path)
    _asset_run(tmp_path, "keepme", "分析销售数据并给出改进建议", 9.0)
    _write_run(
        tmp_path,
        "running",
        task="分析销售数据并给出改进建议",
        status="running",
        prompt="# 还没跑完的中间产物",
        aggregate={"avg_score": None},
        trace=[{"node": "test", "channel": "real"}],
    )
    _write_run(
        tmp_path,
        "emptybody",
        task="分析销售数据并给出改进建议",
        status="passed",
        prompt="  \n ",
        aggregate={"avg_score": 9.0},
        trace=[{"node": "test", "channel": "real"}],
    )

    assert cli_library._library_command(["--all"]) == 0
    out = capsys.readouterr().out
    assert "keepme" in out
    assert "running" not in out, "非终态记录混进了资产库"
    assert "emptybody" not in out, "空正文记录混进了资产库"


def test_library_export_skips_bad_json_shapes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """export 扫描里的坏文件跳过（第三处 `_load_run_file` 调用点）。

    这里特意放**结构不对但 JSON 合法**的那种（`[...]`）——
    它正是 2026-10-02 实测会让整个命令崩掉的形态；
    只防 `JSONDecodeError` 的实现会在这里抛 AttributeError。
    """
    _patch_log_dir(monkeypatch, tmp_path)
    # ⚠️ 顺序决定这条测试有没有测到东西：扫描按 mtime **倒序**，
    # 且一旦命中目标 run_id 就 `break`。所以坏文件必须**排在正常文件之前**
    # （即 mtime 更新），否则扫描先撞上正常文件直接跳出，跳过分支永远走不到
    # —— 我第一版就是这么写的，覆盖率卡在 99% 不动。
    _asset_run(tmp_path, "expok", "分析销售数据并给出改进建议", 8.0)
    import time as _time

    _time.sleep(0.05)
    (tmp_path / "run_shape_bad.json").write_text('["不是对象"]', encoding="utf-8")
    (tmp_path / "run_text_bad.json").write_text("根本不是 JSON", encoding="utf-8")

    out = tmp_path / "x.md"
    assert cli_library._library_command(["--export", "expok", "--out", str(out)]) == 0
    assert "run_id: expok" in out.read_text(encoding="utf-8")
