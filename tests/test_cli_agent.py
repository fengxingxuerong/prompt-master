"""pm/cli/ 异步客户端、校准、流水线与自检的函数级测试。

- agent_mode：HTTP 客户端错误翻译、payload 组装、wait 轮询的终态/超时/失败分支
- calibrate：样本护栏、漂移告警判定、--no-save 只看不记账
- pipeline：interrupt 探测、假后端直跑、无效场景告警与交互 resume 循环
- selftest：整条自检命令的三场景冒烟（假后端，不联网）
"""

from __future__ import annotations

import argparse
import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import calibrate_judge  # noqa: E402  # 仓库根顶层脚本，_calibrate_command 运行时按名导入
from pm.cli import agent_mode  # noqa: E402
from pm.cli import calibrate as cli_calibrate  # noqa: E402
from pm.cli import support as cli_support  # noqa: E402
from pm.cli.history import _TERMINAL_STATUS  # noqa: E402
from pm.cli.pipeline import _pending_interrupts, run_pipeline  # noqa: E402
from pm.cli.selftest import selftest  # noqa: E402
from pm.state import initial_state  # noqa: E402


# ---------------------------------------------------------------------------
# agent_mode._http_json：错误必须带可执行建议
# ---------------------------------------------------------------------------
class _FakeResp:
    def __init__(self, body: bytes) -> None:
        self._b = body

    def read(self) -> bytes:
        return self._b

    def __enter__(self) -> _FakeResp:
        return self

    def __exit__(self, *args: Any) -> bool:
        return False


def test_http_json_ok_post_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    import urllib.request as ur

    captured: dict[str, Any] = {}

    def fake_urlopen(req: Any, timeout: float | None = None) -> _FakeResp:
        captured["method"] = req.get_method()
        captured["url"] = req.full_url
        captured["data"] = req.data
        return _FakeResp(b'{"ok": true}')

    monkeypatch.setattr(ur, "urlopen", fake_urlopen)
    assert agent_mode._http_json("POST", "http://x/api/optimize", {"a": 1}) == {"ok": True}
    assert captured["method"] == "POST"
    assert json.loads(captured["data"].decode("utf-8")) == {"a": 1}


def test_http_json_http_error_includes_body(monkeypatch: pytest.MonkeyPatch) -> None:
    import urllib.error
    import urllib.request as ur

    def fake_urlopen(req: Any, timeout: float | None = None) -> _FakeResp:
        raise urllib.error.HTTPError(req.full_url, 500, "boom", {}, io.BytesIO(b"server died"))

    monkeypatch.setattr(ur, "urlopen", fake_urlopen)
    with pytest.raises(RuntimeError) as ei:
        agent_mode._http_json("GET", "http://x/api/status/r1")
    assert "HTTP 500" in str(ei.value) and "server died" in str(ei.value)


def test_http_json_url_error_suggests_run_server(monkeypatch: pytest.MonkeyPatch) -> None:
    import urllib.error
    import urllib.request as ur

    def fake_urlopen(req: Any, timeout: float | None = None) -> _FakeResp:
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(ur, "urlopen", fake_urlopen)
    with pytest.raises(RuntimeError) as ei:
        agent_mode._http_json("GET", "http://x/api/status/r1")
    assert "run_server.py" in str(ei.value)


# ---------------------------------------------------------------------------
# agent_mode：server 解析与子命令
# ---------------------------------------------------------------------------
def test_agent_server_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PM_SERVER_URL", raising=False)
    assert agent_mode._agent_server(argparse.Namespace(server=None)) == "http://127.0.0.1:8080"
    monkeypatch.setenv("PM_SERVER_URL", "http://env-host:9000/")
    # 环境变量生效 + 末尾斜杠去除
    assert agent_mode._agent_server(argparse.Namespace(server=None)) == "http://env-host:9000"
    # 显式 --server 最优先
    ns = argparse.Namespace(server="http://cli-host:1/")
    assert agent_mode._agent_server(ns) == "http://cli-host:1"


def test_agent_submit_requires_task() -> None:
    with pytest.raises(SystemExit):
        agent_mode.agent_subcommand("submit", [])


