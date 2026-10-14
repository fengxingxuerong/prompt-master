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

import argparse
import json
import os
import queue
import sys
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
    run_cli_text,
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
# 预检最坏会撞满单发墙钟预算（PM_CALL_BUDGET），留 20 分钟余量
PREFLIGHT_TIMEOUT = 1200.0
NO_CASES = "（不指定 · 让 mockgen 生成）"
PASTED = "（用②粘贴的内容）"


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


def render_history(d: dict[str, Any]) -> list[str]:
    """⑤ 历史：逐轮读数 + 同任务跨轮 Δ 显著性（按评委尺子分代）。"""
    lines = [f"最近 {d.get('n_runs')} 轮 · 口径 {d.get('method') or '—'}", ""]
    for r in d.get("runs") or []:
        delta = r.get("delta")
        lines.append(
            f"{r.get('mtime')}  {r.get('run_id')}  {r.get('status')}  "
            f"Δ {delta if delta is not None else '—'}  调用 {r.get('llm_calls')}"
            + ("  [演示]" if r.get("demo") else "")
            + ("" if r.get("trusted", True) else "  [不可采信]")
        )
    groups = d.get("task_groups") or []
    if groups:
        lines += ["", "同任务跨轮 Δ（按评委尺子分代 —— 不同尺的行不能读成一列）："]
        for g in groups:
            ci = g.get("delta_ci95") or []
            mark = "显著" if g.get("significant") else "不显著"
            head = f"· {str(g.get('task'))[:34]}…  Δ均值 {g.get('delta_mean')} {mark} n={g.get('n_runs')}"
            lines.append(head + (f" CI [{ci[0]}, {ci[1]}]" if len(ci) == 2 else ""))
            for ex in g.get("excluded_untrusted") or []:
                lines.append(f"    已排除崩臂 {ex.get('run_id')}（Δ {ex.get('delta')}）")
            if g.get("note"):
                lines.append(f"    {g['note']}")
    lines += ["", "Δ 是「相对基线臂」的自评分差，不是「更好」的证据；结论看盲评那一列。"]
    return lines


def render_calibrate(d: dict[str, Any]) -> list[str]:
    """⑤ 评委仪表：聚合读数 + 三句"这个数能不能信"。

    字段名全部对着 `run.py calibrate --aggregate --json` 的**当场输出**写
    （第一版我按想象的 `r`/`decision_agree` 写，界面直接印出三个 None ——
    字段名只能从命令输出抄，不能照文档或凭记忆）。
    """
    agg = d.get("aggregate") or d
    pooled = agg.get("pooled") or {}
    dec = pooled.get("decision") or {}
    disc = pooled.get("discrimination") or {}
    src = pooled.get("by_human_source") or {}
    owner, proxy = src.get("owner") or {}, src.get("ai_proxy") or {}
    lines = [
        f"评委 {agg.get('judge')} · 打分口径 {agg.get('mode')} · 聚合 {agg.get('rounds_used')} 轮"
        f"（复现轮 {agg.get('n_rep_rounds')}）",
        "",
        f"配对 {pooled.get('n_pairs')} / 锚点 {pooled.get('n_anchors')}"
        f"（亲判 {owner.get('n_anchors', 0)} · AI 代判 {proxy.get('n_anchors', 0)}）",
        f"MAE {pooled.get('mae')}{_fmt_ci(pooled.get('mae_ci95'))} · "
        f"bias {pooled.get('bias')}{_fmt_ci(pooled.get('bias_ci95'))}",
        f"过线一致率 {dec.get('agree')} · κ {dec.get('kappa')} · 偏松 {dec.get('n_lenient')} / "
        f"偏严 {dec.get('n_strict')}（线 {dec.get('line')}）",
        f"判别力 AUC {disc.get('auc')} · 平凡基线准确率 {disc.get('trivial_accuracy')} · "
        f"留一折净增益 {disc.get('loo_gain_vs_trivial')}",
        "",
    ]
    if dec.get("usable") is False:
        lines.append("⚠️ 过线一致性当前**不可估**（格子样本不足），别把上面那行读成「评委坏了」。")
    if disc.get("estimable") is False:
        lines.append("⚠️ 判别力不可估：AUC 的置信区间覆盖 0.5，这条线现在量不出东西。")
    if agg.get("papers_differ"):
        pi = agg.get("paper_identity") or {}
        lines.append(
            f"⚠️ 这几轮的考卷不是同一张（{pi.get('distinct_papers')} 张卷），聚合值别横比。"
        )
    lines += [
        "这是「评委相对人工锚点的偏差」，不是「评委能力」；样本 <5 条只作方向性参考。",
        "跑真复现（--repeat N）要打端点、花真钱，本窗口不提供一键烧钱。",
    ]
    rounds = agg.get("rounds") or []
    if rounds:
        lines += ["", "逐轮："]
        for r in rounds:
            lines.append(
                f"· {r.get('ts')}  n={r.get('n')}  MAE {r.get('mae')}  bias {r.get('bias')}  "
                f"κ {r.get('decision_kappa')}  复现极差 {r.get('rep_range_max')}"
            )
    disp = agg.get("dispersion") or {}
    if disp:
        bits = [f"{k} 极差 {v.get('range')}" for k, v in disp.items() if isinstance(v, dict)]
        lines.append("轮间离散：" + " · ".join(bits))
    return lines


