"""PromptMaster 桌面端 —— 一个 Tkinter 原生窗口，不开浏览器、不起 HTTP 服务。

为什么用 Tk（标准库）而不是 pywebview / Qt：这个产品的人类入口应该在任何一台装了 Python 的
机器上直接就能开，多一个 GUI 依赖就多一个"装不上所以没用"的理由。Tk 8.6 随 CPython 发行，
Windows 上走 `vista` 原生主题。

三条不变量：
1. **判据不在这个文件里**。所有判定都来自 subprocess 打 `run.py`（`pm.cliforward`），
   与 CLI / MCP / 网页面板同一条路。这里只做渲染与线程调度；连退出码都用
   `pm.cli.support._result_exit_code`，不另立一套 status→码 的映射。
2. **不在 worker 线程里摸 widget**。Tk 的 API 不是线程安全的：耗时动作跑在守护线程里，
   结果经 `queue.Queue` 回投，主循环用 `after()` 取。这是 Tk 应用最常见的崩法。
3. **花钱之前先报价**。④闭环提交前把零调用的 `--dry-run` 预估区间写在确认框里，并同时写明
   "优化后是否更好，按本项目自己的判据仍 0 例确证" —— 界面不许替结论打广告。
"""

from __future__ import annotations

import json
import os
import queue
import tempfile
import threading
import tkinter as tk
from collections.abc import Callable
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any

from pm.cli.support import _result_exit_code
from pm.cliforward import (
    ForwardError,
    TemplateNotFound,
    list_templates,
    run_cli_json,
    template_path,
)

APP_TITLE = "PromptMaster · 桌面面板"
WIN_GEOMETRY = "780x660"
BG = "#171a21"
CARD = "#1f2430"
INK = "#e6e9ef"
MUTED = "#8b94a7"
OK = "#3fb27f"
BAD = "#e05c5c"
ACCENT = "#5b8cff"
MONO = ("Consolas", 10)

# 实测量级（来源：docs/evaluation.md §三十·二十二 与 2026-10-13 r3 的 60 分钟 / 74 次调用）
COST_NOTE = "一轮实测约 60 分钟 / 70~80 次真实调用"
HONESTY_NOTE = "「优化后更好」按本项目判据（盲评≥3/4 且 Δ>本轮噪声带）至今 0 例确证"

# 会真的打端点的动作，超时不能按秒级算：单发 timeout 已由 PM_CALL_BUDGET 兜住，
# 这里给整轮留够墙钟，None = 由子进程自己决定何时结束。
LONG_RUN_TIMEOUT: float | None = None
SHORT_TIMEOUT = 60.0


# ---------------------------------------------------------------------------
# 渲染：纯函数（CLI 的 JSON → 要显示的行）。不碰 Tk，所以没有显示器也能测。
# ---------------------------------------------------------------------------
def render_check(d: dict[str, Any]) -> list[str]:
    """① 体检：一句结论 + 命中条目 + 结构统计。"""
    findings = d.get("findings") or []
    head = "无命中（退出码 0）" if d.get("ok") else f"命中 {len(findings)} 条（退出码 1）"
    stats = (
        f"字符 {d.get('chars', 0)} · 行 {d.get('lines', 0)} · "
        f"约束 {d.get('constraints', 0)}/{d.get('constraint_limit', 8)} · "
        f"结构条目 {d.get('structure_items', 0)} · 定界符问题 {len(d.get('delimiter_problems') or [])}"
    )
    lines = [head, "", stats, ""]
    for f in findings:
        lines.append(f"· {f.get('code')}")
        lines.append(f"  {f.get('detail')}")
        lines.append("")
    if not findings:
        lines.append("这条只证「没踩已知事故模式」，不证任务能完成。")
    return lines


def render_cases(d: dict[str, Any]) -> list[str]:
    """② 考卷：四桶分区 + 点名强判据在第几条。"""
    total = int(d.get("cases") or 0)
    able = int(d.get("veto_capable") or 0)
    strong = int(d.get("veto_strong") or 0)
    lines = [f"{total} 条里 {able} 条能否决，其中强判据 {strong} 条", ""]
    if not strong:
        lines.append("⇒ 这份考卷拦不住「写得漂亮但做不到」的输出")
    lines += [
        f"强判据（字面/正则能否决） : {strong}",
        f"只挡得住丢内容（复读即过）: {int(d.get('echo_only') or 0)}",
        f"交评委（rule）            : {int(d.get('judge_only') or 0)}",
        f"空 expected 不执行        : {int(d.get('no_assertion') or 0)}",
        f"提醒项（不否决）          : {int(d.get('advisory') or 0)} · 上限 {int(d.get('max_cases') or 0)}",
        "",
    ]
    at = d.get("veto_strong_cases") or []
    lines.append("强判据在第 " + "、".join(str(x) for x in at) + " 条" if at else "没有强判据")
    for f in d.get("findings") or []:
        lines += ["", f"· {f.get('code')}", f"  {str(f.get('detail'))[:300]}"]
    return lines