def test_agent_submit_payload_from_cases_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: dict[str, Any] = {}

    def fake(
        method: str, url: str, payload: dict[str, Any] | None = None, timeout: float = 30.0
    ) -> dict[str, Any]:
        seen.update(method=method, url=url, payload=payload)
        return {"run_id": "R1"}

    monkeypatch.setattr(agent_mode, "_http_json", fake)
    cf = tmp_path / "cases.json"
    cf.write_text(
        json.dumps(
            [{"input": "a", "expected": "b", "mode": "rule"}, {"input": "c"}], ensure_ascii=False
        ),
        encoding="utf-8",
    )
    assert agent_mode.agent_subcommand("submit", ["--task", "T", "--cases-file", str(cf)]) == 0
    assert seen["method"] == "POST" and seen["url"].endswith("/api/optimize")
    p = seen["payload"]
    # 用例数以用例集为准；mode 逐条透传；无 mode 的用例留空串
    assert p["n_test_cases"] == 2
    assert p["test_cases"][0]["assert_mode"] == "rule"
    assert p["test_cases"][1]["assert_mode"] == ""
    assert p["assertion_mode"] == "contains"

    # 无用例集：不带 test_cases，断言模式固定 contains
    seen.clear()
    assert agent_mode.agent_subcommand("submit", ["--task", "T", "--assert-mode", "rule"]) == 0
    p2 = seen["payload"]
    assert "test_cases" not in p2 and p2["assertion_mode"] == "contains"


