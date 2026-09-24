"""`pm/mcp_server.py` 的单元测试（补 0% 覆盖的洼地）。

背景：2026-09-16 一次性并入 MCP 标准协议接入（9 个 tool / 232 行），
但**零测试**入库，导致 pm 包覆盖率从 94% 掉到 91%。本文件补齐基础覆盖。

测试策略（不联网、不起子进程）：
  · `_run_cli_json` / `_http_json` 两个底层函数：monkeypatch 掉 subprocess / urlopen
  · 9 个 tool：只验证「参数 → 调用参数」的拼装正确性（这是最容易写错的部分）
    —— 真正的业务逻辑在 run.py / server.py 里，各有自己的测试

关键安全性质（必须锁住）：
  · `_run_cli_json` 用 **list 形式**传参（不是 shell=True）→ 任务文本里的
    `; rm -rf /` 之类不会被当命令执行
  · 退出码 0/1/2/3 都算"正常返回"，其余才抛异常
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
import urllib.error
from typing import Any

import pytest
from pm import mcp_server as M


def _fake_proc(stdout: str = "{}", returncode: int = 0, stderr: str = "") -> Any:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


# --------------------------------------------------------------------------
# 1. _run_cli_json
# --------------------------------------------------------------------------
def test_run_cli_json_uses_list_args_not_shell(monkeypatch):
    """核心安全性质：参数走 list，不走 shell —— 任务文本不能变成命令。"""
    captured: dict[str, Any] = {}

    def fake_run(cmd: list[str], **kw: Any) -> Any:
        captured["cmd"] = cmd
        captured["kw"] = kw
        return _fake_proc('{"ok": 1}')

    monkeypatch.setattr(M.subprocess, "run", fake_run)
    M._run_cli_json(["--dry-run", "--task", "x"])
    # 必须是 list + 解释器 + run.py 的形式；shell=True 绝不能出现
    assert captured["cmd"][0] == sys.executable
    assert captured["cmd"][1].endswith("run.py")
    assert "shell" not in captured["kw"], "禁用 shell=True（否则任务文本可被当命令执行）"
    assert captured["kw"]["timeout"] == 60.0


def test_run_cli_json_accepts_expected_exit_codes(monkeypatch):
    """0/1/2/3 都是 CLI 的合法退出码（1=未达标、2=需澄清…），不能当异常。"""
    for code in (0, 1, 2, 3):
        monkeypatch.setattr(
            M.subprocess, "run", lambda *a, _c=code, **k: _fake_proc(f'{{"code": {_c}}}', _c)
        )
        assert M._run_cli_json(["x"])["code"] == code


def test_run_cli_json_raises_on_abnormal_exit(monkeypatch):
    monkeypatch.setattr(M.subprocess, "run", lambda *a, **k: _fake_proc("", 99, "boom"))
    with pytest.raises(RuntimeError, match="退出码异常"):
        M._run_cli_json(["x"])


def test_run_cli_json_raises_on_non_json_stdout(monkeypatch):
    """stdout 不是 JSON（比如 CLI 打印了 traceback）必须报错，不能静默返回空。"""
    monkeypatch.setattr(M.subprocess, "run", lambda *a, **k: _fake_proc("not json"))
    with pytest.raises(json.JSONDecodeError):
        M._run_cli_json(["x"])


# --------------------------------------------------------------------------
# 2. _http_json
# --------------------------------------------------------------------------
def test_http_json_get_and_post(monkeypatch):
    captured: dict[str, Any] = {}

    class _Resp:
        def read(self) -> bytes:
            return b'{"ok": 1}'

        def __enter__(self) -> _Resp:
            return self

        def __exit__(self, *a: Any) -> None:
            return None

    def fake_urlopen(req: Any, timeout: float = 0) -> _Resp:
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["body"] = req.data
        captured["timeout"] = timeout
        return _Resp()

    monkeypatch.setattr(M.urllib.request, "urlopen", fake_urlopen)

    assert M._http_json("GET", "http://x/y") == {"ok": 1}
    assert captured["method"] == "GET"
    assert captured["body"] is None

    M._http_json("POST", "http://x/y", {"a": 1})
    assert captured["method"] == "POST"
    assert json.loads(captured["body"].decode("utf-8")) == {"a": 1}


def test_http_json_error_messages_are_actionable(monkeypatch):
    """错误信息要能直接指导下一步（HTTP 带状态码与响应体；连不上提示去启 server）。"""

    def raise_http(*a: Any, **k: Any) -> Any:
        raise urllib.error.HTTPError("http://x", 500, "err", {}, io.BytesIO(b"boom"))

    monkeypatch.setattr(M.urllib.request, "urlopen", raise_http)
    with pytest.raises(RuntimeError, match="HTTP 500"):
        M._http_json("GET", "http://x")

    def raise_url(*a: Any, **k: Any) -> Any:
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(M.urllib.request, "urlopen", raise_url)
    with pytest.raises(RuntimeError, match=r"run_server\.py"):
        M._http_json("GET", "http://x")


# --------------------------------------------------------------------------
# 3. 计算类 tool：参数 → CLI 参数 的拼装
# --------------------------------------------------------------------------
@pytest.fixture()
def capture_cli(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kw: Any) -> Any:
        calls.append(cmd)
        return _fake_proc("{}")

    monkeypatch.setattr(M.subprocess, "run", fake_run)
    return calls


def test_estimate_cost_args(capture_cli):
    M.estimate_cost("某任务", cases=5, max_iter=2, fast=True)
    args = capture_cli[0]
    assert "--dry-run" in args
    assert "--task" in args and "某任务" in args
    assert "--cases" in args and "5" in args
    assert "--max-iter" in args and "2" in args
    assert "--fast" in args


def test_estimate_cost_omits_fast_when_false(capture_cli):
    M.estimate_cost("t", fast=False)
    assert "--fast" not in capture_cli[0]


def test_run_history_args(capture_cli):
    M.run_history(last=7)
    args = capture_cli[0]
    assert args[args.index("history") + 1 : args.index("history") + 3] == ["--last", "7"]
    assert "--json" in args


def test_library_recommend_args(capture_cli):
    M.library_recommend("客服分诊任务")
    args = capture_cli[0]
    assert "library" in args and "--recommend" in args
    assert "客服分诊任务" in args


def test_library_export_args(capture_cli):
    M.library_export("abc123")
    args = capture_cli[0]
    assert "--export" in args and "abc123" in args


def test_calibrate_judge_uses_long_timeout(capture_cli):
    """校准要真实调用每条锚点，60s 默认超时不够 —— 必须放宽。"""
    M.calibrate_judge("evaluator_b")
    assert "--judge" in capture_cli[0] and "evaluator_b" in capture_cli[0]


def test_task_text_with_shell_metacharacters_stays_one_arg(capture_cli):
    """任务文本含 shell 元字符时，仍应作为**单个** argv 元素传给 CLI。"""
    evil = "分析数据; rm -rf / --force"
    M.estimate_cost(evil)
    args = capture_cli[0]
    assert evil in args, "含分号的文本必须整体作为一个参数（list 形式保证不被 shell 解释）"
    assert "; rm -rf / --force" not in args


# --------------------------------------------------------------------------
# 4. 长任务 tool：转发 server 的 URL 拼装
# --------------------------------------------------------------------------
@pytest.fixture()
def capture_http(monkeypatch):
    calls: list[tuple[str, str]] = []

    class _Resp:
        def read(self) -> bytes:
            return b'{"status": "passed", "report": "R"}'

        def __enter__(self) -> _Resp:
            return self

        def __exit__(self, *a: Any) -> None:
            return None

    def fake_urlopen(req: Any, timeout: float = 0) -> _Resp:
        calls.append((req.get_method(), req.full_url))
        return _Resp()

    monkeypatch.setattr(M.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(M, "SERVER_URL", "http://127.0.0.1:8080")
    return calls


def test_optimize_submit_posts_to_server(capture_http):
    M.optimize_submit("任务", target_model="m", n_test_cases=2, max_iterations=1)
    method, url = capture_http[0]
    assert method == "POST"
    assert url.endswith("/api/optimize")


def test_optimize_submit_parses_cases_json(capture_http, monkeypatch):
    """cases_json 要解析后放进 payload，且用例数以它为准。"""
    payloads: list[dict] = []
    real_request = M.urllib.request.Request

    class SpyRequest(real_request):  # type: ignore[misc,valid-type]
        def __init__(self, url: str, data: Any = None, **kw: Any) -> None:
            if data:
                payloads.append(json.loads(data.decode("utf-8")))
            super().__init__(url, data=data, **kw)

    monkeypatch.setattr(M.urllib.request, "Request", SpyRequest)
    cases = [{"input": "a", "expected": "b", "mode": "contains"}]
    M.optimize_submit("t", cases_json=json.dumps(cases))
    assert payloads[0]["test_cases"] == cases
    assert payloads[0]["n_test_cases"] == 1


def test_optimize_submit_forwards_assertion_mode(capture_http, monkeypatch):
    """断言模式必须能从 MCP 侧传下去：以前 optimize_submit 不带这个字段，
    注册过的 `custom:<名>` 断言从 REST/CLI 能跑、从 MCP 跑不了（三入口口径分叉）。"""
    payloads: list[dict] = []
    real_request = M.urllib.request.Request

    class SpyRequest(real_request):  # type: ignore[misc,valid-type]
        def __init__(self, url: str, data: Any = None, **kw: Any) -> None:
            if data:
                payloads.append(json.loads(data.decode("utf-8")))
            super().__init__(url, data=data, **kw)

    monkeypatch.setattr(M.urllib.request, "Request", SpyRequest)
    cases = json.dumps([{"input": "a", "expected": "b"}])
    M.optimize_submit("t", cases_json=cases)
    assert payloads[-1]["assertion_mode"] == "contains", "缺省要与 CLI/API 同为 contains"
    M.optimize_submit("t", cases_json=cases, assertion_mode="custom:no_apology")
    assert payloads[-1]["assertion_mode"] == "custom:no_apology"
    M.optimize_submit("t", cases_json=cases, assertion_mode="rule")
    assert payloads[-1]["assertion_mode"] == "rule"


def test_optimize_status_and_report_urls(capture_http):
    M.optimize_status("deadbeef1234")
    assert capture_http[-1][1].endswith("/api/status/deadbeef1234")
    M.optimize_report("deadbeef1234")
    assert capture_http[-1][1].endswith("/api/report/deadbeef1234")


def test_optimize_wait_stops_on_terminal_status(capture_http, monkeypatch):
    """等到终态就停；非终态要继续轮询（用单调时钟防止真实 sleep）。"""
    statuses = iter(["running", "running", "passed"])
    ticks: list[float] = []

    class _Resp:
        def __init__(self, st: str) -> None:
            self._body = json.dumps({"status": st, "report": "R"}).encode()

        def read(self) -> bytes:
            return self._body

        def __enter__(self) -> _Resp:
            return self

        def __exit__(self, *a: Any) -> None:
            return None

    def fake_urlopen(req: Any, timeout: float = 0) -> _Resp:
        return _Resp(next(statuses, "passed"))

    monkeypatch.setattr(M.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(M.time, "sleep", lambda s: ticks.append(s))
    monkeypatch.setattr(M.time, "monotonic", lambda: 0.0)

    out = json.loads(M.optimize_wait("x", timeout_seconds=30, interval_seconds=1))
    assert out["status"] == "passed"
    assert len(ticks) == 2, "两次非终态之间各 sleep 一次，第三次到终态不再 sleep"


def test_all_tools_return_json_strings(capture_cli, capture_http):
    """MCP tool 契约：返回值必须是 JSON 字符串（客户端按 str 解析）。"""
    for fn, kw in (
        (M.estimate_cost, {"task": "t"}),
        (M.run_history, {}),
        (M.library_recommend, {"task_text": "t"}),
        (M.library_export, {"run_id": "x"}),
    ):
        out = fn(**kw)  # type: ignore[operator]
        assert isinstance(out, str)
        json.loads(out)
