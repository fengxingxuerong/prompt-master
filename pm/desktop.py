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
import time
import tkinter as tk
from collections.abc import Callable, Iterator
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any

from pm import __version__ as PKG_VERSION
from pm.cli.support import _result_exit_code
from pm.cliforward import (
    ROOT as REPO_ROOT,
)
from pm.cliforward import (
    ForwardError,
    TemplateNotFound,
    list_templates,
    run_cli_json,
    run_cli_json_live,
    run_cli_text,
    template_path,
)

APP_TITLE = "PromptMaster · 桌面面板"
WIN_GEOMETRY = "780x660"
MIN_GEOMETRY = (720, 560)  # 小于这个尺寸控件会被挤到看不见，别让它缩
BG = "#171a21"
CARD = "#1f2430"
INK = "#e6e9ef"
MUTED = "#8b94a7"
OK = "#3fb27f"
BAD = "#e05c5c"
ACCENT = "#5b8cff"
MONO = ("Consolas", 10)

# 换肤靠改这几个模块级名字 + `_retheme()` 走一遍已建好的 widget。
# 为什么不做成"重建整个界面"：那会把用户已经贴进去的正文一起丢掉。
THEME = "dark"


def apply_theme(name: str) -> str:
    """切色板。改的是模块级那几个颜色名 —— 已建好的 widget 由 `_retheme()` 补一遍。"""
    global THEME
    pal = PALETTES.get(name) or PALETTES["dark"]
    g = globals()
    for k, v in pal.items():
        g[k] = v
    THEME = name
    return name


PALETTES: dict[str, dict[str, str]] = {
    "dark": {
        "BG": "#171a21",
        "CARD": "#1f2430",
        "INK": "#e6e9ef",
        "MUTED": "#8b94a7",
        "OK": "#3fb27f",
        "BAD": "#e05c5c",
        "ACCENT": "#5b8cff",
    },
    "light": {
        "BG": "#f5f6f8",
        "CARD": "#ffffff",
        "INK": "#1a1d24",
        "MUTED": "#6b7385",
        "OK": "#1f8a5b",
        "BAD": "#c0392b",
        "ACCENT": "#2f6bd6",
    },
}

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


# ---------------------------------------------------------------------------
# 下面四个是 UI 侧的纯文本处理：**不做任何判定**。
# 判定只有一处来源（`run.py` 的 check/diff/…），这里只做"拆列、过滤、汇总、列举"。
# ---------------------------------------------------------------------------
def split_diff(unified: str) -> list[tuple[str, str, str]]:
    """把 unified diff 拆成 (状态, 左行, 右行)，给并排视图与逐行着色用。

    状态 ∈ {"hdr", "ctx", "add", "del"}。`---`/`+++` 那两行**不进视图**：它们带的是
    宿主临时目录的真实路径（截图里露出过 `C:\\Users\\...\\Temp\\a.md`），而"改动前/改动后"
    两个列头已经把这件事说完了。`@@` 归 hdr —— 那是 hunk 坐标，是有用的。
    """
    rows: list[tuple[str, str, str]] = []
    raw = (unified or "").split("\n")
    # 首尾的空行是 `"".split("\n") == [""]` 造出来的，不是 diff 的内容：留着会让空输入
    # 也画出一行"上下文"，用户看到的就是一条不存在的相同行。
    while raw and not raw[0]:
        raw.pop(0)
    while raw and not raw[-1]:
        raw.pop()
    for line in raw:
        if line.startswith("---") or line.startswith("+++"):
            continue
        if line.startswith("@@"):
            rows.append(("hdr", line, ""))
        elif line.startswith("+"):
            rows.append(("add", "", line[1:]))
        elif line.startswith("-"):
            rows.append(("del", line[1:], ""))
        else:
            rows.append(
                (
                    "ctx",
                    line[1:] if line.startswith(" ") else line,
                    line[1:] if line.startswith(" ") else line,
                )
            )
    return rows


def list_placeholders(text: str) -> list[tuple[int, str]]:
    """列举 `{{变量}}` 占位符（行号, 名字）。

    ⚠️ **只列不判**：`pm/quality.py` 里没有"变量未填"这条规则，判据不在这加。
    列出来是因为竞品（prompt-optimizer / prompt-forge）都把变量当一等公民，
    而一份带 `{{...}}` 的提示词直接喂给模型是最常见的自伤 —— 让人自己看见。
    """
    out: list[tuple[int, str]] = []
    for i, line in enumerate((text or "").splitlines(), start=1):
        rest = line
        while True:
            a = rest.find("{{")
            if a < 0:
                break
            b = rest.find("}}", a + 2)
            if b < 0:
                break
            name = rest[a + 2 : b].strip()
            if name:
                out.append((i, name))
            rest = rest[b + 2 :]
    return out