def _fmt_ci(ci: Any) -> str:
    if not ci or len(ci) != 2:
        return ""
    return f" [{ci[0]}, {ci[1]}]"


def render_library(d: dict[str, Any]) -> list[str]:
    """⑤ 资产库：按任务相似度检索已达标提示词。"""
    recs = d.get("recommendations") or []
    lines = [f"任务「{str(d.get('task_text'))[:40]}」的相似资产 {len(recs)} 条", ""]
    for r in recs:
        lines.append(
            f"· {r.get('run_id')}  相似度 {r.get('similarity')}  自评分 {r.get('avg_score')}  "
            f"{r.get('prompt_chars')} 字"
        )
        lines.append(f"  {str(r.get('task'))[:110]}")
    if not recs:
        lines.append("库里没有相似任务（或还没有达标资产入库）。")
    lines += ["", "相似度是词面重合，不是语义等价；取用前自己读一遍。"]
    return lines


def render_gate(d: dict[str, Any]) -> list[str]:
    """⑤ 改动门禁：只拦本次新引入的规则问题（零调用）。"""
    lines = [
        "通过：没有新引入" if d.get("ok") else f"新引入 {d.get('introduced_total')} 条 —— CI 该红",
        f"基线 {str(d.get('base'))[:12]}（{d.get('base_how')}）· 对象 {'、'.join(d.get('paths') or [])}",
        f"模板 {d.get('unchanged_count')} 个未变 · 改动 {len(d.get('changed') or [])} 个 · "
        f"新增 {len(d.get('added_templates') or [])} · 删除 {len(d.get('removed_templates') or [])}",
    ]
    if d.get("skipped"):
        lines.append(f"本轮被跳过：{d.get('skip_reason')}")
    for note in d.get("notes") or []:
        lines.append(f"· {str(note)[:200]}")
    lines += ["", "判据是增量不是绝对：17 个模板里 16 个天然命中规则，绝对判据会永久假红。"]
    return lines


