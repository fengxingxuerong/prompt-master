"""桌面端（`pm/desktop.py`）的回归。

分两层，因为这两层能证明的事完全不同：

1. **渲染层（不需要显示器）**：`render_*` 是纯函数，输入 CLI 的 JSON、输出要显示的行。
   判据不在这里产生，所以这层只查"该说的话有没有说"——尤其是那句
   "优化有效 0 例确证"，它不许被哪次改样式顺手删掉。
2. **窗口层（需要显示器）**：真建 Tk 窗口、真走 worker 线程 + 队列回投。
   这一层抓到过一个**只有真跑才会现形**的缺陷：探针线程里调 `root.after(0, …)`，
   而 Tk 的 `after()` 同样不是线程安全的 ⇒ 后台线程抛
   `RuntimeError: main thread is not in main loop`，表现是底部两盏灯永远停在"检测中"。
   所以这里断言的是"灯最终会变"，不是"代码看起来对"。

ubuntu CI 上没有 X，第二层自动跳过 —— 它不是"可选的测试"，是"这台机器才量得到的那一半"。
"""

from __future__ import annotations

import os
import time
from typing import Any

import pytest
from pm import desktop
from pm.cliforward import ForwardError

CHECK_PAYLOAD = {
    "mode": "prompt_check",
    "ok": False,
    "findings": [
        {"code": "blanket_missing_branch", "detail": "「数据缺失」被写成全局兜底"},
        {"code": "too_short", "detail": "提示词过短（28 字符 < 100）"},
    ],
    "chars": 28,
    "lines": 1,
    "constraints": 0,
    "constraint_limit": 8,
    "structure_items": 0,
    "delimiter_problems": [],
}

CASES_PAYLOAD = {
    "mode": "cases_check",
    "ok": False,
    "cases": 4,
    "max_cases": 8,
    "veto_capable": 2,
    "veto_strong": 0,
    "echo_only": 2,
    "judge_only": 2,
    "no_assertion": 0,
    "advisory": 0,
    "veto_strong_cases": [],
    "findings": [{"code": "echoes", "detail": "expected 就在本条 input 里"}],
}

DIFF_PAYLOAD = {
    "mode": "prompt_diff",
    "ok": False,
    "regressed": True,
    "summary": "新引入 1 类规则问题；约束条目 1→1",
    "identical": False,
    "rules": {"fixed": [], "introduced": ["blanket_missing_branch"], "still": ["too_short"]},
    "structure": {"items": {"before": 1, "after": 1}},
    "unified": "--- C:\\Users\\someone\\AppData\\Local\\Temp\\x\\a.md\n+++ C:\\Users\\someone\\b.md\n 上下文",
}


def _text(lines: list[str]) -> str:
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 1. 渲染层（纯函数，无需显示器）
# ---------------------------------------------------------------------------
def test_render_check_names_findings_and_stats() -> None:
    out = _text(desktop.render_check(CHECK_PAYLOAD))
    assert "命中 2 条" in out
    assert "blanket_missing_branch" in out and "too_short" in out
    assert "字符 28" in out and "约束 0/8" in out


def test_render_check_on_a_clean_prompt_says_what_it_does_not_prove() -> None:
    out = _text(desktop.render_check({"mode": "prompt_check", "ok": True, "findings": []}))
    assert "无命中" in out
    assert "不证任务能完成" in out, "这条边界声明是产品的一贯口径，不许在改样式时丢掉"


def test_render_cases_shows_the_four_buckets_and_the_warning() -> None:
    out = _text(desktop.render_cases(CASES_PAYLOAD))
    assert "4 条里 2 条能否决" in out
    assert "强判据 0 条" in out
    assert "拦不住" in out
    for label in ("强判据", "只挡得住丢内容", "交评委", "空 expected 不执行"):
        assert label in out, f"四桶少了 {label}"
    assert "echoes" in out


def test_render_diff_hides_the_host_temp_path() -> None:
    """宿主临时路径不该出现在界面上（网页面板同一条，桌面端也别漏）。"""
    out = _text(desktop.render_diff(DIFF_PAYLOAD))
    assert "AppData" not in out and "Temp" not in out
    assert "--- 改动前" in out and "+++ 改动后" in out
    assert "新引入 1 条" in out