def render_placeholders(text: str) -> list[str]:
    """① 窗格顶部的占位符提示。**没有占位符就一行不出**，别制造噪音。"""
    found = list_placeholders(text)
    if not found:
        return []
    by_name: dict[str, list[int]] = {}
    for line, name in found:
        lines = by_name.setdefault(name, [])
        if line not in lines:
            lines.append(line)
    body = "  ".join(
        f"{name}→第 {'/'.join(str(x) for x in sorted(lines))} 行" for name, lines in by_name.items()
    )
    return [f"占位符（只列不判，填没填由你决定）：{body}", ""]


def filter_lines(lines: list[str], needle: str) -> list[str]:
    """⑤ 历史过滤：大小写不敏感的子串匹配；空串原样返回（不猜用户想筛什么）。"""
    if not needle:
        return lines
    low = needle.lower()
    return [ln for ln in lines if low in ln.lower()]


def render_export(panes: dict[str, str], meta: dict[str, str]) -> list[str]:
    """把各窗格汇成一份 Markdown —— 竞品都有的 export，这里不新增任何判断。"""
    lines = ["# PromptMaster 桌面端读数", ""]
    for k, v in meta.items():
        lines.append(f"- {k}：{v}")
    lines.append("")
    for title, body in panes.items():
        lines += [f"## {title}", "", body.strip() or "（空）", ""]
    return lines


def origin_label() -> str:
    """底栏那句"读数来自哪"。整条路径在 780px 宽下会被裁掉尾巴（截图实测只剩 `projects\\pror`），
    所以过长时保两端：盘符 + 仓库目录名。版本永远在最前 —— 它才是判据的代际。"""
    path = str(REPO_ROOT)
    short = path if len(path) <= 26 else f"{path[:2]}{os.sep}…{os.sep}{Path(path).name}"
    return f"v{PKG_VERSION} · {short}"


def run_available() -> bool:
    """能不能跑闭环：有 `PM_FAKE_BACKEND` 或有 Key。晚绑定现读，别缓存（Key 可以运行时补）。"""
    if (os.getenv("PM_FAKE_BACKEND") or "").strip():
        return True
    return bool((os.getenv("PM_API_KEY") or "").strip() or (os.getenv("PM_API_KEYS") or "").strip())


