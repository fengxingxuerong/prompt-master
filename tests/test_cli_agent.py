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

# 校准引擎；_calibrate_command 运行时按模块属性打桩
from pm import calibration as calibrate_judge  # noqa: E402
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


def test_agent_submit_accepts_custom_and_rule_modes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`submit --assert-mode` 与同步 CLI 同一白名单：以前这里是 choices 硬编码，
    `custom:<已注册名>` 从 REST 能提交、从 submit 直接报错 —— 半边入口等于没做。"""
    seen: dict[str, Any] = {}

    def fake(
        method: str, url: str, payload: dict[str, Any] | None = None, timeout: float = 30.0
    ) -> dict[str, Any]:
        seen.update(payload=payload)
        return {"run_id": "R1"}

    monkeypatch.setattr(agent_mode, "_http_json", fake)
    cf = tmp_path / "cases.json"
    cf.write_text(
        json.dumps([{"input": "a", "expected": "b"}], ensure_ascii=False), encoding="utf-8"
    )

    for mode in ("rule", "custom:no_apology", "contains", "exact", "regex"):
        assert (
            agent_mode.agent_subcommand(
                "submit", ["--task", "T", "--cases-file", str(cf), "--assert-mode", mode]
            )
            == 0
        ), f"{mode} 不该被解析层拒掉"
        assert seen["payload"]["assertion_mode"] == mode, "模式必须原样发给 server 校验"


def test_agent_submit_rejects_unknown_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """放开白名单不等于不设闸：非法模式仍在解析阶段退出码 2。"""
    monkeypatch.setattr(
        agent_mode, "_http_json", lambda *a, **k: pytest.fail("非法模式不该走到发请求")
    )
    with pytest.raises(SystemExit) as e:
        agent_mode.agent_subcommand("submit", ["--task", "T", "--assert-mode", "bogus"])
    assert e.value.code == 2


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
def test_calibrate_ledger_persists_per_anchor_detail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """账本必须落逐条明细 + 失败清单：只存聚合数的账本，事后连"两臂是不是同一张考卷"都答不出。

    真实事故（2026-09-25 凌晨）：impression n=18 与 checklist n=15 同锚点指纹、n_pending 都是 0，
    于是"MAE 0.83 → 1.85"到底是结论还是幸存者偏差，今天已经无从查证 —— 失败原因当时只打到
    stderr 的 [SKIP] 行，终端一关就没了。
    """
    from test_cli_units import _patch_log_dir  # tests 无包结构，顶层互导

    _patch_log_dir(monkeypatch, tmp_path)
    analysis: dict[str, Any] = {
        "n": 2,
        "bias": 0.5,
        "mae": 0.5,
        "r": 0.9,
        "rho": 0.9,
        "pairs": [
            {"id": "a1", "human": 8.0, "judge": 8.5, "delta": 0.5, "flag": False},
            {"id": "a2", "human": 7.0, "judge": 7.5, "delta": 0.5, "flag": False},
        ],
        "decision": {"agree": 1.0, "kappa": 1.0, "n": 2},
        "repeatability": {
            "times": 3,
            "n_items": 1,
            "range_mean": 0.5,
            "range_max": 0.5,
            "disagreement_threshold": 2.0,
            "per_item": [
                {"id": "a1", "n": 3, "range": 0.5},
                {"id": "a2", "n": 1, "error": "GatewayError: 上游 500"},
            ],
        },
    }
    _patch_calib(monkeypatch, analysis, errors=[("a3", "ValidationError: 清单解析失败")])

    assert cli_calibrate._calibrate_command([]) == 0
    entry = json.loads((tmp_path / "judge_calibration_history.json").read_text(encoding="utf-8"))[
        -1
    ]
    assert entry["n_failed"] == 1 and entry["failed"][0]["id"] == "a3"
    assert "清单解析失败" in entry["failed"][0]["error"]
    assert [p["id"] for p in entry["items"]] == ["a1", "a2"], "逐条 Δ 要能事后配对"
    assert entry["items"][0]["human"] == 8.0 and entry["items"][0]["judge"] == 8.5
    # 每锚点极差也入账：均值会抹平"半数稳、半数疯"，配对只能靠逐条
    assert {i["id"]: i.get("range") for i in entry["rep_items"]} == {"a1": 0.5, "a2": None}


# ---------------------------------------------------------------------------
# 人工打分表：--make-form / --apply-scores（只动表、不调模型）
# ---------------------------------------------------------------------------
def _candidates_file(tmp_path: Path) -> Path:
    p = tmp_path / "candidates.json"
    data: list[Any] = [
        {"_comment": "分隔条目：写回时必须原样保留"},
        {
            "id": "h-1",
            "band": "7.0-7.9",
            "original_task": "给电商客服对话做分类分诊",
            "test_output": "分类：退款\n" * 3,
            "human_score": None,
            "confirmed": False,
            "note": "命中线索：输出里出现「大概」",
            "provenance": {"judge_score": 7.6, "target_model": "glm-5.2"},
        },
        {
            "id": "h-2",
            "band": "<6.0",
            "original_task": "生成分区定时简报",
            "test_output": "今日简报正文",
            "human_score": None,
            "confirmed": False,
            "note": "无线索命中",
            "provenance": {"judge_score": 4.83, "target_model": "glm-5.2"},
        },
        {
            "id": "h-3",
            "band": "8.0-8.9",
            "original_task": "抽取合同关键条款",
            "test_output": "条款清单",
            "human_score": 9.0,
            "confirmed": True,
            "note": "已确认过",
            "provenance": {"judge_score": 8.4, "target_model": "deepseek-v4-flash"},
        },
    ]
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def _form_rows(path: Path) -> list[dict[str, str]]:
    import csv

    with path.open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def test_make_form_leaves_human_score_empty(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    samples = _candidates_file(tmp_path)
    form = tmp_path / "form.csv"
    assert (
        cli_calibrate._calibrate_command(["--samples", str(samples), "--make-form", str(form)]) == 0
    )
    rows = _form_rows(form)
    assert [r["id"] for r in rows] == ["h-1", "h-2", "h-3"], "分隔条目不该进表"
    # 关键性质：表里的 human_score 只能是空的（或已确认条目的现值），
    # 绝不能预填评委给的分 —— AI 写的"人工分"和被校评委同源
    assert rows[0]["human_score"] == "" and rows[1]["human_score"] == ""
    assert rows[2]["human_score"] == "9.0", "已确认的条目重复生成表时不能丢分"
    assert rows[0]["评委分参考"] == "7.6", "参考分要带上，但只是参考"
    assert "只需填 human_score" in capsys.readouterr().out


def _write_form(path: Path, rows: list[dict[str, str]]) -> Path:
    import csv

    fields = list(cli_calibrate._FORM_COLUMNS)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    return path


def test_apply_scores_writes_only_filled_rows(tmp_path: Path) -> None:
    samples = _candidates_file(tmp_path)
    form = _write_form(
        tmp_path / "filled.csv",
        [
            {"id": "h-1", "human_score": "6.5"},
            {"id": "h-2", "human_score": ""},  # 没填 = 还没判 = 不动
        ],
    )
    assert (
        cli_calibrate._calibrate_command(["--samples", str(samples), "--apply-scores", str(form)])
        == 0
    )
    data = json.loads(samples.read_text(encoding="utf-8"))
    by_id = {d["id"]: d for d in data if isinstance(d, dict) and d.get("id")}
    assert by_id["h-1"]["human_score"] == 6.5 and by_id["h-1"]["confirmed"] is True
    assert by_id["h-2"]["human_score"] is None and by_id["h-2"]["confirmed"] is False
    assert by_id["h-3"]["human_score"] == 9.0, "表里没有的条目不许被动"
    assert data[0]["_comment"], "非条目型记录（_comment）必须原样保留"


def test_apply_scores_is_all_or_nothing_on_bad_rows(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """一行非法就一个字都不写：写一半再让人自己找哪行坏了，比不写更难查。"""
    samples = _candidates_file(tmp_path)
    before = samples.read_text(encoding="utf-8")
    form = _write_form(
        tmp_path / "bad.csv",
        [{"id": "h-1", "human_score": "5"}, {"id": "h-2", "human_score": "11"}],
    )
    assert (
        cli_calibrate._calibrate_command(["--samples", str(samples), "--apply-scores", str(form)])
        == cli_support.EXIT_CONFIG
    )
    assert samples.read_text(encoding="utf-8") == before, "非法行存在时一条都不该写"
    err = capsys.readouterr().err
    assert "超出 1-10" in err and "本次一个字都没写回" in err


def test_apply_scores_rejects_non_numeric_and_duplicates(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    samples = _candidates_file(tmp_path)
    form = _write_form(
        tmp_path / "d.csv",
        [
            {"id": "h-1", "human_score": "很好"},
            {"id": "h-1", "human_score": "7"},
            {"id": "h-1", "human_score": "8"},
        ],
    )
    assert (
        cli_calibrate._calibrate_command(["--samples", str(samples), "--apply-scores", str(form)])
        == cli_support.EXIT_CONFIG
    )
    err = capsys.readouterr().err
    assert "不是数字" in err and "出现两次" in err


def test_apply_scores_needs_filled_rows(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    samples = _candidates_file(tmp_path)
    form = _write_form(tmp_path / "empty.csv", [{"id": "h-1", "human_score": ""}])
    assert (
        cli_calibrate._calibrate_command(["--samples", str(samples), "--apply-scores", str(form)])
        == cli_support.EXIT_CONFIG
    )
    assert "都没填" in capsys.readouterr().err


def test_apply_scores_unknown_ids_only_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    samples = _candidates_file(tmp_path)
    before = samples.read_text(encoding="utf-8")
    form = _write_form(tmp_path / "old.csv", [{"id": "别的表的id", "human_score": "7"}])
    assert (
        cli_calibrate._calibrate_command(["--samples", str(samples), "--apply-scores", str(form)])
        == cli_support.EXIT_CONFIG
    )
    assert samples.read_text(encoding="utf-8") == before
    assert "未写入" in capsys.readouterr().err


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


def test_form_round_trip_survives_commas_and_quotes(tmp_path: Path) -> None:
    """打分表的上下文列里全是逗号/引号/换行（机械线索本来就要举数字）——
    列必须各归各位，两条分数都要落进正确的行。

    写这条用例的直接原因：我用一次性探针做端到端演练时，用 split(',') 拼 CSV，
    结果第二行的分数被塞进了错误的列，回填只写了 1 条，而工具的 csv 解析正确地
    把那条"没填"的行跳过了 —— 探针的错被伪装成工具的行为。所以这条性质要有用例，
    不该靠一次性脚本。
    """
    import csv

    samples = tmp_path / "cand.json"
    samples.write_text(
        json.dumps(
            [
                {
                    "id": "h-c1",
                    "band": "7.0-7.9",
                    "original_task": '需求里有"逗号, 冒号"和换行',
                    "test_output": "输出里 6 个数字无来源：['79.3', '20.7']",
                    "human_score": None,
                    "confirmed": False,
                    "note": '命中线索：含引号 "大概"',
                    "provenance": {"judge_score": 7.6, "target_model": "glm-5.2"},
                },
                {
                    "id": "h-c2",
                    "band": "<6.0",
                    "original_task": "另一个, 带逗号的任务",
                    "test_output": "正文",
                    "human_score": None,
                    "confirmed": False,
                    "note": "无线索",
                    "provenance": {"judge_score": 4.83, "target_model": "glm-5.2"},
                },
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    form = tmp_path / "form.csv"
    assert (
        cli_calibrate._calibrate_command(["--samples", str(samples), "--make-form", str(form)]) == 0
    )
    rows = _form_rows(form)
    assert len(rows) == 2 and rows[0]["human_score"] == ""
    # 上下文列原样可读（被引号包住而不是被拆开）：线索在机械线索、数字在输出摘要
    assert "79.3" in rows[0]["输出摘要"] and "大概" in rows[0]["机械线索"]
    assert "逗号" in rows[0]["原始需求"] and '"' in rows[0]["原始需求"]

    with form.open(encoding="utf-8-sig", newline="") as fh:
        data = list(csv.DictReader(fh))
    for row in data:
        row["human_score"] = "6.5" if row["id"] == "h-c1" else "3.5"
    _write_form(form, data)  # 用 csv 写而不是手拼字符串：这正是探针做错的地方
    assert (
        cli_calibrate._calibrate_command(["--samples", str(samples), "--apply-scores", str(form)])
        == 0
    )

    after = {d["id"]: d for d in json.loads(samples.read_text(encoding="utf-8")) if "id" in d}
    assert after["h-c1"]["human_score"] == 6.5 and after["h-c2"]["human_score"] == 3.5
    assert after["h-c1"]["confirmed"] is True and after["h-c2"]["confirmed"] is True
    assert after["h-c1"]["original_task"] == '需求里有"逗号, 冒号"和换行', "回填不许改上下文字段"
    assert after["h-c1"]["note"] == '命中线索：含引号 "大概"'

    # 已填完再生成一次表：分数要显示出来（幂等，不丢分）
    form2 = tmp_path / "form2.csv"
    assert (
        cli_calibrate._calibrate_command(["--samples", str(samples), "--make-form", str(form2)])
        == 0
    )
    again = _form_rows(form2)
    assert [r["human_score"] for r in again] == ["6.5", "3.5"]


def test_shipped_score_form_is_in_sync_with_the_candidates_file() -> None:
    """仓库里那份待填的  必须与锚点文件同源。

    这条守的是**只有他能做的事**的前置条件：他要照着这张表填 34 个人工分。
    如果哪天重新采集/改写了 candidates，而表没重新生成，他填的就是一张过期表 ——
    轻则 id 对不上回填失败，重则分数落到了另一条锚点上（那比不填更坏）。
    已确认条目（candidates 里有分数的）必须在表里显示同一个分数，防止"填过没回填"和
    "表过期"这两种状态被混成一谈。
    """
    cands = json.loads(
        (ROOT / "judge_calibration" / "samples.candidates.json").read_text(encoding="utf-8")
    )
    items = [c for c in cands if isinstance(c, dict) and c.get("id")]
    pend_ids = [str(c["id"]) for c in items if c.get("confirmed") is False]
    scored = {str(c["id"]): c.get("human_score") for c in items if c.get("human_score") is not None}

    form = _form_rows(ROOT / "judge_calibration" / "score_form.csv")
    assert [r["id"] for r in form] == pend_ids, "打分表与候选文件不同源：重新跑 --make-form"
    assert len(form) == 34, f"待确认候选是 34 条，表里 {len(form)} 条"
    for r in form:
        if r["id"] in scored:
            assert float(r["human_score"]) == float(scored[r["id"]]), (
                f"{r['id']} 表里与锚点文件分数不一致"
            )
        assert r["原始需求"].strip(), f"{r['id']} 没有原始需求，人工没法判"