def render_preflight(stdout: str, code: int) -> list[str]:
    """⑤ 端点预检：它本来就是一张给人看的表，原样转过来，不在 UI 侧另编一套口径。"""
    head = f"退出码 {code}（0 = 全部角色可达）"
    return [head, "", *stdout.rstrip().splitlines()[-40:]]


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
        self.nb: Any = nb  # typeshed 里 ttk.Notebook.select 是无签名的，标成 Any 才不撞 strict
        # 顺序即 _tab_* 的添加顺序：--tab 靠它把键翻译成标签下标
        self.tab_keys = ("check", "cases", "diff", "run", "ledger")
        # ④/⑤ 要把界面上的正文与用例集落成文件再交给 CLI；生命周期跟着 App 走，
        # 不能在窗口开着时就删（子进程还在读它）。
        self.tmpdir = tempfile.TemporaryDirectory(prefix="pm-desktop-")
        self._tab_check(nb)
        self._tab_cases(nb)
        self._tab_diff(nb)
        self._tab_run(nb)
        self._tab_ledger(nb)
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

    def select_tab(self, key: str) -> None:
        """按 `--tab` 停在指定标签；未知键由 argparse 的 choices 挡住，这里不再兜底。"""
        self.nb.select(self.tab_keys.index(key))

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
            elif kind == "confirm":
                # 报价在 worker 线程跑的，确认框必须在主线程弹（label 里带 argv 的 JSON）
                self._confirm_run(json.loads(label), lines)
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
            text=f"{COST_NOTE}.\n{HONESTY_NOTE} —— 界面给的是自评分 Δ 与成对盲评，请看结论别只看分数。",
            style="Muted.TLabel",
            wraplength=700,
            justify="left",
        ).pack(anchor="w", pady=(10, 6))
        self.in_task = self._input(tab, 3)
        self.in_task.pack(fill="x")

        r1 = ttk.Frame(tab)
        r1.pack(fill="x", pady=(6, 0))
        ttk.Label(r1, text="目标模型").pack(side="left")
        self.model = ttk.Entry(r1, width=20)
        self.model.pack(side="left", padx=6)
        ttk.Label(r1, text="用例").pack(side="left")
        self.cases_n = ttk.Spinbox(r1, from_=1, to=8, width=4)
        self.cases_n.set("4")
        self.cases_n.pack(side="left", padx=4)
        ttk.Label(r1, text="最大迭代").pack(side="left")
        self.iter_n = ttk.Spinbox(r1, from_=0, to=10, width=4)
        self.iter_n.set("2")
        self.iter_n.pack(side="left", padx=4)
        ttk.Label(r1, text="采样").pack(side="left")
        self.samples_n = ttk.Spinbox(r1, from_=1, to=5, width=4)
        self.samples_n.set("2")
        self.samples_n.pack(side="left", padx=4)

        r2 = ttk.Frame(tab)
        r2.pack(fill="x", pady=(6, 0))
        ttk.Label(r2, text="用例集").pack(side="left")
        self.run_cases = ttk.Combobox(
            r2,
            width=30,
            state="readonly",
            values=[NO_CASES, PASTED] + [t["name"] for t in list_templates()],
        )
        self.run_cases.set(NO_CASES)
        self.run_cases.pack(side="left", padx=6)
        ttk.Label(r2, text="断言").pack(side="left")
        self.assert_mode = ttk.Combobox(
            r2, width=9, state="readonly", values=["contains", "exact", "regex", "rule"]
        )
        self.assert_mode.set("contains")
        self.assert_mode.pack(side="left", padx=4)
        self.seed_var = tk.BooleanVar(value=False)
        self.fast_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(r2, text="把①的正文当原稿改", variable=self.seed_var).pack(
            side="left", padx=(8, 0)
        )
        ttk.Checkbutton(r2, text="快速档", variable=self.fast_var).pack(side="left", padx=6)

        r3 = ttk.Frame(tab)
        r3.pack(fill="x", pady=(8, 0))
        ttk.Button(r3, text="先报价（零调用）", command=self.act_estimate).pack(side="left")
        ttk.Button(r3, text="端点预检（真调用）", command=self.act_preflight).pack(
            side="left", padx=6
        )
        ttk.Button(r3, text="提交任务", command=self.act_run).pack(side="left")
        ttk.Button(r3, text="取报告", command=self.act_report).pack(side="left", padx=6)
        self.panes["run"] = self._text_pane(tab, 8)

    # ---- 标签⑤：账本与仪表（四个按钮全零调用） ----
    def _tab_ledger(self, nb: ttk.Notebook) -> None:
        tab = ttk.Frame(nb)
        nb.add(tab, text="⑤ 账本与仪表")
        ttk.Label(
            tab,
            text="这四个按钮都不花一分钱：读的是本地账本与运行归档。\n"
            "它们回答的是「这套仪表现在可信吗」，不是「这次优化成功了吗」。",
            style="Muted.TLabel",
            wraplength=700,
            justify="left",
        ).pack(anchor="w", pady=(10, 6))
        row = ttk.Frame(tab)
        row.pack(fill="x")
        ttk.Button(row, text="历史与 Δ", command=self.act_history).pack(side="left")
        ttk.Button(row, text="评委仪表", command=self.act_calibrate).pack(side="left", padx=6)
        ttk.Button(row, text="资产库检索", command=self.act_library).pack(side="left")
        ttk.Button(row, text="改动门禁", command=self.act_gate).pack(side="left", padx=6)
        ttk.Label(row, text="历史条数").pack(side="left", padx=(10, 2))
        self.hist_n = ttk.Spinbox(row, from_=5, to=200, width=5)
        self.hist_n.set("40")
        self.hist_n.pack(side="left")
        self.in_libq = self._input(tab, 2)
        self.in_libq.pack(fill="x", pady=(8, 0))
        self.panes["ledger"] = self._text_pane(tab, 14)

    def act_history(self) -> None:
        self.ok_target = self.err_target = "ledger"
        argv = ["history", "--json", "--last", self._hist_last()]
        self._spawn("历史与 Δ", lambda: render_history(run_cli_json(argv)))

    def act_calibrate(self) -> None:
        self.ok_target = self.err_target = "ledger"
        argv = ["calibrate", "--aggregate", "--json"]
        self._spawn("评委仪表", lambda: render_calibrate(run_cli_json(argv)))

    def act_library(self) -> None:
        self.ok_target = self.err_target = "ledger"
        text = self.in_libq.get("1.0", "end").strip()
        if len(text) < 4:
            self._status("先在输入框写一句任务描述", BAD)
            return
        argv = ["library", "--recommend", "--task-text", text, "--json"]
        self._spawn("资产库检索", lambda: render_library(run_cli_json(argv)))

    def act_gate(self) -> None:
        self.ok_target = self.err_target = "ledger"
        self._spawn("改动门禁", lambda: render_gate(run_cli_json(["gate", "--json"])))

    def _hist_last(self) -> str:
        try:
            return str(max(5, min(200, int(self.hist_n.get()))))
        except ValueError:
            return "40"

    def _ints(self) -> tuple[int, int]:
        try:
            return int(self.cases_n.get()), int(self.iter_n.get())
        except ValueError:
            return 4, 2

    def _samples(self) -> int:
        try:
            return max(1, min(5, int(self.samples_n.get())))
        except ValueError:
            return 2

    def _tmp_write(self, name: str, text: str) -> str:
        """把界面上的正文落成文件交给 CLI —— 判据在 CLI 那边，这里不自己解析。"""
        f = Path(self.tmpdir.name) / name
        f.write_text(text, encoding="utf-8")
        return str(f)

    def _run_argv(self, extra: list[str]) -> list[str]:
        """**主线程**读 widget、拼参数（worker 线程不许摸 widget，见模块文档第 2 条）。

        两处刻意的"不传"：
        - 勾了快速档就不传 `--samples/--max-iter`：快速档自带单采样 1 轮，
          再传显式值等于两个来源抢同一个开关，读结果的人分不清哪个生效；
        - 没选用例集就不传 `--assert-mode`：它只对 `--cases-file` 的 expected 有意义。
        """
        task = self.in_task.get("1.0", "end").strip()
        cases, iters = self._ints()
        argv = [*extra, "--task", task, "--json", "--cases", str(cases)]
        if not self.fast_var.get():
            argv += ["--max-iter", str(iters), "--samples", str(self._samples())]
        model = self.model.get().strip()
        if model:
            argv += ["--target-model", model]
        if self.fast_var.get():
            argv.append("--fast")
        src = self.run_cases.get()
        if src == PASTED:
            raw = self.in_cases.get("1.0", "end").strip()
            if raw:
                argv += ["--cases-file", self._tmp_write("cases.json", raw)]
        elif src and src != NO_CASES:
            argv += ["--cases-file", str(template_path(src))]
        if "--cases-file" in argv:
            argv += ["--assert-mode", self.assert_mode.get()]
        if self.seed_var.get():
            seed = self.in_check.get("1.0", "end").rstrip()
            if seed:
                argv += ["--prompt-file", self._tmp_write("seed.md", seed)]
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
        argv = self._run_argv(["--dry-run"])
        self._spawn("预算估计", lambda: render_dry_run(run_cli_json(argv)))

    def act_run(self) -> None:
        """提交前先**当场报价**：确认框里写的是这次参数下的预估区间，不是固定文案。

        报价本身也是一次 CLI 调用，所以在 worker 线程跑；确认框回主线程弹
        （`_drain` 的 "confirm" 分支）。报价失败不拦提交，但会在框里明说。
        """
        self.ok_target = self.err_target = "run"
        if not self._need_task():
            return
        if not run_available():
            self._status("没配 Key：④不可用（①②③⑤不受影响）", BAD)
            return
        argv = self._run_argv([])
        quote_argv = [*argv, "--dry-run"]

        def runner() -> None:
            try:
                try:
                    lines = render_dry_run(run_cli_json(quote_argv))
                except (ForwardError, TemplateNotFound, OSError, ValueError) as e:
                    lines = [f"报价失败：{e}", COST_NOTE]
                self.q.put(("confirm", json.dumps(argv, ensure_ascii=False), lines))
            except Exception as e:  # noqa: BLE001 - 后台线程不许把异常丢给解释器
                self.q.put(("err", "预算估计", [f"{type(e).__name__}: {e}"]))

        self._status("提交前先报价…")
        threading.Thread(target=runner, daemon=True, name="pm-desktop-quote").start()

    def _confirm_run(self, argv: list[str], quote: list[str]) -> None:
        body = "\n".join(quote)
        if not messagebox.askyesno("确认提交", f"{body}\n\n{HONESTY_NOTE}。\n\n现在提交这一轮？"):
            self._status("已取消，未产生任何调用")
            return

        def work() -> list[str]:
            d = run_cli_json(argv, timeout=LONG_RUN_TIMEOUT)
            self.last_run_id = str(d.get("run_id") or "")
            return render_run(d)

        self._spawn("优化闭环", work)

    def act_preflight(self) -> None:
        """逐角色冒烟：真调用（约 9 发），所以也要确认。"""
        self.ok_target = self.err_target = "run"
        if not run_available():
            self._status("没配 Key：预检没有意义", BAD)
            return
        if not messagebox.askyesno("确认预检", "逐角色打一次真实端点（约 9 次调用），现在跑？"):
            self._status("已取消，未产生任何调用")
            return
        self._spawn(
            "端点预检",
            lambda: render_preflight(*run_cli_text(["--preflight"], timeout=PREFLIGHT_TIMEOUT)),
        )

    def act_report(self) -> None:
        self.ok_target = self.err_target = "run"
        if not self.last_run_id:
            self._status("还没有 run_id（先提交一轮）", BAD)
            return
        self._spawn(
            "取报告",
            lambda: [str(run_cli_json(["report", self.last_run_id]).get("report", ""))],
        )

    def _fill(self, w: tk.Text, text: str) -> None:
        w.delete("1.0", "end")
        w.insert("1.0", text)