def test_render_diff_when_nothing_new_is_introduced() -> None:
    payload = dict(
        DIFF_PAYLOAD, regressed=False, rules={"fixed": ["too_short"], "introduced": [], "still": []}
    )
    out = _text(desktop.render_diff(payload))
    assert "没有新引入已知事故模式" in out


def test_render_dry_run_reports_the_estimate_range() -> None:
    payload = {
        "mode": "dry_run",
        "cases": 3,
        "samples": 2,
        "judges": 2,
        "max_iterations": 3,
        "baseline_enabled": True,
        "pairwise_enabled": True,
        "estimated_llm_calls": {"min": 45, "max": 102},
        "notes": "下界=首轮即达标",
    }
    out = _text(desktop.render_dry_run(payload))
    assert "预估调用 45~102 次" in out
    assert "基线对照 开" in out and "成对盲评 开" in out


def test_render_run_uses_the_same_exit_code_mapping_as_the_cli() -> None:
    """退出码不许在桌面端另立一套：`_result_exit_code` 是唯一映射。"""
    payload = {
        "status": "max_iterations",
        "iterations": 2,
        "max_iterations": 2,
        "llm_calls": 74,
        "aggregate": {"avg_score": 9.5},
        "baseline_aggregate": {"avg_score": 7.35},
        "pairwise": {"verdict": "baseline_wins", "tally": {"win": 1, "lose": 3}},
        "report_path": r"D:\logs\report_x.md",
    }
    out = _text(desktop.render_run(payload))
    assert "退出码 1" in out
    assert "Δ 2.15" in out
    assert "baseline_wins" in out
    assert desktop.HONESTY_NOTE in out, "结论轴那句诚实话必须在界面上"


def test_render_run_survives_missing_sections() -> None:
    """空产物早停那一轮真的存在过（status=early_stopped、aggregate 为空）——不能崩。"""
    out = _text(
        desktop.render_run({"status": "early_stopped", "early_stop_reason": "未观测到截断证据"})
    )
    assert "早停" in out and "0 例确证" in out


def test_run_available_reads_env_at_call_time(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("PM_API_KEY", "PM_API_KEYS", "PM_FAKE_BACKEND"):
        monkeypatch.delenv(name, raising=False)
    assert desktop.run_available() is False
    monkeypatch.setenv("PM_API_KEY", "sk-not-real")
    assert desktop.run_available() is True
    monkeypatch.delenv("PM_API_KEY")
    monkeypatch.setenv("PM_FAKE_BACKEND", "progress")
    assert desktop.run_available() is True, "演示模式也算可用，否则假后端下这个入口自己把自己关掉了"


# ---------------------------------------------------------------------------
# 2. 窗口层（真 Tk：抓线程安全那一形）
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def tk_root():
    """整个模块共用**一个** Tk 根。

    每条用例各建各的根，在本机实测会撞 Windows 上的 Tcl 终结器：跑到第 18 条时
    `Windows fatal exception: code 0x80000003`，崩在探针线程正等的 `subprocess._readerthread`
    里 —— 整个 pytest 进程直接没了（rc=3），不是某条用例红。根数降成 1 之后不再出现。
    """
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except Exception as e:  # noqa: BLE001 - 无显示器（如 ubuntu CI）时整层跳过
        pytest.skip(f"这台机器开不出 Tk 窗口：{e}")
    root.withdraw()  # 不在测试机上闪一个真窗口
    yield root
    root.destroy()


@pytest.fixture()
def app_and_pump(tk_root, monkeypatch: pytest.MonkeyPatch):
    """探针在测试里**不许起真子进程**：它每次建 App 都会跑一发，叠上共享根就是崩点。

    桩只替掉 `desktop.run_cli_json`（转发出口），线程/队列/主线程落地这条形状照原样跑 ——
    被这条夹具保护的是"灯最终会变"那个断言，不是桩自己。
    """

    def stub(args: list[str], timeout: float | None = 60.0) -> dict[str, object]:
        return {"mode": "prompt_check", "ok": True, "findings": []}

    monkeypatch.setattr(desktop, "run_cli_json", stub)
    built = desktop.App(tk_root)

    def pump(seconds: float = 1.5) -> None:
        end = time.time() + seconds
        while time.time() < end:
            tk_root.update()
            time.sleep(0.02)

    return built, pump


class _FakeForward:
    """替身转发：记录 argv，返回固定 payload（窗口层不该再花 1 秒起一发真子进程）。"""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str], timeout: float | None = 60.0) -> dict[str, Any]:
        self.calls.append(list(args))
        return self.payload


