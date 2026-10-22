"""桌面端回归：渲染纯函数 + 真 Tk 上的单页流程。

窗口层不调用真实模型：所有 `run_agent_request` 都由用例替换成固定 JSON。
真子进程那条路由 `test_run_agent_request_*` 用临时脚本单独验证。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import tkinter as tk
from pathlib import Path
from typing import Any

import pytest
from extras import desktop

REPO = Path(__file__).resolve().parents[1]

PROJECT = {
    "root_path": r"D:\demo",
    "root_name": "demo",
    "file_count": 4,
    "dir_count": 2,
    "by_ext": {".py": 2, ".md": 1, ".json": 1},
    "tree": ["README.md", "src/", "src/app.py", "docs/", "docs/plan.md"],
    "key_files": [{"path": "README.md", "reason": "项目说明", "chars": 20}],
    "git": {
        "branch": "main",
        "recent_commits": ["abc123 2026-10-20 完成登录"],
        "status": [" M src/app.py"],
        "diffstat": [],
    },
    "todos": [{"path": "src/app.py", "line": "1", "text": "TODO: add auth"}],
    "tool_calls": [{"tool": "list_files", "detail": "列出 4 个文件", "count": 4}],
    "skipped_secrets": 1,
    "truncated": False,
    "warnings": [],
}

SCAN_PAYLOAD = {"mode": "project_scan", "ok": True, "project": PROJECT}

OPTIMIZE_PAYLOAD = {
    "mode": "project_optimize",
    "ok": True,
    "project": PROJECT,
    "analysis": {
        "project_summary": "项目已完成登录，支付尚未开始。",
        "evidence": ["src/app.py：TODO add auth"],
        "prompt_gaps": ["缺少输出格式", "信息不足时没有处理规则"],
        "assumptions": ["支付渠道未指定"],
    },
    "optimized_prompt": "[角色] 你是项目维护者。\n[任务] 实现支付。\n[约束]\n1. 只改支付相关文件。",
    "checks": {
        "ok": False,
        "findings": [{"code": "too_short", "detail": "提示词过短（28 字符 < 100）"}],
    },
    "usage": {"role": "optimizer", "model": "fake", "channel": "fake", "requests": 1},
}


def _text(widget: tk.Text) -> str:
    return str(widget.get("1.0", "end")).strip()


# ---------------------------------------------------------------------------
# 1. 纯函数
# ---------------------------------------------------------------------------
def test_run_available_reads_env_at_call_time(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("PM_API_KEY", "PM_API_KEYS", "PM_OPTIMIZER_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    assert desktop.run_available() is False
    monkeypatch.setenv("PM_API_KEY", "sk-not-real")
    assert desktop.run_available() is True
    monkeypatch.delenv("PM_API_KEY")
    monkeypatch.setenv("PM_API_KEYS", "one,two")
    assert desktop.run_available() is True
    monkeypatch.delenv("PM_API_KEYS")
    monkeypatch.setenv("PM_OPTIMIZER_API_KEY", "sk-role")
    assert desktop.run_available() is True


def test_build_request_requires_inputs_only_when_generating(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        desktop.build_request(project_dir=str(tmp_path))
    request = desktop.build_request(project_dir=str(tmp_path), scan_only=True)
    assert request["scan_only"] is True
    assert request["prompt"] == ""
    request = desktop.build_request(prompt="继续开发", goal="实现支付")
    assert request["project_dir"] == "" and request["scan_only"] is False
    with pytest.raises(ValueError):
        desktop.build_request(project_dir=str(tmp_path / "missing"), scan_only=True)


def test_render_scan_lists_progress_evidence() -> None:
    out = "\n".join(desktop.render_scan(PROJECT))
    assert "读取 4 个文件" in out
    assert "Git：main" in out
    assert "README.md" in out
    assert "TODO: add auth" in out
    assert "已跳过 1 个疑似密钥文件" in out


def test_render_analysis_keeps_the_honest_boundary() -> None:
    out = "\n".join(desktop.render_analysis(OPTIMIZE_PAYLOAD))
    assert "项目：demo" in out
    assert "项目已完成登录" in out
    assert "缺少输出格式" in out
    assert "结构体检：命中 1 条" in out
    assert "模型调用：fake · 请求 1 次" in out
    assert "README.md" in out
    assert desktop.HONESTY_NOTE in out, "不能把模型改写说成已证实更好"


def test_render_checks_clean_prompt_does_not_overclaim() -> None:
    out = "\n".join(desktop.render_checks({"ok": True, "findings": []}))
    assert "未命中已知规则" in out
    assert "不证明任务能完成" in out


def test_prompt_of_strips_whitespace() -> None:
    assert desktop.prompt_of({"optimized_prompt": "  正文  \n"}) == "正文"
    assert desktop.prompt_of({}) == ""


def test_format_elapsed_for_long_real_runs() -> None:
    assert desktop._format_elapsed(0) == "0:00"
    assert desktop._format_elapsed(443.6) == "7:23"
    assert desktop._format_elapsed(-1) == "0:00"


def test_extract_json_ignores_log_lines() -> None:
    payload = desktop._extract_json('日志一行\n{"ok": true, "mode": "project_scan"}\n')
    assert payload["mode"] == "project_scan"
    with pytest.raises(desktop.AgentRunError):
        desktop._extract_json("not json")


# ---------------------------------------------------------------------------
# 2. 子进程边界（用临时脚本，不联网、不碰真模型）
# ---------------------------------------------------------------------------
def test_run_agent_request_roundtrip(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    script = tmp_path / "fake_agent.py"
    script.write_text(
        "import json\nprint(json.dumps({'ok': True, 'mode': 'project_scan', 'project': None}))\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(desktop, "AGENT_SCRIPT", script)
    seen: list[Any] = []
    payload = desktop.run_agent_request({"scan_only": True}, seen.append)
    assert payload["ok"] is True
    assert seen[0] is not None and seen[-1] is None, "子进程句柄必须登记并在结束后清空"


def test_run_agent_request_reports_child_stderr(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    script = tmp_path / "bad_agent.py"
    script.write_text(
        "import sys\nsys.stderr.write('boom sk-abcdefghijklmnopqrstuvwxyz')\nsys.exit(3)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(desktop, "AGENT_SCRIPT", script)
    with pytest.raises(desktop.AgentRunError) as caught:
        desktop.run_agent_request({}, lambda _proc: None)
    assert "boom" in str(caught.value)
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in str(caught.value)


# ---------------------------------------------------------------------------
# 3. 真 Tk 窗口
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def tk_root() -> Any:
    tk_module = pytest.importorskip("tkinter")
    try:
        root = tk_module.Tk()
    except Exception as exc:  # noqa: BLE001 - 无显示器时整层跳过
        pytest.skip(f"这台机器开不出 Tk 窗口：{exc}")
    root.withdraw()
    yield root
    # 不销毁 Tcl 解释器：Windows 上整套用例共享一个根时销毁会触发 Tcl 终结器崩溃。


@pytest.fixture()
def app(tk_root: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("PM_API_KEY", "sk-test-only")

    def blocked(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("窗口用例走到了真实的子进程分析路径")

    monkeypatch.setattr(desktop, "run_agent_request", blocked)
    window = tk.Toplevel(tk_root)
    window.withdraw()
    built = desktop.App(window)
    yield built
    built._alive = False
    try:
        window.destroy()
    except tk.TclError:
        pass


def _pump(root: Any, app: desktop.App, timeout: float = 8.0) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        root.update()
        if app.inflight == 0 and app.q.empty():
            for _ in range(3):
                root.update()
                time.sleep(0.01)
            return
        time.sleep(0.01)
    raise AssertionError(f"窗口动作超时：inflight={app.inflight} q={app.q.qsize()}")


def test_window_scan_flow_shows_project_progress(
    app: desktop.App, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: list[dict[str, Any]] = []

    def fake(request: dict[str, Any], register: Any) -> dict[str, Any]:
        seen.append(request)
        register(None)
        return SCAN_PAYLOAD

    monkeypatch.setattr(desktop, "run_agent_request", fake)
    app.in_project.insert(0, str(tmp_path))
    app.in_prompt.insert("1.0", "帮我继续开发这个项目")
    app.act_scan()
    _pump(app.root, app)

    assert seen and seen[0]["scan_only"] is True
    out = _text(app.out_analysis)
    assert "读取 4 个文件" in out and "README.md" in out
    assert _text(app.out_prompt) == ""
    assert app.prompt_card.grid_info() == {}, "只读项目时别让空的提示词卡占着结果区"
    assert "完成" in str(app.status_lbl.cget("text"))


def test_window_generate_flow_fills_the_result(
    app: desktop.App, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fake(_request: dict[str, Any], register: Any) -> dict[str, Any]:
        register(None)
        return OPTIMIZE_PAYLOAD

    monkeypatch.setattr(desktop, "run_agent_request", fake)
    app.in_project.insert(0, str(tmp_path))
    app.in_prompt.insert("1.0", "帮我继续开发这个项目")
    app.in_goal.insert(0, "实现支付")
    app.act_generate()
    _pump(app.root, app)

    assert _text(app.out_prompt) == OPTIMIZE_PAYLOAD["optimized_prompt"]
    assert app.prompt_card.grid_info() != {}, "生成结果必须把提示词卡放回来"
    out = _text(app.out_analysis)
    assert "项目已完成登录" in out
    assert "结构体检：命中 1 条" in out
    assert "完成" in str(app.status_lbl.cget("text"))


def test_generate_without_key_is_refused_before_any_call(
    app: desktop.App, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("PM_API_KEY", "PM_API_KEYS", "PM_OPTIMIZER_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    app.in_prompt.insert("1.0", "继续开发")
    app.act_generate()
    assert app.inflight == 0
    assert "没有检测到 API Key" in str(app.status_lbl.cget("text"))


def test_empty_inputs_are_rejected(app: desktop.App) -> None:
    app.act_generate()
    assert app.inflight == 0
    assert "至少填一个" in str(app.status_lbl.cget("text"))


def test_worker_error_is_rendered_instead_of_hanging(
    app: desktop.App, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake(_request: dict[str, Any], _register: Any) -> dict[str, Any]:
        raise desktop.AgentRunError("模型调用失败：boom")

    monkeypatch.setattr(desktop, "run_agent_request", fake)
    app.in_prompt.insert("1.0", "继续开发")
    app.act_generate()
    _pump(app.root, app)

    out = _text(app.out_analysis)
    assert "这次没有结果" in out and "boom" in out
    assert desktop.HONESTY_NOTE not in out, (
        "失败页不许贴交付物的口径说明：那句会被读成“至少还给了提示词”"
    )
    assert app.inflight == 0
    assert "失败" in str(app.status_lbl.cget("text"))


def test_cancel_terminates_the_registered_child(app: desktop.App) -> None:
    class FakeProc:
        terminated = False

        def terminate(self) -> None:
            self.terminated = True

    proc = FakeProc()
    app.proc = proc  # type: ignore[assignment]
    app.inflight = 1
    app.act_cancel()
    assert proc.terminated is True
    assert "取消" in str(app.status_lbl.cget("text"))
    app.inflight = 0


def test_cancel_wording_follows_what_the_round_actually_did(app: desktop.App) -> None:
    """取消文案不许撒谎：只读取项目没有模型调用，就不能说“已发出的调用不退还”。

    真窗手测抓到：扫大仓库时点取消，面板仍写“已经发出的模型调用不退还”——
    对一次零调用的扫描来说，这句话是假的。
    """
    app.inflight = 1
    app.q.put(("aborted", True))  # scan_only=True
    app._drain()
    scan_note = _text(app.out_analysis)
    assert "不调用模型" in scan_note, scan_note
    assert "不退还" not in scan_note, scan_note

    app.inflight = 1
    app.q.put(("aborted", False))  # 生成轮
    app._drain()
    gen_note = _text(app.out_analysis)
    assert "不退还" in gen_note, gen_note


def test_scan_refuses_a_second_round_while_busy(app: desktop.App, tmp_path: Path) -> None:
    """一轮在跑时再点「只读取项目」必须被挡：两个子进程会抢同一个 self.proc。"""
    app.in_project.delete(0, "end")
    app.in_project.insert(0, str(tmp_path))
    app.inflight = 1
    app.act_scan()
    assert app.inflight == 1, "busy 时又起了一轮"
    assert "已经有一轮在跑" in str(app.status_lbl.cget("text"))
    app.inflight = 0


def test_refusal_clears_the_previous_reading(app: desktop.App, tmp_path: Path) -> None:
    """本地拒绝（目录不存在 / 没 Key / 空输入）不许留着上一轮读数。

    真窗手测抓到：目录被删后点「只读取项目」，状态行报了「文件夹不存在」，
    而结果区仍是上一轮的项目进度 —— 用户会把它读成本次结果。
    """
    app._set_analysis(["上一轮的项目进度：读取 3 个文件"])
    app.in_project.delete(0, "end")
    app.in_project.insert(0, str(tmp_path / "nope"))
    app.act_scan()
    shown = _text(app.out_analysis)
    assert "不存在" in shown, shown
    assert "上一轮的项目进度" not in shown, shown
    assert "已清空" in shown, shown


def test_normalize_project_path_handles_quotes_uri_and_spaces(tmp_path: Path) -> None:
    """粘贴进来的路径要先收拾：资源管理器带引号、浏览器给 file:///、手抄带空格。

    不收拾的话，用户会看到“文件夹不存在”——而他明明是把资源管理器给的地址原样贴上的。
    """
    folder = tmp_path / "项目 一"
    folder.mkdir()
    for raw in (
        str(folder),
        f'"{folder}"',
        f"“{folder}”",
        f"  {folder}  ",
        f"file:///{str(folder).replace(chr(92), '/')}",
    ):
        assert desktop.normalize_project_path(raw)[0] == str(folder), raw
    assert desktop.normalize_project_path("")[0] == ""
    assert desktop.normalize_project_path('""')[0] == ""


def test_normalize_project_path_maps_a_file_to_its_folder(tmp_path: Path) -> None:
    """资源管理器的「复制文件地址」常给的是文件：自动改用所在文件夹，并说明一句。"""
    file = tmp_path / "src.py"
    file.write_text("x", encoding="utf-8")
    path, note = desktop.normalize_project_path(f'"{file}"')
    assert path == str(tmp_path)
    assert "文件" in note and "文件夹" in note


def test_paste_fills_the_path_and_validates_it(
    app: desktop.App, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """「粘贴路径」要把带引号的地址填进去，并当场给出 ✓ 读数。"""
    monkeypatch.setattr(app.root, "clipboard_get", lambda: f'"{tmp_path}"', raising=False)
    app.act_paste_project()
    assert app.in_project.get() == str(tmp_path)
    assert "✓" in str(app.project_hint.cget("text"))
    assert "已选择" in str(app.status_lbl.cget("text"))


def test_paste_with_empty_clipboard_says_so(
    app: desktop.App, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom() -> str:
        raise tk.TclError("CLIPBOARD selection doesn't exist")

    monkeypatch.setattr(app.root, "clipboard_get", boom, raising=False)
    app.act_paste_project()
    assert "剪贴板" in str(app.status_lbl.cget("text"))


def test_project_hint_flags_a_missing_path(app: desktop.App, tmp_path: Path) -> None:
    app.in_project.delete(0, "end")
    app.in_project.insert(0, str(tmp_path / "nope"))
    app._sync_project_hint()
    assert "✗" in str(app.project_hint.cget("text"))


def test_scan_uses_the_pasted_path(
    app: desktop.App, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """粘贴的路径必须原样进请求 —— 否则“看起来填上了，实际读的是旧目录”。"""
    seen: list[dict[str, Any]] = []

    def fake(request: dict[str, Any], register: Any) -> dict[str, Any]:
        seen.append(request)
        register(None)
        return SCAN_PAYLOAD

    monkeypatch.setattr(desktop, "run_agent_request", fake)
    monkeypatch.setattr(app.root, "clipboard_get", lambda: f'"{tmp_path}"', raising=False)
    app.act_paste_project()
    app.act_scan()
    _pump(app.root, app)
    assert seen and seen[0]["project_dir"] == str(tmp_path), seen
    assert seen[0]["scan_only"] is True


def test_project_entry_enter_is_wired_to_the_free_scan(app: desktop.App) -> None:
    """回车只能触发免费动作：绑到生成上意味着一次误按就花钱。"""
    assert app.in_project.bind("<Return>"), "路径框回车没有绑定"


def test_copy_and_save_take_the_prompt(
    app: desktop.App, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    app.out_prompt.insert("1.0", "最终提示词")
    app.act_copy()
    try:
        assert app.root.clipboard_get() == "最终提示词"
    except tk.TclError:
        pass  # 无剪贴板的后端不阻断其余断言
    target = tmp_path / "prompt.md"
    monkeypatch.setattr(desktop.filedialog, "asksaveasfilename", lambda **_kwargs: str(target))
    app.act_save()
    assert target.read_text(encoding="utf-8") == "最终提示词\n"


def test_preload_fills_prompt_and_project(
    tk_root: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PM_API_KEY", "sk-test-only")
    prompt_file = tmp_path / "seed.md"
    prompt_file.write_text("预填的提示词", encoding="utf-8")
    window = tk.Toplevel(tk_root)
    window.withdraw()
    built = desktop.App(window, preload=str(prompt_file), project_dir=str(tmp_path))
    try:
        assert _text(built.in_prompt) == "预填的提示词"
        assert built.in_project.get() == str(tmp_path)
    finally:
        built._alive = False
        window.destroy()


def test_desktop_help_exits_without_opening_a_window() -> None:
    proc = subprocess.run(
        [sys.executable, str(REPO / "extras" / "desktop.py"), "--help"],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    assert proc.returncode == 0
    assert "--project-dir" in proc.stdout and "--prompt-file" in proc.stdout


# ---------------------------------------------------------------------------
# 2026-10-21：两块结果都要导得出来。
# 真窗手测抓到——免费扫描之后只有「项目进度」那块有内容，而复制/另存只挂在提示词卡上，
# 点「另存为…」得到一句"还没有可保存的提示词"，刚读出来的进度一个字也拿不走。
# ---------------------------------------------------------------------------
def _flush(app: desktop.App, text: str) -> None:
    app.out_analysis.configure(state="normal")
    app.out_analysis.delete("1.0", "end")
    app.out_analysis.insert("1.0", text)
    app.out_analysis.configure(state="disabled")


def test_scan_only_reading_is_exportable(
    app: desktop.App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """只做了免费扫描时，项目进度必须能另存 —— 那是本次唯一的结果。"""
    app.out_prompt.delete("1.0", "end")
    _flush(app, "项目：demo · 读取 3 个文件")

    target = tmp_path / "analysis.md"
    monkeypatch.setattr(desktop.filedialog, "asksaveasfilename", lambda **_kw: str(target))
    app.act_save_analysis()
    assert target.read_text(encoding="utf-8").startswith("项目：demo")
    # 状态行要说清导的是哪块，别让用户猜
    assert "项目进度" in str(app.status_lbl.cget("text")), app.status_lbl.cget("text")


def test_scan_only_reading_is_copyable(app: desktop.App) -> None:
    app.out_prompt.delete("1.0", "end")
    _flush(app, "项目：demo · 读取 3 个文件")
    app.act_copy_analysis()
    try:
        assert app.root.clipboard_get() == "项目：demo · 读取 3 个文件"
    except tk.TclError:
        pass  # 无剪贴板的后端不阻断其余断言
    assert "项目进度" in str(app.status_lbl.cget("text")), app.status_lbl.cget("text")


def test_ctrl_s_falls_back_to_the_reading_when_there_is_no_prompt(
    app: desktop.App, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ctrl+S：有提示词存提示词（交付物优先），没有就存项目进度。"""
    app.out_prompt.delete("1.0", "end")
    _flush(app, "只有进度")
    target = tmp_path / "fallback.md"
    monkeypatch.setattr(desktop.filedialog, "asksaveasfilename", lambda **_kw: str(target))
    app.act_save()
    assert target.read_text(encoding="utf-8") == "只有进度\n"
    assert "项目进度" in str(app.status_lbl.cget("text")), app.status_lbl.cget("text")


def test_pick_block_prefers_the_prompt(app: desktop.App) -> None:
    """提示词是这个窗口的交付物，两块都有内容时先给提示词。"""
    app.out_prompt.delete("1.0", "end")
    app.out_prompt.insert("1.0", "提示词")
    _flush(app, "进度")
    picked = app._pick_block()
    assert picked is not None and picked[0] == "提示词", picked
    assert app._pick_block("项目进度")[0] == "项目进度"  # type: ignore[index]


def test_pick_block_returns_none_when_both_are_empty(app: desktop.App) -> None:
    """两块都空时不许挑出一块空内容去写文件。"""
    app.out_prompt.delete("1.0", "end")
    _flush(app, "")
    assert app._pick_block() is None
    assert app._pick_block("项目进度") is None