def test_agent_status_and_report(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        agent_mode,
        "_http_json",
        lambda m, u, payload=None, timeout=30.0: {"status": "passed", "report": "R 内容"},
    )
    assert agent_mode.agent_subcommand("status", ["r1"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "passed"

    assert agent_mode.agent_subcommand("report", ["r1"]) == 0
    assert json.loads(capsys.readouterr().out)["report"] == "R 内容"

    out = tmp_path / "report.md"
    assert agent_mode.agent_subcommand("report", ["r1", "--out", str(out)]) == 0
    assert out.read_text(encoding="utf-8") == "R 内容"
    assert json.loads(capsys.readouterr().out)["report_path"] == str(out)


def test_agent_wait_reaches_terminal_and_fetches_report(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    polls: list[str] = []

    def fake(
        method: str, url: str, payload: dict[str, Any] | None = None, timeout: float = 30.0
    ) -> dict[str, Any]:
        polls.append(url)
        if "/api/status/" in url:
            if len([p for p in polls if "/api/status/" in p]) == 1:
                return {"status": "running", "iteration": 1}
            return {
                "status": "passed",
                "aggregate": {"avg_score": 9.0},
                "iteration": 2,
                "llm_calls": 9,
            }
        return {"report": "最终报告"}

    monkeypatch.setattr(agent_mode, "_http_json", fake)
    monkeypatch.setattr(agent_mode.time, "sleep", lambda s: None)
    assert agent_mode.agent_subcommand("wait", ["r1", "--interval", "5"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "passed"
    assert payload["report"] == "最终报告"
    assert payload["aggregate"] == {"avg_score": 9.0}


def test_agent_wait_timeout(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        agent_mode,
        "_http_json",
        lambda m, u, payload=None, timeout=30.0: {"status": "running"},
    )
    monkeypatch.setattr(agent_mode.time, "sleep", lambda s: None)
    clock = iter([0.0, 1e12])  # deadline=max(30,timeout) 后直接超时
    monkeypatch.setattr(agent_mode.time, "monotonic", lambda: next(clock))
    assert agent_mode.agent_subcommand("wait", ["r1"]) == cli_support.EXIT_FAILED
    assert json.loads(capsys.readouterr().out)["error"] == "等待超时"


def test_agent_wait_failed_terminal_no_report_fetch(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    urls: list[str] = []

    def fake(
        method: str, url: str, payload: dict[str, Any] | None = None, timeout: float = 30.0
    ) -> dict[str, Any]:
        urls.append(url)
        return {"status": "failed", "error": "炸了"}

    monkeypatch.setattr(agent_mode, "_http_json", fake)
    monkeypatch.setattr(agent_mode.time, "sleep", lambda s: None)
    assert agent_mode.agent_subcommand("wait", ["r1"]) == cli_support.EXIT_FAILED
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "failed" and payload["report"] == ""
    assert all("/api/report/" not in u for u in urls), "failed 终态不该再拉报告"
    assert "passed" in _TERMINAL_STATUS  # 终态集合与 history 共用，别漂移


# ---------------------------------------------------------------------------
# calibrate：样本护栏 + 漂移账本
# ---------------------------------------------------------------------------
def _patch_calib(
    monkeypatch: pytest.MonkeyPatch, analysis: dict[str, Any], errors: list[Any] | None = None
) -> None:
    monkeypatch.setattr(calibrate_judge, "load_samples", lambda p: [{"id": "s1"}])
    monkeypatch.setattr(
        calibrate_judge,
        "calibrate",
        lambda samples, judge, mode="impression": (analysis, list(errors or [])),
    )
    monkeypatch.setattr(calibrate_judge, "render_report", lambda judge, a: f"REPORT-{judge}")


def test_calibrate_missing_samples(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert (
        cli_calibrate._calibrate_command(["--samples", str(tmp_path / "nope.json")])
        == cli_support.EXIT_CONFIG
    )
    assert "样本加载失败" in capsys.readouterr().err


def test_calibrate_empty_samples(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    f = tmp_path / "s.json"
    f.write_text("[]", encoding="utf-8")
    assert cli_calibrate._calibrate_command(["--samples", str(f)]) == cli_support.EXIT_CONFIG
    assert "样本为空" in capsys.readouterr().err


def test_calibrate_all_anchors_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_calib(monkeypatch, {}, errors=[("s1", "超时")])
    assert cli_calibrate._calibrate_command([]) == cli_support.EXIT_FAILED
    err = capsys.readouterr().err
    assert "[SKIP] s1" in err and "全部锚点评估失败" in err


def test_calibrate_first_run_no_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from test_cli_units import _patch_log_dir  # tests 无包结构，顶层互导

    _patch_log_dir(monkeypatch, tmp_path)
    _patch_calib(monkeypatch, {"n": 5, "bias": 0.2, "mae": 0.3, "r": 0.9})
    assert cli_calibrate._calibrate_command(["--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["drift"] is None and payload["saved"] is True
    assert payload["history_len"] == 1
    assert (tmp_path / "judge_calibration_history.json").exists()


def test_calibrate_drift_alert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from test_cli_units import _patch_log_dir  # tests 无包结构，顶层互导

    _patch_log_dir(monkeypatch, tmp_path)
    history = [{"ts": "t0", "judge": "evaluator", "n": 5, "bias": 0.9, "mae": 0.9, "r": 0.8}]
    (tmp_path / "judge_calibration_history.json").write_text(json.dumps(history), encoding="utf-8")
    _patch_calib(monkeypatch, {"n": 5, "bias": 0.2, "mae": 0.3, "r": 0.9})
    assert cli_calibrate._calibrate_command(["--json"]) == 0
    d = json.loads(capsys.readouterr().out)["drift"]
    assert d["delta_bias"] == -0.7 and d["delta_mae"] == -0.6
    assert d["drifted"] is True  # 任一变化超 ±0.5 即告警
    # 账本在旧记录之后追加
    saved = json.loads((tmp_path / "judge_calibration_history.json").read_text(encoding="utf-8"))
    assert len(saved) == 2 and saved[-1]["bias"] == 0.2


def test_calibrate_no_save_keeps_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from test_cli_units import _patch_log_dir  # tests 无包结构，顶层互导

    _patch_log_dir(monkeypatch, tmp_path)
    _patch_calib(monkeypatch, {"n": 5, "bias": 0.0, "mae": 0.0, "r": None})
    assert cli_calibrate._calibrate_command(["--json", "--no-save"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["saved"] is False and payload["history_len"] == 0 and payload["drift"] is None
    assert not (tmp_path / "judge_calibration_history.json").exists()


# ---------------------------------------------------------------------------
# pipeline：interrupt 探测与执行
# ---------------------------------------------------------------------------
def test_pending_interrupts_collects_values() -> None:
    snap = SimpleNamespace(
        tasks=[
            SimpleNamespace(
                interrupts=[SimpleNamespace(value={"q": 1}), SimpleNamespace(value={"q": 2})]
            ),
            SimpleNamespace(interrupts=[]),
        ]
    )
    app = SimpleNamespace(get_state=lambda config: snap)
    assert _pending_interrupts(app, {}) == [{"q": 1}, {"q": 2}]


def test_run_pipeline_fake_progress(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PM_FAKE_BACKEND", "progress")
    args = SimpleNamespace(checkpoint=None, thread_id=None, interactive=False, max_iter=1)
    init = initial_state(
        task="让 AI 分析销售数据",
        target_model="fake-target",
        n_test_cases=2,
        max_iterations=1,
        auto_clarify=True,
    )
    final = run_pipeline(args, init)
    assert final["run_id"] == init["run_id"]
    assert "llm_usage" in final  # 台账快照写回 state


def test_run_pipeline_invalid_scenario_warns(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from pm.cli import pipeline as cli_pipeline

    monkeypatch.setenv("PM_FAKE_BACKEND", "bogus")
    invoked: list[Any] = []

    class _Stub:
        def invoke(self, init: Any, config: Any) -> dict[str, Any]:
            invoked.append(init)
            return {}

        def get_state(self, config: Any) -> SimpleNamespace:
            return SimpleNamespace(values={"run_id": "stub", "status": "failed"})

    monkeypatch.setattr(cli_pipeline, "build_app", lambda sqlite_path=None: _Stub())
    args = SimpleNamespace(checkpoint=None, thread_id="t9", interactive=False, max_iter=1)
    final = run_pipeline(args, {"run_id": "stub"})
    assert "不是可用场景" in capsys.readouterr().out and invoked
    assert "llm_usage" in final


def test_run_pipeline_interactive_resumes_interrupts(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """交互模式：命中澄清中断 → 读回答 → resume → 下轮无中断收尾。"""
    from pm.cli import pipeline as cli_pipeline

    monkeypatch.setenv("PM_FAKE_BACKEND", "progress")
    resumes: list[Any] = []
    states = iter(
        [
            SimpleNamespace(
                tasks=[
                    SimpleNamespace(
                        interrupts=[
                            SimpleNamespace(
                                value={"task_summary": "销售分析", "questions": ["口径是什么？"]}
                            )
                        ]
                    )
                ]
            ),
            SimpleNamespace(tasks=[]),
            SimpleNamespace(values={"run_id": "stub", "final_report": "R"}),
        ]
    )
    app = SimpleNamespace(
        invoke=lambda init, config: resumes.append("invoke"),
        get_state=lambda config: next(states),
    )
    monkeypatch.setattr(cli_pipeline, "build_app", lambda sqlite_path=None: app)
    monkeypatch.setattr("builtins.input", lambda *a: "按月度口径")

    args = SimpleNamespace(checkpoint=None, thread_id="t1", interactive=True, max_iter=1)
    final = run_pipeline(args, {"run_id": "stub"})
    assert final["final_report"] == "R"
    assert len(resumes) == 2  # 初次 invoke + resume
    assert "口径是什么" in capsys.readouterr().out


def test_run_pipeline_interactive_eof_falls_back(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """stdin 关闭（EOF）时按"按你的推断继续"推进，不挂死。"""
    from pm.cli import pipeline as cli_pipeline

    monkeypatch.setenv("PM_FAKE_BACKEND", "progress")
    resumes: list[Any] = []
    states = iter(
        [
            SimpleNamespace(
                tasks=[SimpleNamespace(interrupts=[SimpleNamespace(value={"questions": ["Q"]})])]
            ),
            SimpleNamespace(tasks=[]),
            SimpleNamespace(values={"run_id": "stub"}),
        ]
    )

    class _CaptureApp:
        def invoke(self, init: Any, config: Any) -> dict[str, Any]:
            resumes.append(init)
            return {}

        def get_state(self, config: Any) -> Any:
            return next(states)

    monkeypatch.setattr(cli_pipeline, "build_app", lambda sqlite_path=None: _CaptureApp())

    def _eof(*args: Any) -> str:
        raise EOFError

    monkeypatch.setattr("builtins.input", _eof)
    args = SimpleNamespace(checkpoint=None, thread_id="t2", interactive=True, max_iter=1)
    assert run_pipeline(args, {"run_id": "stub"})["run_id"] == "stub"
    assert len(resumes) == 2


# ---------------------------------------------------------------------------
# selftest：整条自检命令冒烟（三场景假后端）
# ---------------------------------------------------------------------------
def test_selftest_command_passes(capsys: pytest.CaptureFixture[str]) -> None:
    assert selftest() == 0
    out = capsys.readouterr().out
    assert "自检通过" in out
    assert "[FAIL]" not in out, "三场景任一检查失败都算自检失败"


def _seed_history(tmp_path: Path, rows: list[dict]) -> None:
    (tmp_path / "judge_calibration_history.json").write_text(json.dumps(rows), encoding="utf-8")


def test_calibrate_drift_ignores_records_from_another_rubric(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """改了评分提示词（锚点收紧）之后的 MAE 阶跃不是漂移：基线要跳过不可比的那条。"""
    from test_cli_units import _patch_log_dir  # tests 无包结构，顶层互导

    _patch_log_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(
        cli_calibrate, "_calib_fingerprints", lambda role, samples, mode: ("glm-5.2", "r2", "a1")
    )
    _seed_history(
        tmp_path,
        [
            {
                "ts": "t0",
                "judge": "evaluator",
                "n": 5,
                "bias": 0.1,
                "mae": 0.4,
                "r": 0.9,
                "model": "glm-5.2",
                "rubric": "r2",
            },  # 同口径，可比
            {
                "ts": "t1",
                "judge": "evaluator",
                "n": 5,
                "bias": 1.3,
                "mae": 1.3,
                "r": 0.98,
                "model": "glm-5.2",
                "rubric": "r1",
            },  # 旧 rubric，不可比且更新
        ],
    )
    _patch_calib(monkeypatch, {"n": 5, "bias": 0.2, "mae": 0.5, "r": 0.95})
    assert cli_calibrate._calibrate_command(["--json"]) == 0
    d = json.loads(capsys.readouterr().out)["drift"]
    assert d["prev_ts"] == "t0", "应跳过不可比的 t1，与同口径的 t0 比"
    assert d["delta_mae"] == 0.1 and d["drifted"] is False


def test_calibrate_reports_no_comparable_baseline_instead_of_fake_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """只有旧口径记录时：说清"尺子变了"，不当成评委漂移告警。"""
    from test_cli_units import _patch_log_dir

    _patch_log_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(
        cli_calibrate, "_calib_fingerprints", lambda role, samples, mode: ("glm-5.2", "r2", "a1")
    )
    _seed_history(
        tmp_path,
        [
            {
                "ts": "t1",
                "judge": "evaluator",
                "n": 5,
                "bias": 1.3,
                "mae": 1.3,
                "r": 0.98,
                "model": "glm-5.2",
                "rubric": "r1",
            }
        ],
    )
    _patch_calib(monkeypatch, {"n": 5, "bias": 0.2, "mae": 0.5, "r": 0.95})
    assert cli_calibrate._calibrate_command(["--json"]) == 0
    d = json.loads(capsys.readouterr().out)["drift"]
    assert d["comparable"] is False and d["drifted"] is False
    assert "评分提示词已改动" in d["why_not_comparable"]


def test_calibrate_ledger_records_cohort_fingerprints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """新记录必须带上模型与 rubric 指纹，否则下次没法判断可不可比。"""
    from test_cli_units import _patch_log_dir

    _patch_log_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(
        cli_calibrate,
        "_calib_fingerprints",
        lambda role, samples, mode: ("glm-5.2", "abc123", "a1"),
    )
    _patch_calib(monkeypatch, {"n": 5, "bias": 0.0, "mae": 0.3, "r": 0.9})
    assert cli_calibrate._calibrate_command(["--json"]) == 0
    capsys.readouterr()
    saved = json.loads((tmp_path / "judge_calibration_history.json").read_text(encoding="utf-8"))
    assert saved[-1]["model"] == "glm-5.2" and saved[-1]["rubric"] == "abc123"
    assert saved[-1]["anchors"] == "a1", "锚点集指纹漏记 = 下次没法判断是不是换了考卷"


# ---------------------------------------------------------------------------
# 复现性测量（calibrate --repeat）
# ---------------------------------------------------------------------------
def test_calibrate_repeat_lands_in_ledger_and_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """--repeat 的读数必须进账本，否则换评委型号后没法区分"仪表换了"与"评委漂了"。"""
    from test_cli_units import _patch_log_dir

    _patch_log_dir(monkeypatch, tmp_path)
    _patch_calib(monkeypatch, {"n": 5, "bias": 0.2, "mae": 0.5, "r": 0.9})
    monkeypatch.setattr(
        calibrate_judge,
        "repeatability",
        lambda samples, judge, times, mode="impression": {
            "role": judge,
            "times": times,
            "n_items": 2,
            "range_mean": 1.4,
            "range_max": 2.6,
            "worst_id": "s1",
            "disagreement_threshold": 2.0,
            "threshold_exceeded": True,
            "per_item": [],
        },
    )
    assert cli_calibrate._calibrate_command(["--json", "--repeat", "3"]) == 0
    out = json.loads(capsys.readouterr().out)
    rep = out["analysis"]["repeatability"]
    assert rep["range_mean"] == 1.4 and rep["threshold_exceeded"] is True
    saved = json.loads((tmp_path / "judge_calibration_history.json").read_text(encoding="utf-8"))
    assert saved[-1]["rep_range_max"] == 2.6 and saved[-1]["rep_times"] == 3
    assert saved[-1]["rep_threshold"] == 2.0


def test_repeat_not_requested_does_not_measure_or_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """不带 --repeat 时一次都不许多打：复现性测量是 N×锚点 的真实花费。"""
    from test_cli_units import _patch_log_dir

    _patch_log_dir(monkeypatch, tmp_path)
    _patch_calib(monkeypatch, {"n": 5, "bias": 0.0, "mae": 0.3, "r": 0.9})

    def _boom(*a, **k):
        raise AssertionError("未请求 --repeat 却做了复现性测量")

    monkeypatch.setattr(calibrate_judge, "repeatability", _boom)
    assert cli_calibrate._calibrate_command(["--json"]) == 0
    capsys.readouterr()
    saved = json.loads((tmp_path / "judge_calibration_history.json").read_text(encoding="utf-8"))
    assert "rep_range_mean" not in saved[-1]


def test_repeatability_measures_variance_with_real_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真函数：同一输入打出 8.0 / 5.5 时极差必须是 2.5，并标出淹没阈值。"""
    scores = iter([8.0, 5.5, 7.0])

    class _Ev:
        def __init__(self, v: float) -> None:
            self.weighted_score = v

    monkeypatch.setattr(
        calibrate_judge, "evaluate_sample", lambda role, s, mode="impression": _Ev(next(scores))
    )
    monkeypatch.setenv("PM_JUDGE_DISAGREEMENT", "2.0")
    rep = calibrate_judge.repeatability([{"id": "s1"}], "evaluator", times=3)
    assert rep["n_items"] == 1
    assert rep["range_max"] == 2.5
    assert rep["per_item"][0]["scores"] == [8.0, 5.5, 7.0]
    assert rep["per_item"][0]["median"] == 7.0
    # 2.5 > 阈值 2.0 → 这台仪表的自我分歧足以自己触发仲裁
    assert rep["threshold_exceeded"] is True


def test_repeatability_bypasses_cache_but_keeps_the_injected_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """绕缓存的那层必须在**不丢掉假后端**的前提下生效。

    两个坑各测一次：① 不绕缓存的话第二次永远命中，极差恒 0，等于自证"很稳定"；
    ② 绕缓存若用 `backend.use(CallHook(disable_cache=True))` 整体替换，会把
    testing.scope 注入的假后端一起丢掉，测试就会开始打真实端点。
    """
    from pm import backend, testing

    seen: dict[str, bool] = {}

    class _Ev:
        def __init__(self, v: float) -> None:
            self.weighted_score = v

    def fake_eval(role: str, s: dict, mode: str = "impression"):
        seen["cache_off"] = seen.get("cache_off", False) or backend.cache_disabled()
        seen["hook_still_fake"] = (
            backend.current() is not None and backend.current().structured is not None
        )
        return _Ev(7.0)

    monkeypatch.setattr(calibrate_judge, "evaluate_sample", fake_eval)
    with testing.scope("progress"):  # 假后端在册
        calibrate_judge.repeatability([{"id": "s1"}], "evaluator", times=2)
    assert seen["cache_off"] is True, "复现性测量必须绕开评估缓存"
    assert seen["hook_still_fake"] is True, "绕缓存不许把假后端一起换掉"
    assert backend.cache_disabled() is False, "退出上下文后必须恢复"


def test_repeatability_survives_a_failing_item(monkeypatch: pytest.MonkeyPatch) -> None:
    """端点抖到某条打不通时不能整体崩，标出来即可。"""

    class _Ev:
        def __init__(self, v: float) -> None:
            self.weighted_score = v

    def fake_eval(role: str, s: dict, mode: str = "impression"):
        if s["id"] == "bad":
            raise RuntimeError("engine is not available temporarily")
        return _Ev(6.0)

    monkeypatch.setattr(calibrate_judge, "evaluate_sample", fake_eval)
    rep = calibrate_judge.repeatability([{"id": "bad"}, {"id": "ok"}], "arbiter", times=3)
    assert rep["n_items"] == 1 and rep["range_max"] == 0.0
    bad = next(x for x in rep["per_item"] if x["id"] == "bad")
    assert "RuntimeError" in bad["error"]


def test_disagreement_threshold_has_one_source(monkeypatch: pytest.MonkeyPatch) -> None:
    """阈值只许有一个定义处，否则校准说"淹没阈值"而主管道用的是另一个数。"""
    from pm.nodes import judge as judge_mod
    from pm.schemas import judge_disagreement_threshold

    monkeypatch.setenv("PM_JUDGE_DISAGREEMENT", "3.5")
    assert judge_disagreement_threshold() == 3.5
    assert judge_mod._judge_disagreement_threshold() == 3.5
    monkeypatch.setenv("PM_JUDGE_DISAGREEMENT", "垃圾值")
    assert judge_disagreement_threshold() == 2.0, "非法值回退默认而不是崩"


def test_render_repeatability_wording_is_conditional() -> None:
    """稳的时说"信号多于噪声"，抖的时候才告警——不许无脑印警告。"""
    base = {
        "times": 3,
        "n_items": 2,
        "range_mean": 0.4,
        "range_max": 0.6,
        "worst_id": "s1",
        "disagreement_threshold": 2.0,
        "per_item": [],
    }
    ok = calibrate_judge.render_repeatability({**base, "threshold_exceeded": False})
    assert "✅" in ok and "⚠️" not in ok
    bad = calibrate_judge.render_repeatability(
        {**base, "range_max": 4.43, "threshold_exceeded": True}
    )
    assert "抖动已淹没阈值" in bad
    assert "median-of-3" in bad


def test_calibrate_anchor_set_change_is_not_judge_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """换考卷不是评委漂移：6 条手写锚点的基线不能拿来判 11 条含真实锚点的读数。

    老记录连模型/rubric 指纹都没有（账本里确实存在这种行），所以 `n` 是这里唯一的信号；
    而 `_comparable` 对"缺指纹"是宽松放行的，因此放行条件必须同时看 n。
    """
    from test_cli_units import _patch_log_dir

    _patch_log_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(
        cli_calibrate, "_calib_fingerprints", lambda role, samples, mode: ("glm-5.2", "r2", "a2")
    )
    _seed_history(
        tmp_path,
        [{"ts": "legacy", "judge": "evaluator", "n": 6, "bias": 1.24, "mae": 1.32, "r": 0.907}],
    )
    _patch_calib(monkeypatch, {"n": 11, "bias": 0.41, "mae": 0.9, "r": 0.925})
    assert cli_calibrate._calibrate_command(["--json"]) == 0
    d = json.loads(capsys.readouterr().out)["drift"]
    assert d["comparable"] is False, "6 条 → 11 条是换考卷，MAE 1.32→0.9 不是评委变好"
    assert "锚点条数不同" in d["why_not_comparable"]


def test_anchors_stamp_tracks_scores_not_just_ids() -> None:
    """人工分被重新核对过就是换了尺子，指纹必须跟着变。"""
    a = [{"id": "x", "human_score": 6}, {"id": "y", "human_score": 8}]
    b = [{"id": "y", "human_score": 8}, {"id": "x", "human_score": 6}]  # 顺序无关
    c = [{"id": "x", "human_score": 5}, {"id": "y", "human_score": 8}]  # 改了一个分
    assert cli_calibrate._anchors_stamp(a) == cli_calibrate._anchors_stamp(b)
    assert cli_calibrate._anchors_stamp(a) != cli_calibrate._anchors_stamp(c)


def test_calibrate_real_fingerprints_are_populated(monkeypatch: pytest.MonkeyPatch) -> None:
    """没被 monkeypatch 时，指纹要真的取到模型名与 rubric 哈希（不是占位符）。"""
    model, rubric, anchors = cli_calibrate._calib_fingerprints(
        "evaluator", [{"id": "a", "human_score": 5}], "impression"
    )
    assert model and model != "(unknown)"
    assert len(rubric) == 10
    assert len(anchors) == 10
    # 换协议必须换指纹：否则漂移检测会把"换成判定式"读成"评委漂了"
    _m2, rubric2, _a2 = cli_calibrate._calib_fingerprints(
        "evaluator", [{"id": "a", "human_score": 5}], "checklist"
    )
    assert rubric2 != rubric and len(rubric2) == 10
