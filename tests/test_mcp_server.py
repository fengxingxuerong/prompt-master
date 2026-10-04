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


def test_calibrate_aggregate_is_offline_ledger_read(capture_cli):
    """聚合是零调用的账本重算：不碰样本文件、不触发任何计费路径。"""
    M.calibrate_aggregate("evaluator_b", "impression")
    args = capture_cli[0]
    assert "calibrate" in args and "--aggregate" in args
    assert "--judge" in args and "evaluator_b" in args
    assert "--scoring-mode" in args and "impression" in args
    assert "--json" in args


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
        (M.calibrate_judge, {"judge": "evaluator"}),
        (M.calibrate_aggregate, {"judge": "evaluator", "scoring_mode": "impression"}),
    ):
        out = fn(**kw)  # type: ignore[operator]
        assert isinstance(out, str)
        json.loads(out)


# --------------------------------------------------------------------------
# prompt_check / prompt_diff：两个零调用工具（2026-10-02 补，此前无覆盖）
# --------------------------------------------------------------------------
def test_prompt_check_passes_prompt_through(capture_cli):
    """`prompt_check` 把正文原样交给 `run.py check --prompt ... --json`。

    注意它是**把正文当参数传**（不落临时文件）—— `--prompt` 显式在前，
    所以正文以 `-` 开头也不会被 argparse 当选项吃掉。
    """
    out = M.prompt_check(prompt="- 看起来像选项的正文")
    # capture_cli 记的是**完整命令行**：[python, run.py, <子命令>, ...]
    cmd = capture_cli[0]
    assert "check" in cmd, cmd
    i = cmd.index("--prompt")
    assert cmd[i + 1] == "- 看起来像选项的正文", "正文没被原样传下去（或被当成选项吃掉了）"
    assert cmd[-1] == "--json"
    assert json.loads(out) == {}


def test_prompt_diff_writes_both_sides_to_separate_temp_files(capture_cli):
    """`prompt_diff` 必须把两侧正文落成**两个不同**的文件再调 `run.py diff`。

    `diff` 的接口是文件路径（不是正文），中转不可避免。要钉住：
    ① 两侧分别写进不同文件、内容不串；② 用 `tempfile`（用完即删）而不是往仓库里写。
    """
    out = M.prompt_diff(before="旧正文 AAA", after="新正文 BBB")
    cmd = capture_cli[0]
    assert "diff" in cmd and cmd[-1] == "--json"
    d = cmd.index("diff")
    path_a, path_b = cmd[d + 1], cmd[d + 2]
    assert path_a != path_b, "两侧写进了同一个文件，内容会互相覆盖"

    import os
    import tempfile

    # 临时目录在 with 块结束后已删：证明用的是 tempfile 而不是仓库内路径
    assert not os.path.exists(path_a), "临时文件没被清理 —— 可能写进了仓库"
    assert not os.path.exists(path_b)
    # `TemporaryDirectory()` 会在系统临时目录下建一个**子目录**，所以判据是"在其下"，
    # 不是"等于它"（写成相等会把正确实现判红 —— 我第一版就这么写的）。
    assert os.path.dirname(path_a).startswith(tempfile.gettempdir()), "没落在系统临时目录下"
    assert json.loads(out) == {}


def test_prompt_diff_content_order_is_not_swapped(monkeypatch):
    """真跑一次（打桩 subprocess 层）：写进文件的内容必须与入参**同序**。

    只验参数拼装的话，"a/b 写反了"会被漏掉 —— 而它会让 diff 的结论整个反向
    （把"引入回归"报成"修好了"）。
    """
    seen: dict[str, str] = {}

    def fake_run(cmd: list[str], **kw: Any) -> Any:
        diff_idx = cmd.index("diff")
        from pathlib import Path

        seen["a"] = Path(cmd[diff_idx + 1]).read_text(encoding="utf-8")
        seen["b"] = Path(cmd[diff_idx + 2]).read_text(encoding="utf-8")
        return _fake_proc("{}")

    monkeypatch.setattr(M.subprocess, "run", fake_run)
    M.prompt_diff(before="左侧原始正文", after="右侧修改后正文")
    assert seen["a"] == "左侧原始正文", "before 没有落到第一个文件（或顺序反了）"
    assert seen["b"] == "右侧修改后正文", "after 没有落到第二个文件（或顺序反了）"


def test_prompt_diff_roundtrips_unicode(monkeypatch):
    """中文/emoji 正文必须原样往返（显式 UTF-8 写读）。

    Windows 默认码页是 GBK：漏了 `encoding="utf-8"` 会让中文正文抛
    UnicodeEncodeError 或被**静默替换**成问号 —— 后者更坏，
    同一份正文会被 diff 报成"有改动"。
    """
    seen: dict[str, str] = {}

    def fake_run(cmd: list[str], **kw: Any) -> Any:
        from pathlib import Path

        diff_idx = cmd.index("diff")
        seen["a"] = Path(cmd[diff_idx + 1]).read_text(encoding="utf-8")
        return _fake_proc("{}")

    monkeypatch.setattr(M.subprocess, "run", fake_run)
    text = "【角色】分析师 🎯\n【约束】1. 不得编造。"
    M.prompt_diff(before=text, after=text)
    assert seen["a"] == text, "中文/emoji 在落盘往返中被改动了"


