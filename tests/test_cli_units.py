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
    """support/history/library/calibrate 各自绑定了 LOG_DIR 引用，逐模块换到 tmp。"""
    from pm.cli import calibrate as cli_calibrate

    for mod in (cli_support, cli_history, cli_library):
        monkeypatch.setattr(mod, "LOG_DIR", tmp_path)
    monkeypatch.setattr(
        cli_calibrate, "_CALIB_HISTORY", tmp_path / "judge_calibration_history.json"
    )


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
    monkeypatch.setattr(cli_main, "selftest", lambda: called.append(1) or 0)
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


def test_library_recommend_requires_task_text(monkeypatch: pytest.MonkeyPatch) -> None:
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