def test_probe_lights_update_via_the_queue_not_from_the_worker_thread(
    app_and_pump: tuple[desktop.App, Any],
) -> None:
    """底部两盏灯必须由**主线程**落地。

    这是那条真事故的回归位：第一版在 worker 线程里 `root.after(0, …)`，Tk 抛
    `main thread is not in main loop`，灯永远停在"检测中"。所以断言的是"灯最终会变"。
    """
    app, pump = app_and_pump
    assert "检测中" in str(app.led_cli.cget("text"))
    pump(2.0)
    cli = str(app.led_cli.cget("text"))
    key = str(app.led_key.cget("text"))
    assert "检测中" not in cli, f"探针结果没回投到主线程：{cli}"
    assert cli.startswith("●") or cli.startswith("×"), f"灯的形状不对：{cli}"
    assert key, "Key 灯也该有读数"


def test_act_check_renders_into_the_pane(
    app_and_pump: tuple[desktop.App, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    app, pump = app_and_pump
    fake = _FakeForward(CHECK_PAYLOAD)
    monkeypatch.setattr(desktop, "run_cli_json", fake)
    app._fill(app.in_check, "你是分析师。所有结论位置一律标注数据缺失。")
    app.act_check()
    pump(1.5)
    shown = str(app.panes["check"].get("1.0", "end"))
    assert "命中 2 条" in shown and "blanket_missing_branch" in shown
    assert fake.calls and fake.calls[0][:2] == ["check", "--prompt"], "该打的是体检那条 CLI"
    assert "完成" in str(app.status_lbl.cget("text"))


def test_act_cases_picks_a_whitelisted_template(
    app_and_pump: tuple[desktop.App, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    app, pump = app_and_pump
    fake = _FakeForward(CASES_PAYLOAD)
    monkeypatch.setattr(desktop, "run_cli_json", fake)
    monkeypatch.setattr(desktop, "template_path", lambda name: f"/tmp/{name}")
    app.tpl.set("support_triage.json")
    app.act_cases_tpl()
    pump(1.5)
    assert "4 条里 2 条能否决" in str(app.panes["cases"].get("1.0", "end"))
    assert "--cases-file" in fake.calls[0]


def test_forward_failure_shows_up_as_a_status_not_a_silent_pane(
    app_and_pump: tuple[desktop.App, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """转发坏了要在界面上说人话 —— 静默空白窗格比报错更坏（用户以为"没命中"）。"""
    app, pump = app_and_pump

    def boom(args: list[str], timeout: float | None = 60.0) -> dict[str, Any]:
        raise ForwardError("run.py 退出码异常 99：boom")

    monkeypatch.setattr(desktop, "run_cli_json", boom)
    app._fill(app.in_check, "一份提示词")
    app.act_check()
    pump(1.5)
    assert "退出码异常" in str(app.panes["check"].get("1.0", "end"))
    assert "失败" in str(app.status_lbl.cget("text"))


def test_run_without_key_refuses_before_spending_anything(
    app_and_pump: tuple[desktop.App, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """④在无 Key 时必须直接拒，且**不许**弹出确认框（弹了就说明它打算花钱）。"""
    app, pump = app_and_pump
    called: list[str] = []
    monkeypatch.setattr(desktop, "run_available", lambda: False)
    monkeypatch.setattr(
        desktop.messagebox, "askyesno", lambda *a, **k: called.append("asked") or True
    )
    fake = _FakeForward({"status": "passed"})
    monkeypatch.setattr(desktop, "run_cli_json", fake)
    app._fill(app.in_task, "让 AI 分析销售数据并给出趋势结论")
    app.act_run()
    pump(0.6)
    assert called == [], "无 Key 时不该走到确认框"
    assert fake.calls == [], "无 Key 时一发都不该打出去"
    assert "没配 Key" in str(app.status_lbl.cget("text"))


def test_cancelled_confirmation_sends_nothing(
    app_and_pump: tuple[desktop.App, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    app, pump = app_and_pump
    monkeypatch.setattr(desktop, "run_available", lambda: True)
    monkeypatch.setattr(desktop.messagebox, "askyesno", lambda *a, **k: False)
    fake = _FakeForward({"status": "passed"})
    monkeypatch.setattr(desktop, "run_cli_json", fake)
    app._fill(app.in_task, "让 AI 分析销售数据并给出趋势结论")
    app.act_run()
    pump(0.6)
    assert fake.calls == [], "点了取消就是没花钱"
    assert "已取消" in str(app.status_lbl.cget("text"))


def test_task_shorter_than_four_chars_is_rejected(
    app_and_pump: tuple[desktop.App, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """护栏与 CLI 同口径（`task` 的 min_length=4），别把"太短"送到子进程去撞退出码 2。"""
    app, pump = app_and_pump
    fake = _FakeForward({})
    monkeypatch.setattr(desktop, "run_cli_json", fake)
    app._fill(app.in_task, "短")
    app.act_estimate()
    pump(0.4)
    assert fake.calls == []
    assert "至少 4 个字" in str(app.status_lbl.cget("text"))


def test_probe_failure_is_spelled_out_in_the_status_line(
    app_and_pump: tuple[desktop.App, Any],
) -> None:
    """转发坏了不能只让灯变红——状态行要点名为什么（静默的"不可用"会被当成"没测"）。"""
    app, _pump = app_and_pump
    app._apply_probe(["0", "0", "run.py 退出码异常 99"])
    assert "转发不可用" in str(app.status_lbl.cget("text"))
    assert str(app.led_cli.cget("text")).startswith("×")


def test_empty_inputs_are_rejected_before_any_subprocess(
    app_and_pump: tuple[desktop.App, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """三张卡各自空输入时都不该派子进程 —— 护栏在窗口层，不是在 CLI 撞退出码 2。"""
    app, pump = app_and_pump
    fake = _FakeForward({})
    monkeypatch.setattr(desktop, "run_cli_json", fake)
    app.act_check()
    app.tpl.set("")
    app.act_cases_tpl()
    app.act_cases_paste()
    app.act_diff()
    app.act_estimate()
    app.act_report()
    pump(0.5)
    assert fake.calls == [], f"空输入还是打出去了：{fake.calls}"


def test_act_load_check_reads_a_real_file(
    app_and_pump: tuple[desktop.App, Any], monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    app, _pump = app_and_pump
    f = tmp_path / "我的提示词.md"
    f.write_text("一份从磁盘载入的提示词", encoding="utf-8")
    monkeypatch.setattr(desktop.filedialog, "askopenfilename", lambda **k: str(f))
    app.act_load_check()
    assert "从磁盘载入" in str(app.in_check.get("1.0", "end"))


def test_act_load_check_reports_an_unreadable_file(
    app_and_pump: tuple[desktop.App, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _pump = app_and_pump
    monkeypatch.setattr(desktop.filedialog, "askopenfilename", lambda **k: r"D:\没有这个文件.md")
    app.act_load_check()
    assert "读不到文件" in str(app.status_lbl.cget("text"))


def test_act_cases_paste_bad_json_says_so_and_good_json_goes_through(
    app_and_pump: tuple[desktop.App, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    app, pump = app_and_pump
    fake = _FakeForward(CASES_PAYLOAD)
    monkeypatch.setattr(desktop, "run_cli_json", fake)
    app._fill(app.in_cases, "{不是 JSON")
    app.act_cases_paste()
    pump(0.4)
    assert fake.calls == [], "坏 JSON 不该被送进子进程"
    assert "JSON 解析失败" in str(app.panes["cases"].get("1.0", "end"))
    app._fill(app.in_cases, '[{"input":"a","expected":"b"}]')
    app.act_cases_paste()
    pump(1.2)
    assert fake.calls and fake.calls[0][0] == "check" and "--cases-file" in fake.calls[0]


def test_act_diff_renders_and_tab_one_can_seed_tab_three(
    app_and_pump: tuple[desktop.App, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    app, pump = app_and_pump
    fake = _FakeForward(DIFF_PAYLOAD)
    monkeypatch.setattr(desktop, "run_cli_json", fake)
    app._fill(app.in_check, "改动前的正文")
    app.act_fill_a()
    assert "改动前的正文" in str(app.in_a.get("1.0", "end"))
    app._fill(app.in_b, "改动后的正文")
    app.act_diff()
    pump(1.2)
    assert "新引入 1 条" in str(app.panes["diff"].get("1.0", "end"))
    assert fake.calls and fake.calls[0][0] == "diff"


def test_spinbox_garbage_falls_back_to_the_documented_defaults(
    app_and_pump: tuple[desktop.App, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spinbox 能手输任意文本：坏值退成 4/2，而不是把 ValueError 抛到线程里静默死掉。"""
    app, pump = app_and_pump
    fake = _FakeForward({"estimated_llm_calls": {"min": 1, "max": 2}, "mode": "dry_run"})
    monkeypatch.setattr(desktop, "run_cli_json", fake)
    app.cases_n.set("abc")
    app.iter_n.set("")
    assert app._ints() == (4, 2)
    app._fill(app.in_task, "让 AI 分析销售数据并给出趋势结论")
    app.act_estimate()
    pump(1.2)
    assert fake.calls and "--cases" in fake.calls[0]
    assert "预估调用 1~2 次" in str(app.panes["run"].get("1.0", "end"))


def test_act_report_after_a_run_uses_the_same_cli_path(
    app_and_pump: tuple[desktop.App, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    app, pump = app_and_pump
    fake = _FakeForward({"status": "passed", "run_id": "abc123def456", "report": "# 交付报告"})
    monkeypatch.setattr(desktop, "run_cli_json", fake)
    monkeypatch.setattr(desktop, "run_available", lambda: True)
    monkeypatch.setattr(desktop.messagebox, "askyesno", lambda *a, **k: True)
    app.model.delete(0, "end")
    app.model.insert(0, "deepseek-v3")
    app._fill(app.in_task, "让 AI 分析销售数据并给出趋势结论")
    app.act_run()
    pump(1.2)
    assert app.last_run_id, "跑完要记住 run_id，否则「取报告」按钮是死的"
    assert "--target-model" in fake.calls[0], "填了目标模型就得带到 CLI，否则跑的是缺省模型"
    app.act_report()
    pump(1.2)
    assert fake.calls[-1][:2] == ["report", app.last_run_id]
    assert "# 交付报告" in str(app.panes["run"].get("1.0", "end"))


def test_cancelling_the_file_dialog_leaves_the_input_alone(
    app_and_pump: tuple[desktop.App, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """取消不是错误：别把状态行染红，也别清空用户已经贴好的正文。"""
    app, _pump = app_and_pump
    app._fill(app.in_check, "已经贴好的正文")
    monkeypatch.setattr(desktop.filedialog, "askopenfilename", lambda **k: "")
    app.act_load_check()
    assert "已经贴好的正文" in str(app.in_check.get("1.0", "end"))
    assert "读不到" not in str(app.status_lbl.cget("text"))


def test_app_main_builds_the_window_and_enters_the_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """入口本身也要被走过一次：Tk 必须在主线程进 mainloop，这条用替身验"确实进了循环"。"""
    calls: list[str] = []

    class _Root:
        def mainloop(self) -> None:
            calls.append("mainloop")

    class _FakeApp:
        def __init__(self, root: object) -> None:
            calls.append("app")

        def select_tab(self, key: str) -> None:
            calls.append("tab:" + key)

    monkeypatch.setattr(desktop.tk, "Tk", _Root)
    monkeypatch.setattr(desktop, "App", _FakeApp)
    assert desktop.app_main([]) == 0
    assert calls == ["app", "tab:check", "mainloop"], calls


def test_app_main_opens_the_requested_tab(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    class _Root:
        def mainloop(self) -> None:
            calls.append("mainloop")

    class _FakeApp:
        def __init__(self, root: object) -> None:
            pass

        def select_tab(self, key: str) -> None:
            calls.append("tab:" + key)

    monkeypatch.setattr(desktop.tk, "Tk", _Root)
    monkeypatch.setattr(desktop, "App", _FakeApp)
    assert desktop.app_main(["--tab", "cases"]) == 0
    assert calls == ["tab:cases", "mainloop"], calls


def test_desktop_help_exits_instead_of_opening_a_window() -> None:
    """真事故：第一版 `app_main` 忽略 argv，`run.py desktop --help` 直接开窗口卡在
    mainloop（实测 rc=124、stdout 0 字节）—— 用户会以为命令坏了。"""
    import subprocess
    import sys
    from pathlib import Path as _P

    root = _P(__file__).resolve().parents[1]
    proc = subprocess.run(
        [sys.executable, str(root / "run.py"), "desktop", "--help"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=90,
        cwd=str(root),
        stdin=subprocess.DEVNULL,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    assert proc.returncode == 0, proc.stderr[-300:]
    assert "run.py desktop" in proc.stdout
    assert "--tab" in proc.stdout