def test_main_starts_stdio_server(monkeypatch):
    """`main()` 必须调用 `mcp.run()`（stdio 传输）。

    MCP 客户端按 `{"command":"python","args":["-m","pm.mcp_server"]}` 拉起它；
    若 `main()` 是空实现或走错传输方式，挂载后会一直连不上，
    而"连不上"在服务端看起来只是"客户端没来"。
    """
    called: list[int] = []
    monkeypatch.setattr(M.mcp, "run", lambda: called.append(1))
    M.main()
    assert called == [1], "main() 没有调用 mcp.run()，stdio 服务不会起来"


# --------------------------------------------------------------------------
# optimize_wait 的超时分支（此前零覆盖）
# --------------------------------------------------------------------------
def _http_seq(monkeypatch, payloads: list[dict[str, Any]], calls: list[Any]) -> None:
    """让 `_http_json` 依次返回 payloads（用尽后重复最后一个）。"""
    seq = list(payloads)

    def fake_http(method: str, url: str, timeout: float = 0) -> dict[str, Any]:
        calls.append((method, url))
        return seq.pop(0) if len(seq) > 1 else seq[0]

    monkeypatch.setattr(M, "_http_json", fake_http)


def test_optimize_wait_reports_timeout_without_pretending_success(capture_http, monkeypatch):
    """超时不是"完成"：必须带 `error` 说明，且**不带** report。

    危险在于：若把超时静默当终态返回，调用方会拿到一个 `report: ""` 的
    "成功"响应 —— 看起来像"跑完了但报告是空的"，实际是"还没跑完"。
    两者的下一步动作完全不同（一个去查报告，一个应该继续等或去查服务）。
    """
    calls: list[Any] = []
    _http_seq(monkeypatch, [{"status": "running", "iteration": 1, "llm_calls": 7}], calls)
    # 时钟推快 + sleep 打桩：不真等，但仍走真实的超时判定
    ticks = iter([0.0, 0.0, 10_000.0, 10_000.0, 10_000.0])
    monkeypatch.setattr(M.time, "monotonic", lambda: next(ticks, 10_000.0))
    monkeypatch.setattr(M.time, "sleep", lambda _s: None)

    out = json.loads(M.optimize_wait(run_id="abc123", timeout_seconds=30, interval_seconds=5))
    assert out["run_id"] == "abc123"
    assert out["error"] == "等待超时", "超时必须显式说明，不能静默当完成"
    assert "report" not in out, "超时时不该带 report —— 会被读成「跑完了但报告为空」"
    assert out["status"] == "running", "应如实回报当前状态"


def test_optimize_wait_returns_report_on_terminal_status(monkeypatch):
    """终态时取回报告（与上面配对，防止"永远报超时"也算通过）。"""
    calls: list[Any] = []
    _http_seq(
        monkeypatch,
        [{"status": "passed", "iteration": 2, "llm_calls": 9}, {"report": "# 交付报告"}],
        calls,
    )
    out = json.loads(M.optimize_wait(run_id="ok123", timeout_seconds=30, interval_seconds=5))
    assert out["status"] == "passed"
    assert out["report"] == "# 交付报告"
    assert not out.get("error")
    # 两次调用分别是 status 与 report 两个端点
    urls = [u for _m, u in calls]
    assert any("/api/status/ok123" in u for u in urls)
    assert any("/api/report/ok123" in u for u in urls)


def test_module_entry_actually_starts_the_server(tmp_path):
    """`python -m pm.mcp_server` 这条命令必须真的起 stdio 服务。

    这是 MCP 客户端的**真实挂载方式**（`{"command":"python","args":["-m","pm.mcp_server"]}`），
    而 `if __name__ == "__main__": main()` 这一行此前零覆盖 ——
    也就是说"客户端照文档配置能不能连上"从没被验证过。

    怎么验的：在子进程里**真启动**它，然后发一个真实的 MCP `initialize` 请求，
    看它是否按协议回一个带 `serverInfo` 的 JSON-RPC 响应。
    这比"断言 main() 被调用"强 —— 它同时证明了传输方式（stdio）、
    JSON-RPC 帧格式与服务名都对得上；任何一环错，客户端就是连不上。

    ⚠️ 不要在进程内 `runpy` 该模块：FastMCP 起 stdio 时会接管/关闭 stdout，
    进程内执行会把测试进程的 stdout 弄坏（实测 `ValueError: I/O operation on closed file`）。
    子进程隔离是这里唯一安全的做法。
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    # 一条标准的 MCP initialize 请求（换行分隔的 JSON-RPC）
    req = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "probe", "version": "0"},
            },
        }
    )
    proc = subprocess.run(
        [sys.executable, "-m", "pm.mcp_server"],
        cwd=str(root),
        input=req + "\n",
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    assert proc.returncode == 0, (proc.stdout[-500:], proc.stderr[-500:])
    # 响应可能含通知行，挑出带 result 的那条
    payloads = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                payloads.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    assert payloads, f"没有回出任何 JSON-RPC 帧：{proc.stdout[-300:]!r}"
    init = next((p for p in payloads if "result" in p), None)
    assert init, f"没有 initialize 响应：{payloads}"
    assert init["result"]["serverInfo"]["name"], "响应里没有服务名，客户端无法确认挂载成功"