def render_diff(d: dict[str, Any]) -> list[str]:
    """③ 差分：规则得失 + 结构变化 + 逐行 diff（临时路径换成中文标签，别把宿主路径给用户看）。"""
    rules = d.get("rules") or {}
    items = (d.get("structure") or {}).get("items") or {}
    intro = rules.get("introduced") or []
    lines = [
        str(d.get("summary") or ""),
        "" if d.get("regressed") else "没有新引入已知事故模式",
        "",
        f"修好 {len(rules.get('fixed') or [])} 条 · 新引入 {len(intro)} 条 · 仍在 {len(rules.get('still') or [])} 条",
        f"约束条目 {items.get('before', 0)} → {items.get('after', 0)} · 逐字相同 {'是' if d.get('identical') else '否'}",
    ]
    if intro:
        lines += ["", "下面这些是本次新引入的 —— CI 该拦的就是这一半："]
        lines += [f"· {code}" for code in intro]
    unified = str(d.get("unified") or "")
    if unified:
        parts = unified.split("\n")
        if len(parts) > 1:
            parts[0], parts[1] = "--- 改动前", "+++ 改动后"
        lines += ["", "\n".join(parts)]
    return lines


def render_dry_run(d: dict[str, Any]) -> list[str]:
    """报价（零调用）：预估调用区间 + 它自己的口径说明。"""
    est = d.get("estimated_llm_calls") or {}
    return [
        f"预估调用 {est.get('min')}~{est.get('max')} 次",
        f"用例 {d.get('cases')} · 采样 {d.get('samples')} · 评委 {d.get('judges')} · 最大迭代 {d.get('max_iterations')}",
        f"基线对照 {'开' if d.get('baseline_enabled') else '关'} · 成对盲评 {'开' if d.get('pairwise_enabled') else '关'}",
        "",
        str(d.get("notes") or ""),
    ]


def render_run(d: dict[str, Any]) -> list[str]:
    """④ 闭环结果：状态 + 自评 Δ + 盲评结论 + 产物路径；退出码取自 CLI 同一份映射。"""
    agg = d.get("aggregate") or {}
    base = d.get("baseline_aggregate") or {}
    pair = d.get("pairwise") or {}
    avg, bavg = agg.get("avg_score"), base.get("avg_score")
    delta = "—" if avg is None or bavg is None else round(float(avg) - float(bavg), 2)
    tally = pair.get("tally") or pair.get("votes") or {}
    lines = [
        f"状态 {d.get('status')}（退出码 {_result_exit_code(d.get('status'))}：0 达标 / 1 未达标但已交付 / 3 失败）",
        f"轮次 {d.get('iterations')} / 上限 {d.get('max_iterations')} · LLM 调用 {d.get('llm_calls')}",
        f"自评分 基线 {bavg if bavg is not None else '—'} → 优化后 {avg if avg is not None else '—'} · Δ {delta}",
        f"盲评 {pair.get('verdict') or '—'} · 投票 {json.dumps(tally, ensure_ascii=False) if tally else '（无）'}",
    ]
    if d.get("early_stop_reason"):
        lines.append(f"早停：{d['early_stop_reason']}")
    if d.get("report_path"):
        lines.append(f"交付报告：{d['report_path']}")
    lines += ["", HONESTY_NOTE]
    return lines


def run_available() -> bool:
    """能不能跑闭环：有 `PM_FAKE_BACKEND` 或有 Key。晚绑定现读，别缓存（Key 可以运行时补）。"""
    if (os.getenv("PM_FAKE_BACKEND") or "").strip():
        return True
    return bool((os.getenv("PM_API_KEY") or "").strip() or (os.getenv("PM_API_KEYS") or "").strip())