def _build_parser() -> argparse.ArgumentParser:
    """每个子命令都要有原地 `--help`（本仓约定，且 CI 有守卫）。

    这条不是形式：第一版 `app_main` 直接忽略 argv，实测 `run.py desktop --help`
    **开出一个窗口并卡在 mainloop**，rc=124、stdout 0 字节 —— 用户以为命令坏了。
    """
    prog = Path(sys.argv[0]).name
    p = argparse.ArgumentParser(
        prog=f"{prog} desktop",
        description="PromptMaster 原生桌面窗口（Tkinter）：体检 / 考卷有几票 / 两版差分 / 优化闭环",
        epilog="零调用三张卡不需要 Key；④要 Key 或 PM_FAKE_BACKEND。窗口只在本机，不开任何端口。",
    )
    p.add_argument(
        "--tab",
        choices=("check", "cases", "diff", "run"),
        default="check",
        help="启动后停在哪个标签（①体检 / ②考卷 / ③差分 / ④闭环）",
    )
    return p


def app_main(argv: list[str] | None = None) -> int:
    """入口：解析参数 → 建窗口 → 进主循环。Tk 必须在主线程，所以没有 --workers 这类参数。"""
    args = _build_parser().parse_args(sys.argv[2:] if argv is None else argv)
    root = tk.Tk()
    app = App(root)
    app.select_tab(args.tab)
    root.mainloop()
    return 0