class App:
    """主窗口：四个标签 + 底部状态灯。耗时动作一律 worker 线程 + 队列回投。"""

    def __init__(self, root: tk.Tk, scale: float | None = None) -> None:
        self.root = root
        # 队列项 = (种类, 标签/载荷, 要显示的行, **结果该落进哪个窗格**)。
        # 窗格必须在**派发时**定死：早先版本回投时才读 self.ok_target，
        # 于是"先点①（慢）再点⑤（快）"会把①的结果画进⑤的窗格里 —— 串台且没有任何报错。
        self.q: queue.Queue[tuple[str, str, list[str], str]] = queue.Queue()
        self.panes: dict[str, tk.Text] = {}
        self.last_run_id = ""
        self.prev_run_id = ""
        self.last_prompt = ""
        self.prev_prompt = ""
        self._diff_rows: list[tuple[str, str, str]] = []
        self._pending_diff: list[tuple[str, str, str]] | None = None
        self._ledger_raw: list[str] = []
        self.run_proc: Any = None  # ④那一轮的真子进程；非 None = 正在烧钱
        self.inflight = 0
        self.aborted = False
        root.title(APP_TITLE)
        root.geometry(WIN_GEOMETRY)
        root.minsize(*MIN_GEOMETRY)
        root.configure(bg=BG)
        if scale:
            # 高 DPI 屏上 Tk 的默认换算有时偏小；给一个显式出口而不是猜系统 DPI
            root.tk.call("tk", "scaling", float(scale))
        self._style()
        self._header()
        self._footer()  # 底栏先占位，见 `_footer` 里那条 `side="bottom"` 的注释
        nb = ttk.Notebook(root)
        nb.pack(fill="both", expand=True, padx=12, pady=(0, 6))
        self.nb: Any = nb  # typeshed 里 ttk.Notebook.select 无签名，标 Any 才不撞 strict
        # 顺序即 _tab_* 的添加顺序：--tab 与键盘 Alt+N 都靠它
        self.tab_keys = ("check", "cases", "diff", "run", "ledger")
        # ④/⑤ 要把界面上的正文与用例集落成文件再交给 CLI；生命周期跟着窗口走，
        # 关窗时显式清（只靠 TemporaryDirectory 的 finalizer 会在子进程还在读时炸）。
        self.tmpdir = tempfile.TemporaryDirectory(prefix="pm-desktop-")
        self._tab_check(nb)
        self._tab_cases(nb)
        self._tab_diff(nb)
        self._tab_run(nb)
        self._tab_ledger(nb)
        self._keys()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._probe_async()
        root.after(120, self._drain)

    def _keys(self) -> None:
        """键盘路径：Alt+1..5 切标签，Ctrl+Enter 跑当前标签的主动作。

        为什么不是 Enter：三个标签的主输入框是多行 Text，Enter 必须是换行。
        """

        def binder(k: str) -> Callable[[object], None]:
            def handler(_event: object) -> None:
                self.select_tab(k)

            return handler

        for idx, key in enumerate(self.tab_keys, start=1):
            self.root.bind(f"<Alt-{idx}>", binder(key))
        self.root.bind("<Control-Return>", lambda _e: self._run_active_action())
        self.root.bind("<Control-s>", lambda _e: self.act_save_pane())
        self.root.bind("<Control-l>", lambda _e: self.act_clear_pane())
        self.root.bind("<F1>", lambda _e: self.act_help())

    def _run_active_action(self) -> None:
        try:
            current = self.tab_keys[int(self.nb.index("current"))]
        except (TypeError, ValueError, IndexError):  # pragma: no cover - 索引只会是 0..4
            return
        {
            "check": self.act_check,
            "cases": self.act_cases_tpl,
            "diff": self.act_diff,
            "run": self.act_run,
            "ledger": self.act_history,
        }[current]()

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
        # 常驻"这份读数出自哪份代码"：版本 + 仓库路径。同一份读数在两台机器上可能来自
        # 不同代码，这是本产品一贯要暴露的东西。放表头而不是底栏 —— 底栏左右两组挤在一起
        # 时它第一个被裁（截图实测只剩 `projects\pror`），而表头右侧一直是空的。
        self.env_lbl = tk.Label(top, text=origin_label(), bg=BG, fg=MUTED, font=("Segoe UI", 8))
        self.env_lbl.pack(side="right")

    def _footer(self) -> None:
        bar = ttk.Frame(self.root)
        # `side="bottom"` + **先于笔记本 pack**：底部条原本在笔记本之后 pack，
        # 而笔记本是 `expand=True` ⇒ 窗口一矮，整条底栏被挤出可视区（截图实测：
        # 灯、导出、主题、帮助、版本号全都不在画面里，而单测照样绿，因为它直接调方法）。
        bar.pack(side="bottom", fill="x", padx=14, pady=(0, 10))
        self.led_cli = tk.Label(bar, text="转发检测中…", bg=BG, fg=MUTED, font=("Segoe UI", 9))
        self.led_cli.pack(side="left")
        self.led_key = tk.Label(bar, text="", bg=BG, fg=MUTED, font=("Segoe UI", 9))
        self.led_key.pack(side="left", padx=(14, 0))
        self.status_lbl = tk.Label(bar, text="", bg=BG, fg=MUTED, font=("Segoe UI", 9))
        self.status_lbl.pack(side="left", padx=12)
        ttk.Button(bar, text="中止", width=6, command=self.act_abort).pack(side="right")
        ttk.Button(bar, text="导出全部", width=9, command=self.act_export_all).pack(
            side="right", padx=6
        )
        ttk.Button(bar, text="主题", width=6, command=self.act_theme).pack(side="right", padx=6)
        ttk.Button(bar, text="帮助 F1", width=8, command=self.act_help).pack(side="right", padx=6)
        ttk.Button(bar, text="另存结果…", width=10, command=self.act_save_pane).pack(
            side="right", padx=6
        )
        ttk.Button(bar, text="复制结果", width=9, command=self.act_copy_pane).pack(side="right")

    def select_tab(self, key: str) -> None:
        """按 `--tab` 停在指定标签；未知键由 argparse 的 choices 挡住，这里不再兜底。"""
        self.nb.select(self.tab_keys.index(key))

    def _led(self, w: tk.Label, text: str, good: bool) -> None:
        w.configure(text=("● " if good else "× ") + text, fg=OK if good else BAD)

    # ---- 线程调度 ----
    def _spawn(self, label: str, work: Callable[[], list[str]], target: str) -> None:
        """把耗时动作丢进守护线程；**窗格在派发时定死**，回投只认队列里那份。"""

        def runner() -> None:
            t0 = time.monotonic()
            try:
                lines = work()
                self.q.put(("ok", f"{label} · {time.monotonic() - t0:.1f}s", lines, target))
            except (ForwardError, TemplateNotFound, OSError, ValueError) as e:
                self.q.put(("err", label, [str(e)], target))
            except Exception as e:  # noqa: BLE001 - 兜底必须宽：静默死线程比报错难查得多
                # 真事故形状（2026-10-14）：只捕四类异常时，渲染函数里一个 TypeError
                # 就把线程带走，界面永远停在"进行中…"，用户只能关窗口猜。
                self.q.put(
                    (
                        "err",
                        label,
                        [f"{type(e).__name__}: {e}", "（这不是判据失败，是界面自己的错）"],
                        target,
                    )
                )

        self.inflight += 1
        self._status(label + " 进行中…")
        threading.Thread(target=runner, daemon=True, name=f"pm-desktop-{label}").start()

    def _drain(self) -> None:
        drained = False
        while True:
            try:
                kind, label, lines, target = self.q.get_nowait()
            except queue.Empty:
                break
            drained = True
            if kind == "led":
                self._apply_probe(lines)
            elif kind == "confirm":
                # 报价在 worker 线程跑的，确认框必须回主线程弹（label 里带 argv 的 JSON）
                self._confirm_run(json.loads(label), lines)
            else:
                self.inflight = max(0, self.inflight - 1)
                if kind == "err":
                    self._status(label + " 失败", BAD)
                else:
                    self._status(label + " 完成", OK)
                self._show(target, lines)
                # 并排视图的绘制留到这里：主线程
                if self._pending_diff is not None:
                    self._paint_diff(self._pending_diff)
                    self._pending_diff = None
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
            # led 不落脚格，target 位留空
            self.q.put(
                ("led", "probe", ["1" if ok else "0", "1" if run_available() else "0", note], "")
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
            # 原本这句 27 字，窄窗口下右端被裁成"…能不能完成任务"半截 —— 裁掉的字恰好是重点
            text="零调用、不联网；只答「会不会出事」",
            style="Muted.TLabel",
        ).pack(side="right")
        self.panes["check"] = self._text_pane(tab, 12)

    def act_check(self) -> None:
        text = self.in_check.get("1.0", "end").rstrip()
        if not text:
            self._status("先把要体检的提示词贴进来", BAD)
            return
        self._spawn(
            "体检",
            lambda: [
                *render_placeholders(text),
                *render_check(run_cli_json(["check", "--prompt", text, "--json"])),
            ],
            target="check",
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
        if not name:
            self._status("先选一个模板", BAD)
            return
        self._spawn(
            "考卷体检",
            lambda: render_cases(
                run_cli_json(["check", "--cases-file", str(template_path(name)), "--json"])
            ),
            target="cases",
        )

    def act_cases_paste(self) -> None:
        raw = self.in_cases.get("1.0", "end").strip()
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

        self._spawn("考卷体检", work, target="cases")

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
        ttk.Button(row, text="按 run_id 差分", command=self.act_diff_runs).pack(side="left", padx=6)
        # 贴左而不是贴右：原本挤在行尾，最后一个字被窗口右边缘切掉半截
        ttk.Label(row, text="零调用 · 只拦新引入", style="Muted.TLabel").pack(
            side="left", padx=(12, 0)
        )
        r4 = ttk.Frame(tab)
        r4.pack(fill="x", pady=(6, 0))
        ttk.Label(r4, text="run A").pack(side="left")
        self.diff_run_a = ttk.Entry(r4, width=16)
        self.diff_run_a.pack(side="left", padx=4)
        ttk.Label(r4, text="run B", style="Muted.TLabel").pack(side="left", padx=(8, 0))
        self.diff_run_b = ttk.Entry(r4, width=16)
        self.diff_run_b.pack(side="left", padx=4)
        ttk.Label(r4, text="（④跑完会自动填进来）", style="Muted.TLabel").pack(side="left")
        # 并排视图：竞品（promptfoo 的 side-by-side、forge 的 diff）第一眼的形状。
        # 判据仍是 CLI 的 diff --json，这里只把 unified 拆成左右两列着色。
        # 高度是量出来的：原本 9 行 + 摘要 7 行，在默认 660px 高的窗口里把摘要挤成了
        # 半行（截图实测），改小之后两边都完整。
        self.diff_view: Any = ttk.Treeview(tab, columns=("l", "r"), show="headings", height=6)
        self.diff_view.heading("l", text="改动前")
        self.diff_view.heading("r", text="改动后")
        self.diff_view.column("l", width=330, anchor="w")
        self.diff_view.column("r", width=330, anchor="w")
        self.diff_view.pack(fill="x", pady=(8, 0))
        self._diff_tags(self.diff_view)
        self.panes["diff"] = self._text_pane(tab, 5)

    def act_fill_a(self) -> None:
        self._fill(self.in_a, self.in_check.get("1.0", "end").rstrip())

    def act_diff(self) -> None:
        a = self.in_a.get("1.0", "end").rstrip()
        b = self.in_b.get("1.0", "end").rstrip()
        if not a or not b:
            self._status("两版都要填", BAD)
            return

        def work() -> list[str]:
            with tempfile.TemporaryDirectory() as d:
                pa, pb = Path(d) / "a.md", Path(d) / "b.md"
                pa.write_text(a, encoding="utf-8")
                pb.write_text(b, encoding="utf-8")
                return self._diff_work(run_cli_json(["diff", str(pa), str(pb), "--json"]))

        self._spawn("差分", work, target="diff")

    def act_diff_runs(self) -> None:
        """按两个 run_id 差分：`run.py diff <A> <B>` 本来就吃 run_id，界面别只给文件。"""
        a, b = self.diff_run_a.get().strip(), self.diff_run_b.get().strip()
        if not a or not b:
            self._status("两个 run_id 都要填", BAD)
            return
        self._spawn(
            "差分（run 对比）",
            lambda: self._diff_work(run_cli_json(["diff", a, b, "--json"])),
            target="diff",
        )

    def _diff_work(self, d: dict[str, Any]) -> list[str]:
        """worker 线程里**只算不画**：算好并排行数据，交给主线程去刷 Treeview。

        这条不是洁癖 —— 第一版在这里直接调 `diff_view.insert()`，实测抛
        `RuntimeError: main thread is not in main loop`（上一轮加的"异常必须显示出来"
        把它照了出来，否则界面只会停在"进行中…"）。
        """
        self._pending_diff = split_diff(str(d.get("unified") or ""))
        return render_diff(d)

    def _paint_diff(self, rows: list[tuple[str, str, str]]) -> None:
        view = self.diff_view
        for iid in view.get_children():
            view.delete(iid)
        for kind, left, right in rows:
            view.insert("", "end", values=(left, right), tags=(kind,))
        self._diff_rows = rows

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
        r4 = ttk.Frame(tab)
        r4.pack(fill="x", pady=(6, 0))
        ttk.Button(r4, text="取用交付提示词 → ①", command=self.act_take_prompt).pack(side="left")
        ttk.Button(r4, text="把它填到③的「后」", command=self.act_prompt_to_diff_b).pack(
            side="left", padx=6
        )
        ttk.Label(r4, text="跑完才有内容", style="Muted.TLabel").pack(side="left")
        self.panes["run"] = self._text_pane(tab, 7)

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
        row2 = ttk.Frame(tab)
        row2.pack(fill="x", pady=(8, 0))
        ttk.Label(row2, text="过滤").pack(side="left")
        self.hist_filter = ttk.Entry(row2, width=26)
        self.hist_filter.pack(side="left", padx=6)
        ttk.Button(row2, text="应用过滤", command=self.act_filter_ledger).pack(side="left")
        ttk.Label(row2, text="取 run_id 进③").pack(side="left", padx=(14, 2))
        self.ledger_run = ttk.Entry(row2, width=16)
        self.ledger_run.pack(side="left", padx=4)
        ttk.Button(row2, text="填入", command=self.act_run_to_diff).pack(side="left")
        self.in_libq = self._input(tab, 2)
        self.in_libq.pack(fill="x", pady=(8, 0))
        self.panes["ledger"] = self._text_pane(tab, 12)

    def _remember(self, key: str, lines: list[str]) -> list[str]:
        """留一份未过滤的原文：过滤永远是可回退的视图，不是覆盖。"""
        if key == "ledger":
            self._ledger_raw = lines
        return lines

    def act_filter_ledger(self) -> None:
        """过滤当前窗格已有的行（纯文本操作：不重跑 CLI、更不改判据）。"""
        if not self._ledger_raw:
            self._status("先点一个按钮拿读数")
            return
        needle = self.hist_filter.get().strip()
        if not needle:
            self._show("ledger", self._ledger_raw)
            self._status("已取消过滤", OK)
            return
        kept = filter_lines(self._ledger_raw, needle)
        # 末行是给 Δ 的口径说明。子串过滤会把它一起筛掉 —— 而那正是最危险的读法：
        # 满屏只剩分数，没有"Δ 不是更好的证据"这句。固定尾巴跟着过滤后的行重新贴上。
        note = self._ledger_raw[-1]
        tail = [] if note in kept else ["", note]
        head = f"过滤「{needle}」：{len(kept)} 行（原文 {len(self._ledger_raw)} 行）"
        self._show("ledger", [head, "", *(kept or ["（没有匹配的行）"]), *tail])
        self._status(f"过滤后 {len(kept)} 行", OK)

    def act_run_to_diff(self) -> None:
        """历史里看中的那一轮直接进③当"改动前"——竞品都有这步跳转，原来要手抄。"""
        rid = self.ledger_run.get().strip()
        if not rid:
            self._status("先填一个 run_id", BAD)
            return
        self.nb.select(2)
        self._set_entry(self.diff_run_a, rid)
        self._status(f"run {rid} 已填进③的「前」", OK)

    def act_history(self) -> None:
        argv = ["history", "--json", "--last", self._hist_last()]
        self._spawn(
            "历史与 Δ",
            lambda: self._remember("ledger", render_history(run_cli_json(argv))),
            target="ledger",
        )

    def act_calibrate(self) -> None:
        argv = ["calibrate", "--aggregate", "--json"]
        self._spawn(
            "评委仪表",
            lambda: self._remember("ledger", render_calibrate(run_cli_json(argv))),
            target="ledger",
        )

    def act_library(self) -> None:
        text = self.in_libq.get("1.0", "end").strip()
        if len(text) < 4:
            self._status("先在输入框写一句任务描述", BAD)
            return
        argv = ["library", "--recommend", "--task-text", text, "--json"]
        self._spawn(
            "资产库检索",
            lambda: self._remember("ledger", render_library(run_cli_json(argv))),
            target="ledger",
        )

    def act_gate(self) -> None:
        self._spawn(
            "改动门禁",
            lambda: self._remember("ledger", render_gate(run_cli_json(["gate", "--json"]))),
            target="ledger",
        )

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
        if not self._need_task():
            return
        argv = self._run_argv(["--dry-run"])
        self._spawn("预算估计", lambda: render_dry_run(run_cli_json(argv)), target="run")

    def act_run(self) -> None:
        """提交前先**当场报价**：确认框里写的是这次参数下的预估区间，不是固定文案。

        报价本身也是一次 CLI 调用，所以在 worker 线程跑；确认框回主线程弹
        （`_drain` 的 "confirm" 分支）。报价失败不拦提交，但会在框里明说。
        """
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
                self.q.put(("confirm", json.dumps(argv, ensure_ascii=False), lines, "run"))
            except Exception as e:  # noqa: BLE001 - 后台线程不许把异常丢给解释器
                self.q.put(("err", "预算估计", [f"{type(e).__name__}: {e}"], "run"))

        self._status("提交前先报价…")
        threading.Thread(target=runner, daemon=True, name="pm-desktop-quote").start()

    def _confirm_run(self, argv: list[str], quote: list[str]) -> None:
        body = "\n".join(quote)
        if not messagebox.askyesno("确认提交", f"{body}\n\n{HONESTY_NOTE}。\n\n现在提交这一轮？"):
            self._status("已取消，未产生任何调用")
            return

        if self.run_proc is not None:
            self._status("已经有一轮在跑了 —— 先中止它", BAD)
            return

        def work() -> list[str]:
            def register(proc: Any) -> None:
                self.run_proc = proc  # 只存句柄，不碰 widget

            d = run_cli_json_live(argv, register, timeout=LONG_RUN_TIMEOUT)
            # 只存普通变量，不碰 widget（线程安全那条）
            self.prev_run_id, self.prev_prompt = self.last_run_id, self.last_prompt
            self.last_run_id = str(d.get("run_id") or "")
            self.last_prompt = str(d.get("best_prompt") or "")
            return render_run(d)

        self._spawn("优化闭环", work, target="run")

    def act_preflight(self) -> None:
        """逐角色冒烟：真调用（约 9 发），所以也要确认。"""
        if not run_available():
            self._status("没配 Key：预检没有意义", BAD)
            return
        if not messagebox.askyesno("确认预检", "逐角色打一次真实端点（约 9 次调用），现在跑？"):
            self._status("已取消，未产生任何调用")
            return
        self._spawn(
            "端点预检",
            lambda: render_preflight(*run_cli_text(["--preflight"], timeout=PREFLIGHT_TIMEOUT)),
            target="run",
        )

    @staticmethod
    def _set_entry(w: Any, text: str) -> None:
        w.delete(0, "end")
        w.insert(0, text)

    def _diff_tags(self, view: Any) -> None:
        """并排视图的行色：加=绿、删=红、头=灰。只是着色，不改内容。"""
        view.tag_configure(
            "add", background="#123126" if THEME == "dark" else "#e6f6ee", foreground=OK
        )
        view.tag_configure(
            "del", background="#3a1d1d" if THEME == "dark" else "#fdeaea", foreground=BAD
        )
        view.tag_configure("hdr", foreground=MUTED)
        view.tag_configure("ctx", background=CARD, foreground=INK)

    def act_take_prompt(self) -> None:
        """把这一轮交付的提示词填回①：竞品都有"取用结果"这一步，我们原来只能手抄。"""
        if not self.last_prompt:
            self._status("还没有交付的提示词（先跑一轮，或从⑤取历史）", BAD)
            return
        self.nb.select(0)
        self._fill(self.in_check, self.last_prompt)
        self._status(f"已取用 {len(self.last_prompt)} 字到①", OK)

    def act_prompt_to_diff_b(self) -> None:
        if not self.last_prompt:
            self._status("还没有交付的提示词", BAD)
            return
        self.nb.select(2)
        self._fill(self.in_b, self.last_prompt)
        if self.prev_prompt:
            self._fill(self.in_a, self.prev_prompt)
        if self.prev_run_id and self.last_run_id:
            self._set_entry(self.diff_run_a, self.prev_run_id)
            self._set_entry(self.diff_run_b, self.last_run_id)
        self._status("已填③：点「差分」看这一改新引入了什么", OK)

    def act_abort(self) -> None:
        """中止④那一轮：子进程还在打真端点，每分钟都在花钱。"""
        proc = self.run_proc
        if proc is None:
            self._status("没有在跑的任务")
            return
        self.aborted = True
        try:
            proc.terminate()
            self._status("已请求中止，等它收尾…", BAD)
        except OSError as e:  # pragma: no cover - terminate 失败极罕见
            self._status(f"中止失败：{e}", BAD)

    def _current_pane_key(self) -> str:
        try:
            return self.tab_keys[int(self.nb.index("current"))]
        except (TypeError, ValueError, IndexError):
            return "check"

    def act_theme(self) -> None:
        """深/浅切换。**不重建界面** —— 重建会把用户贴进去的正文一起丢掉。"""
        apply_theme("light" if THEME == "dark" else "dark")
        self._retheme()
        self._status(f"主题：{THEME}", OK)

    def _retheme(self) -> None:
        self._style()
        for w in self._descendants(self.root):
            try:
                cls = w.winfo_class()
            except Exception:  # noqa: BLE001 - 窗口正在销毁时问类名会抛
                continue
            if cls == "Text":
                w.configure(bg=CARD, fg=INK, insertbackground=INK)
            elif cls == "Label":
                # 只换底色：前景色留给各条自己的语义（绿=可用、红=坏了），别在换肤时抹平
                w.configure(bg=BG)
            elif cls == "Frame":
                w.configure(bg=BG)
        view = getattr(self, "diff_view", None)
        if view is not None:
            self._diff_tags(view)

    @staticmethod
    def _descendants(w: Any) -> Iterator[Any]:
        for c in w.winfo_children():
            yield from App._descendants(c)
            yield c

    def act_export_all(self) -> None:
        """把四个窗格汇成一份 Markdown 存盘（竞品都有的 export，这里不新增判断）。"""
        panes = {
            "① 提示词体检": str(self.panes["check"].get("1.0", "end")),
            "② 考卷有几票": str(self.panes["cases"].get("1.0", "end")),
            "③ 两版差分": str(self.panes["diff"].get("1.0", "end")),
            "④ 优化闭环": str(self.panes["run"].get("1.0", "end")),
            "⑤ 账本与仪表": str(self.panes["ledger"].get("1.0", "end")),
        }
        meta = {
            "版本": PKG_VERSION,
            "仓库": str(REPO_ROOT),
            "最近 run_id": self.last_run_id or "（无）",
            "导出时间": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        path = filedialog.asksaveasfilename(
            title="导出全部读数",
            defaultextension=".md",
            filetypes=[("Markdown", "*.md"), ("文本", "*.txt")],
        )
        if not path:
            return
        try:
            Path(path).write_text("\n".join(render_export(panes, meta)) + "\n", encoding="utf-8")
        except OSError as e:
            self._status(f"写不出去：{e}", BAD)
            return
        self._status(f"已导出 {path}", OK)

    def act_help(self) -> None:
        messagebox.showinfo(
            "PromptMaster 桌面面板",
            "Alt+1..5 切标签 · Ctrl+Enter 跑当前标签 · Ctrl+S 另存窗格 · Ctrl+L 清空窗格\n\n"
            "①②③ 零调用不花钱；④ 要 Key，提交前会先报价。\n"
            f"所有判据都来自同一份 CLI（{REPO_ROOT / 'run.py'}），界面不另立一套。\n\n"
            + HONESTY_NOTE,
        )

    def act_clear_pane(self) -> None:
        key = self._current_pane_key()
        self._show(key, [])
        self._status(f"已清空 {key}")

    def act_copy_pane(self) -> None:
        """把当前窗格的文本塞进剪贴板：结果常常要贴进别处，选中文本复制太慢。"""
        text = str(self.panes[self._current_pane_key()].get("1.0", "end")).rstrip()
        if not text:
            self._status("这个窗格还是空的")
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self._status(f"已复制 {len(text)} 字", OK)

    def act_save_pane(self) -> None:
        key = self._current_pane_key()
        text = str(self.panes[key].get("1.0", "end")).rstrip()
        if not text:
            self._status("没有可保存的内容")
            return
        path = filedialog.asksaveasfilename(
            title="保存窗格内容",
            defaultextension=".txt",
            filetypes=[("文本", "*.txt"), ("Markdown", "*.md"), ("所有文件", "*.*")],
        )
        if not path:
            return
        try:
            Path(path).write_text(text + "\n", encoding="utf-8")
        except OSError as e:
            self._status(f"写不出去：{e}", BAD)
            return
        self._status(f"已保存 {path}", OK)

    def on_close(self) -> None:
        """关窗前拦一道：④正在烧钱时，直接关窗口**不会**杀掉子进程（Windows 上
        父进程退出不管孩子），那一轮会继续把调用打完。所以要么中止、要么明确放行。"""
        if self.run_proc is not None:
            if not messagebox.askyesno(
                "有任务在跑",
                "④那一轮还在打真端点。关闭会中止它（已花掉的调用不退还）。\n现在关闭并中止？",
            ):
                return
            self.act_abort()
        self.tmpdir.cleanup()
        self.root.destroy()

    def act_report(self) -> None:
        if not self.last_run_id:
            self._status("还没有 run_id（先提交一轮）", BAD)
            return
        self._spawn(
            "取报告",
            lambda: [str(run_cli_json(["report", self.last_run_id]).get("report", ""))],
            target="run",
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
        description="PromptMaster 原生桌面窗口（Tkinter）：体检 / 考卷 / 差分 / 闭环 / 账本",
        epilog="①②③⑤ 全零调用不需要 Key；④要 Key 或 PM_FAKE_BACKEND。窗口只在本机，不开任何端口。",
    )
    p.add_argument(
        "--tab",
        # ⑤账本一度漏在 choices 之外：五个标签里有一个永远开不了
        choices=("check", "cases", "diff", "run", "ledger"),
        default="check",
        help="启动后停在哪个标签（①体检 / ②考卷 / ③差分 / ④闭环 / ⑤账本）",
    )
    p.add_argument(
        "--scale",
        type=float,
        default=None,
        help="Tk 的像素换算（tk scaling）；高 DPI 屏上字太小或太大时才需要设",
    )
    return p


def app_main(argv: list[str] | None = None) -> int:
    """入口：解析参数 → 建窗口 → 进主循环。Tk 必须在主线程，所以没有 --workers 这类参数。"""
    args = _build_parser().parse_args(sys.argv[2:] if argv is None else argv)
    root = tk.Tk()
    app = App(root, scale=args.scale)
    app.select_tab(args.tab)
    root.mainloop()
    return 0