class App:
    """主窗口：四个标签 + 底部状态灯。耗时动作一律 worker 线程 + 队列回投。"""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.q: queue.Queue[tuple[str, str, list[str]]] = queue.Queue()
        self.ok_target = "check"
        self.err_target = "check"
        self.last_run_id = ""
        self.panes: dict[str, tk.Text] = {}
        root.title(APP_TITLE)
        root.geometry(WIN_GEOMETRY)
        root.configure(bg=BG)
        self._style()
        self._header()
        nb = ttk.Notebook(root)
        nb.pack(fill="both", expand=True, padx=12, pady=(0, 6))
        self._tab_check(nb)
        self._tab_cases(nb)
        self._tab_diff(nb)
        self._tab_run(nb)
        self._footer()
        self._probe_async()
        root.after(120, self._drain)

    # ---- 外观 ----
    def _style(self) -> None:
        """深色配色走 `clam`：`vista` / `winnative` 是原生主题，**不理会** background/foreground
        —— 截图里按钮变成一块块浅灰、字几乎看不见，标签选中项也糊。`clam` 全量可着色，
        代价是不那么"原生"，但这是"能看清"与"看不清"的区别。clam 缺失时才退回原生主题。"""
        st = ttk.Style()
        st.theme_use("clam" if "clam" in st.theme_names() else "alt")
        st.configure(
            ".",
            background=BG,
            foreground=INK,
            fieldbackground=CARD,
            bordercolor=BG,
            lightcolor=BG,
            darkcolor=BG,
            troughcolor=BG,
        )
        st.configure("TNotebook", background=BG, borderwidth=0)
        st.configure(
            "TNotebook.Tab", padding=(14, 8), background=CARD, foreground=MUTED, borderwidth=0
        )
        st.map(
            "TNotebook.Tab",
            background=[("selected", ACCENT)],
            foreground=[("selected", "#ffffff"), ("active", INK)],
        )
        st.configure(
            "TButton",
            padding=(11, 5),
            background=CARD,
            foreground=INK,
            bordercolor=BG,
            lightcolor=CARD,
            darkcolor=CARD,
            focuscolor=ACCENT,
        )
        st.map("TButton", background=[("active", ACCENT)], foreground=[("active", "#ffffff")])
        st.configure("TLabel", background=BG, foreground=INK)
        st.configure("Muted.TLabel", background=BG, foreground=MUTED)
        st.configure("TFrame", background=BG)
        st.configure(
            "TSpinbox", fieldbackground=CARD, foreground=INK, arrowcolor=INK, bordercolor=BG
        )
        st.configure(
            "TCombobox", fieldbackground=CARD, background=CARD, foreground=INK, arrowcolor=INK
        )
        st.map("TCombobox", fieldbackground=[("readonly", CARD)], foreground=[("readonly", INK)])
        self.root.option_add("*TCombobox*Listbox.background", CARD)
        self.root.option_add("*TCombobox*Listbox.foreground", INK)
        self.root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)

    def _text_pane(self, parent: tk.Widget, height: int) -> tk.Text:
        frm = ttk.Frame(parent)
        txt = tk.Text(
            frm,
            height=height,
            wrap="word",
            bg=CARD,
            fg=INK,
            insertbackground=INK,
            relief="flat",
            font=MONO,
            state="disabled",
        )
        bar = ttk.Scrollbar(frm, command=txt.yview)
        txt.configure(yscrollcommand=bar.set)
        txt.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        frm.pack(fill="both", expand=True, pady=(8, 0))
        return txt

    def _input(self, parent: tk.Widget, height: int) -> tk.Text:
        return tk.Text(
            parent,
            height=height,
            wrap="word",
            bg=CARD,
            fg=INK,
            insertbackground=INK,
            relief="flat",
            font=MONO,
        )

    def _show(self, key: str, lines: list[str]) -> None:
        w = self.panes[key]
        w.configure(state="normal")
        w.delete("1.0", "end")
        w.insert("1.0", "\n".join(lines).rstrip())
        w.configure(state="disabled")

    def _status(self, text: str, color: str = MUTED) -> None:
        self.status_lbl.configure(text=text, foreground=color)

    def _header(self) -> None:
        top = ttk.Frame(self.root)
        top.pack(fill="x", padx=14, pady=(12, 6))
        ttk.Label(top, text="PromptMaster", font=("Segoe UI", 13, "bold")).pack(side="left")
        ttk.Label(top, text="桌面面板 · 判据同源 run.py", style="Muted.TLabel").pack(
            side="left", padx=8
        )

    def _footer(self) -> None:
        bar = ttk.Frame(self.root)
        bar.pack(fill="x", padx=14, pady=(0, 10))
        self.led_cli = tk.Label(bar, text="转发检测中…", bg=BG, fg=MUTED, font=("Segoe UI", 9))
        self.led_cli.pack(side="left")
        self.led_key = tk.Label(bar, text="", bg=BG, fg=MUTED, font=("Segoe UI", 9))
        self.led_key.pack(side="left", padx=(14, 0))
        self.status_lbl = tk.Label(bar, text="", bg=BG, fg=MUTED, font=("Segoe UI", 9))
        self.status_lbl.pack(side="right")

    def _led(self, w: tk.Label, text: str, good: bool) -> None:
        w.configure(text=("● " if good else "× ") + text, fg=OK if good else BAD)

    # ---- 线程调度 ----
    def _spawn(self, label: str, work: Callable[[], list[str]]) -> None:
        def runner() -> None:
            try:
                self.q.put(("ok", label, work()))
            except (ForwardError, TemplateNotFound, OSError, ValueError) as e:
                self.q.put(("err", label, [str(e)]))

        self._status(label + " 进行中…")
        threading.Thread(target=runner, daemon=True, name=f"pm-desktop-{label}").start()

    def _drain(self) -> None:
        drained = False
        while True:
            try:
                kind, label, lines = self.q.get_nowait()
            except queue.Empty:
                break
            drained = True
            if kind == "led":
                self._apply_probe(lines)
            elif kind == "err":
                self._status(label + " 失败", BAD)
                self._show(self.err_target, lines)
            else:
                self._status(label + " 完成", OK)
                self._show(self.ok_target, lines)
        if not drained:
            self.root.after(120, self._drain)
        else:
            self.root.after(1, self._drain)

    def _apply_probe(self, lines: list[str]) -> None:
        """首屏自检的落地半边 —— **必须在主线程**：灯是 widget，widget 只能主线程碰。"""
        cli_ok, key_ready, note = lines[0] == "1", lines[1] == "1", lines[2]
        self._led(self.led_cli, "零调用可用" if cli_ok else "零调用不可用", cli_ok)
        self._led(self.led_key, "Key 就绪" if key_ready else "无 Key（④不可用）", key_ready)
        if not cli_ok:
            self._status("转发不可用：" + note, BAD)

    def _probe_async(self) -> None:
        """首屏自检：转发通不通要**真跑一发**，Key 在不在要现读环境变量。

        ⚠️ 线程里只碰子进程与 `os.getenv`，结果一律经队列回投。Tk 的 `after()` 同样不是
        线程安全的 —— 第一版在这里调 `root.after(0, …)`，实测后台线程直接抛
        `RuntimeError: main thread is not in main loop`，表现是底部两盏灯永远停在"检测中"。
        """

        def work() -> None:
            try:
                d = run_cli_json(
                    ["check", "--prompt", "探针：一份很短的提示词", "--json"], timeout=SHORT_TIMEOUT
                )
                ok = str(d.get("mode")) == "prompt_check"
                note = "" if ok else f"mode={d.get('mode')!r}"
            except (ForwardError, OSError, ValueError) as e:
                ok, note = False, str(e)[:160]
            self.q.put(
                ("led", "probe", ["1" if ok else "0", "1" if run_available() else "0", note])
            )

        threading.Thread(target=work, daemon=True, name="pm-desktop-probe").start()

    # ---- 标签①：体检 ----
    def _tab_check(self, nb: ttk.Notebook) -> None:
        tab = ttk.Frame(nb)
        nb.add(tab, text="① 提示词体检")
        self.in_check = self._input(tab, 7)
        self.in_check.pack(fill="x", pady=(10, 6))
        row = ttk.Frame(tab)
        row.pack(fill="x")
        ttk.Button(row, text="体检", command=self.act_check).pack(side="left")
        ttk.Button(row, text="从文件载入…", command=self.act_load_check).pack(side="left", padx=6)
        ttk.Button(row, text="清空", command=lambda: self._fill(self.in_check, "")).pack(
            side="left"
        )
        ttk.Label(
            row,
            text="零调用、不联网；回答「会不会出事」，不回答「能不能完成任务」",
            style="Muted.TLabel",
        ).pack(side="right")
        self.panes["check"] = self._text_pane(tab, 12)

    def act_check(self) -> None:
        text = self.in_check.get("1.0", "end").rstrip()
        self.ok_target = self.err_target = "check"
        if not text:
            self._status("先把要体检的提示词贴进来", BAD)
            return
        self._spawn(
            "体检", lambda: render_check(run_cli_json(["check", "--prompt", text, "--json"]))
        )

    def act_load_check(self) -> None:
        path = filedialog.askopenfilename(
            title="载入提示词", filetypes=[("文本", "*.md *.txt"), ("所有文件", "*.*")]
        )
        if not path:
            return
        try:
            self._fill(self.in_check, Path(path).read_text(encoding="utf-8", errors="replace"))
        except OSError as e:
            self._status(f"读不到文件：{e}", BAD)

    # ---- 标签②：考卷 ----
    def _tab_cases(self, nb: ttk.Notebook) -> None:
        tab = ttk.Frame(nb)
        nb.add(tab, text="② 考卷有几票")
        row = ttk.Frame(tab)
        row.pack(fill="x", pady=(10, 6))
        ttk.Label(row, text="自带模板").pack(side="left")
        names = [t["name"] for t in list_templates()]
        self.tpl = ttk.Combobox(row, width=34, state="readonly", values=names)
        self.tpl.pack(side="left", padx=8)
        if names:
            self.tpl.current(0)
        ttk.Button(row, text="体检这份考卷", command=self.act_cases_tpl).pack(side="left")
        ttk.Label(row, text="花钱之前先量考卷", style="Muted.TLabel").pack(side="right")
        self.in_cases = self._input(tab, 5)
        self.in_cases.pack(fill="x")
        ttk.Button(tab, text="体检粘贴的 JSON 数组", command=self.act_cases_paste).pack(
            anchor="w", pady=(6, 0)
        )
        self.panes["cases"] = self._text_pane(tab, 12)

    def act_cases_tpl(self) -> None:
        name = self.tpl.get()
        self.ok_target = self.err_target = "cases"
        if not name:
            self._status("先选一个模板", BAD)
            return
        self._spawn(
            "考卷体检",
            lambda: render_cases(
                run_cli_json(["check", "--cases-file", str(template_path(name)), "--json"])
            ),
        )

    def act_cases_paste(self) -> None:
        raw = self.in_cases.get("1.0", "end").strip()
        self.ok_target = self.err_target = "cases"
        if not raw:
            self._status("粘贴区是空的", BAD)
            return
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as e:
            self._show("cases", [f"JSON 解析失败：{e}"])
            self._status("粘贴内容不是合法 JSON", BAD)
            return

        def work() -> list[str]:
            with tempfile.TemporaryDirectory() as d:
                f = Path(d) / "cases.json"
                f.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                return render_cases(run_cli_json(["check", "--cases-file", str(f), "--json"]))

        self._spawn("考卷体检", work)

    # ---- 标签③：差分 ----
    def _tab_diff(self, nb: ttk.Notebook) -> None:
        tab = ttk.Frame(nb)
        nb.add(tab, text="③ 两版差分")
        self.in_a = self._input(tab, 6)
        self.in_a.pack(fill="x", pady=(10, 2))
        ttk.Label(tab, text="↓ 改成", style="Muted.TLabel").pack(anchor="w")
        self.in_b = self._input(tab, 6)
        self.in_b.pack(fill="x")
        row = ttk.Frame(tab)
        row.pack(fill="x", pady=(6, 0))
        ttk.Button(row, text="差分", command=self.act_diff).pack(side="left")
        ttk.Button(row, text="把①的正文填到「前」", command=self.act_fill_a).pack(
            side="left", padx=6
        )
        ttk.Label(row, text="零调用 · 只拦本次新引入", style="Muted.TLabel").pack(side="right")
        self.panes["diff"] = self._text_pane(tab, 12)

    def act_fill_a(self) -> None:
        self._fill(self.in_a, self.in_check.get("1.0", "end").rstrip())

    def act_diff(self) -> None:
        a = self.in_a.get("1.0", "end").rstrip()
        b = self.in_b.get("1.0", "end").rstrip()
        self.ok_target = self.err_target = "diff"
        if not a or not b:
            self._status("两版都要填", BAD)
            return

        def work() -> list[str]:
            with tempfile.TemporaryDirectory() as d:
                pa, pb = Path(d) / "a.md", Path(d) / "b.md"
                pa.write_text(a, encoding="utf-8")
                pb.write_text(b, encoding="utf-8")
                return render_diff(run_cli_json(["diff", str(pa), str(pb), "--json"]))

        self._spawn("差分", work)

    # ---- 标签④：闭环 ----
    def _tab_run(self, nb: ttk.Notebook) -> None:
        tab = ttk.Frame(nb)
        nb.add(tab, text="④ 优化闭环")
        ttk.Label(
            tab,
            text=f"{COST_NOTE}。\n{HONESTY_NOTE} —— 界面给的是自评分 Δ 与成对盲评，请看结论别只看分数。",
            style="Muted.TLabel",
            wraplength=700,
            justify="left",
        ).pack(anchor="w", pady=(10, 6))
        self.in_task = self._input(tab, 3)
        self.in_task.pack(fill="x")
        row = ttk.Frame(tab)
        row.pack(fill="x", pady=(6, 0))
        ttk.Label(row, text="目标模型").pack(side="left")
        self.model = ttk.Entry(row, width=22)
        self.model.pack(side="left", padx=6)
        ttk.Label(row, text="用例").pack(side="left")
        self.cases_n = ttk.Spinbox(row, from_=1, to=8, width=4)
        self.cases_n.set("4")
        self.cases_n.pack(side="left", padx=4)
        ttk.Label(row, text="最大迭代").pack(side="left")
        self.iter_n = ttk.Spinbox(row, from_=0, to=10, width=4)
        self.iter_n.set("2")
        self.iter_n.pack(side="left", padx=4)
        ttk.Button(row, text="先报价（零调用）", command=self.act_estimate).pack(
            side="left", padx=(10, 0)
        )
        ttk.Button(row, text="提交任务", command=self.act_run).pack(side="left", padx=6)
        ttk.Button(row, text="取报告", command=self.act_report).pack(side="left")
        self.panes["run"] = self._text_pane(tab, 10)

    def _ints(self) -> tuple[int, int]:
        try:
            return int(self.cases_n.get()), int(self.iter_n.get())
        except ValueError:
            return 4, 2

    def _argv(self, extra: list[str]) -> list[str]:
        task = self.in_task.get("1.0", "end").strip()
        cases, iters = self._ints()
        argv = [*extra, "--task", task, "--json", "--cases", str(cases), "--max-iter", str(iters)]
        model = self.model.get().strip()
        if model:
            argv += ["--target-model", model]
        return argv

    def _need_task(self) -> bool:
        if len(self.in_task.get("1.0", "end").strip()) >= 4:
            return True
        self._status("需求至少 4 个字", BAD)
        return False

    def act_estimate(self) -> None:
        self.ok_target = self.err_target = "run"
        if not self._need_task():
            return
        argv = self._argv(["--dry-run"])  # 主线程取值：worker 线程里不许摸 widget
        self._spawn("预算估计", lambda: render_dry_run(run_cli_json(argv)))

    def act_run(self) -> None:
        self.ok_target = self.err_target = "run"
        if not self._need_task():
            return
        if not run_available():
            self._status("没配 Key：④不可用（①②③不受影响）", BAD)
            return
        if not messagebox.askyesno(
            "确认提交", f"{COST_NOTE}。\n{HONESTY_NOTE}。\n\n现在提交这一轮？"
        ):
            self._status("已取消，未产生任何调用")
            return

        argv = self._argv([])  # 同上：进线程之前把 widget 的值全部读成普通变量

        def work() -> list[str]:
            d = run_cli_json(argv, timeout=LONG_RUN_TIMEOUT)
            self.last_run_id = str(d.get("run_id") or "")
            return render_run(d)

        self._spawn("优化闭环", work)

    def act_report(self) -> None:
        self.ok_target = self.err_target = "run"
        if not self.last_run_id:
            self._status("还没有 run_id（先提交一轮）", BAD)
            return
        self._spawn(
            "取报告", lambda: [str(run_cli_json(["report", self.last_run_id]).get("report", ""))]
        )

    def _fill(self, w: tk.Text, text: str) -> None:
        w.delete("1.0", "end")
        w.insert("1.0", text)


def app_main(argv: list[str] | None = None) -> int:
    """入口：建窗口、进主循环。Tk 必须在主线程，所以这里不接 worker/并发参数。"""
    del argv  # 目前无参数；留位是为了 `run.py desktop --help` 之后能加而不改调用形状
    root = tk.Tk()
    App(root)
    root.mainloop()
    return 0
