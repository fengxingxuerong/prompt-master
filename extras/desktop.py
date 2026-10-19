"""PromptMaster 桌面端 —— 一个 Tkinter 原生窗口，不开浏览器、不起 HTTP 服务。

为什么用 Tk（标准库）而不是 pywebview / Qt：这个产品的人类入口应该在任何一台装了 Python 的
机器上直接就能开，多一个 GUI 依赖就多一个"装不上所以没用"的理由。Tk 8.6 随 CPython 发行，
Windows 上走 `clam` 主题（原生主题不理会 background/foreground，深色下按钮会成一块块浅灰）。

四条不变量：
1. **判据不在这个文件里**。所有判定都来自 subprocess 打 `run.py`（`pm.cliforward`），
   与 CLI / MCP / 网页面板同一条路。这里只做渲染与线程调度；连退出码都用
   `pm.cli.support._result_exit_code`，不另立一套 status→码 的映射。
2. **不在 worker 线程里摸 widget**。Tk 的 API 不是线程安全的：耗时动作跑在守护线程里，
   结果经 `queue.Queue` 回投，主循环用 `after()` 取。这是 Tk 应用最常见的崩法。
3. **花钱之前先报价**。④闭环提交前把零调用的 `--dry-run` 预估区间写在确认框里，并同时写明
   "优化后是否更好，按本项目自己的判据仍 0 例确证" —— 界面不许替结论打广告。
4. **外观只有一套令牌**。颜色在 `PALETTES` 里按主题成对定义（缺一个键 = 换肤后残留另一套
   颜色，这种缺陷肉眼才看得见、单测看不见），字号在 `FONT_*` 里，间距在 `S_*` 里。
   任何 widget 都不许自己写死颜色/字号 —— 字号表立起来的理由就一句话：同一句话在五张卡上
   必须同号，否则"看起来更重要"的那句只是字更大。
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
# 肉眼量出来的：这套界面多了一条左侧导航栏（NAV_W），窗宽再窄下去每一张卡的正文就被挤成半截。
WIN_GEOMETRY = "1020x720"
MIN_GEOMETRY = (900, 680)  # 量出来的：五张卡里最高的③在 660 下仍会把读数挤到看不见

# ---- 颜色：唯一事实源是 PALETTES，两套主题必须**同键**（深色那套也是从它派生的） ----
# 同一个键在两套里都出现才有意义：少一个键，换肤后那个位置会留着上一套主题的颜色，
# 而它只在浅色下现形（默认从深色起），所以这种缺陷只有点过"主题"才看得见。
PALETTES: dict[str, dict[str, str]] = {
    "dark": {
        "BG": "#060a10",  # 深空底
        "PANEL": "#0c1420",  # 卡片 / 侧栏（比底稍抬一层）
        "CARD": "#121e2f",  # 输入区 / 读数面（三层里最亮）
        "GRID": "#1b2b40",  # 分隔线 / 1px 描边
        "INK": "#d9e6f6",
        "MUTED": "#6b7f96",
        "OK": "#2bff9e",  # 霓虹绿
        "BAD": "#ff3d6e",  # 霓虹红
        "WARN": "#ffb547",
        "ACCENT": "#1ee0ff",  # 霓虹青
        "ON_ACCENT": "#04121a",  # 霓虹面上的字色（深色主题下取深、浅色主题下取白）
        # 导航选中态：深色下"比侧栏亮一层"，浅色下必须反过来"比侧栏深一点" ——
        # 同一个 CARD 在两套主题里一个更亮一个更白，直接复用等于浅色下没有选中态。
        "NAVSEL": "#16273c",
    },
    "light": {
        # 三层必须拉开：页面 > 卡 > 输入面。浅色下三档白很容易糊成一片
        # （#eef1f6 / #f7f9fc / #ffffff 实测几乎分不出卡在哪），所以底色压深一档。
        "BG": "#e8ecf3",
        "PANEL": "#fbfcfe",
        "CARD": "#ffffff",
        "GRID": "#ccd6e3",
        "INK": "#0f1620",
        "MUTED": "#55637a",
        "OK": "#0f7a52",
        "BAD": "#c02a45",
        "WARN": "#96590a",
        "ACCENT": "#1f6ad6",
        "ON_ACCENT": "#ffffff",
        "NAVSEL": "#dfe8f8",
    },
}

BG = PALETTES["dark"]["BG"]
PANEL = PALETTES["dark"]["PANEL"]
CARD = PALETTES["dark"]["CARD"]
GRID = PALETTES["dark"]["GRID"]
INK = PALETTES["dark"]["INK"]
MUTED = PALETTES["dark"]["MUTED"]
OK = PALETTES["dark"]["OK"]
BAD = PALETTES["dark"]["BAD"]
WARN = PALETTES["dark"]["WARN"]
ACCENT = PALETTES["dark"]["ACCENT"]
ON_ACCENT = PALETTES["dark"]["ON_ACCENT"]
NAVSEL = PALETTES["dark"]["NAVSEL"]

MONO: tuple[str, int] = ("Consolas", 10)

# ---- 字号：全走这一张表 ----
FONT_XS: tuple[str, int] = ("Segoe UI", 8)
FONT_SM: tuple[str, int] = ("Segoe UI", 9)
FONT_SM_B: tuple[str, int, str] = ("Segoe UI", 9, "bold")
FONT_BODY: tuple[str, int] = ("Segoe UI", 10)
FONT_BODY_B: tuple[str, int, str] = ("Segoe UI", 10, "bold")
FONT_TITLE: tuple[str, int, str] = ("Segoe UI", 14, "bold")
FONT_LOGO: tuple[str, int, str] = ("Segoe UI", 11, "bold")

# ---- 间距：四档就够，多一档就有人随手挑一个"看起来差不多"的数 ----
S_PAD = 14  # 窗口四周留白
S_GAP = 8  # 卡与卡
S_ROW = 6  # 行与行
S_CARD = 10  # 卡内留白

NAV_W = 168  # 左侧导航栏宽度
NAV_META: dict[str, tuple[str, str]] = {
    "check": ("① 提示词体检", "零调用 · 不出网"),
    "cases": ("② 考卷有几票", "只数能否决的票"),
    "diff": ("③ 两版差分", "只拦新引入"),
    "run": ("④ 优化闭环", "要 Key · 会花钱"),
    "ledger": ("⑤ 账本与仪表", "读本地归档"),
}

# 换肤靠改这几个模块级名字 + `_retheme()` 走一遍已建好的 widget。
# 为什么不做成"重建整个界面"：那会把用户已经贴进去的正文一起丢掉。
THEME = "dark"

# 忙指示的四帧：Tk 里做动画成本最低的形状（一个 Label 换字符），
# 比"进度条走百分之几"诚实 —— 这些动作没有可信的百分比可报。
SPIN: tuple[str, ...] = ("◐", "◓", "◑", "◒")

# 前景色的语义名（换肤时按名字重取，见 `_set_fg`）。顺序无关，只要有名字进得去。
FG_TOKENS: tuple[str, ...] = ("INK", "MUTED", "OK", "BAD", "WARN", "ACCENT", "ON_ACCENT")


def apply_theme(name: str) -> str:
    """切色板。改的是模块级那几个颜色名 —— 已建好的 widget 由 `_retheme()` 补一遍。"""
    global THEME
    pal = PALETTES.get(name) or PALETTES["dark"]
    g = globals()
    for k, v in pal.items():
        g[k] = v
    THEME = name
    return name


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
    # 评委席降级：这一行不许省。2026-10-15 真端点一轮里 4/4 例只剩单评委，
    # 而④窗格当时连"基线/Δ/盲评"都是 —（`--json` 没带这几个键），于是"半个评委席
    # 的读数"在界面上和双评委共识长得一模一样。分母取不到就不编一个分母。
    absent = d.get("judge_seat_absent") or {}
    if absent:
        seats = "、".join(sorted({s for v in absent.values() for s in v}))
        n_cases = agg.get("n_cases")
        where = f" {len(absent)}/{n_cases} 例" if n_cases else f" {len(absent)} 例"
        lines.append(
            f"⚠️ 评委席降级{where}（{seats} 缺席）——这些分数不是双评委共识；"
            "两臂完整度不同则 Δ 不可比"
        )
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
# 下面几个是 UI 侧的纯文本处理：**不做任何判定**。
# 判定只有一处来源（`run.py` 的 check/diff/…），这里只做"拆列、过滤、汇总、列举、着色分类"。
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


def line_tag(line: str) -> str:
    """给一行读数挑一个色（"" = 用正文色）。**只着色，不改一个字**。

    为什么值得做：五张卡的读数全是"分数、条数、警告、口径说明"混排的纯文本，
    颜色是唯一能在 0.2 秒内把"出了问题"和"这是一句说明"分开的东西。
    判据一句不参与 —— 分类只看行首形状，命中不了就老实留白（满屏染色等于没染）。
    """
    s = line.strip()
    if not s:
        return ""
    if s.startswith("---") or s.startswith("+++") or s.startswith("@@"):
        return "hdr"
    if s.startswith("⚠️") or "不可估" in s or "不可采信" in s or "不是双评委共识" in s:
        return "warn"
    if s.startswith("命中") or s.startswith("新引入") or "拦不住" in s or "解析失败" in s:
        return "bad"
    if s.startswith("无命中") or s.startswith("通过：") or "没有新引入" in s:
        return "ok"
    if line.startswith("  ") or s.startswith("· "):
        return "muted"
    return ""


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
    """主窗口：左侧导航 + 五张卡 + 底部状态条。耗时动作一律 worker 线程 + 队列回投。"""

    def __init__(self, root: tk.Tk, scale: float | None = None) -> None:
        self.root = root
        # 队列项 = (种类, 标签/载荷, 要显示的行, **结果该落进哪个窗格**)。
        # 窗格必须在**派发时**定死：早先版本回投时才读 self.ok_target，
        # 于是"先点①（慢）再点⑤（快）"会把①的结果画进⑤的窗格里 —— 串台且没有任何报错。
        self.q: queue.Queue[tuple[str, str, list[str], str]] = queue.Queue()
        self.panes: dict[str, tk.Text] = {}
        self._pane_hints: dict[str, Any] = {}
        self._input_hints: list[tuple[Any, Any]] = []
        self._skins: dict[int, str] = {}
        self._fg_skins: dict[int, str] = {}
        self._panes_tagged: list[Any] = []
        self._nav_items: list[tuple[str, Any, Any]] = []
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
        self._alive = True
        self._spinning = False
        self._spin_i = 0
        self._key_ok = False
        # 顺序即 _tab_* 的添加顺序：--tab 与键盘 Alt+N 都靠它。
        # 必须在建左侧导航之前就位：导航就是按这张表排的。
        self.tab_keys = ("check", "cases", "diff", "run", "ledger")
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
        self._nav()
        nb = ttk.Notebook(root)
        nb.pack(side="left", fill="both", expand=True, padx=(0, S_PAD), pady=(0, S_PAD))
        self.nb: Any = nb  # typeshed 里 ttk.Notebook.select 无签名，标 Any 才不撞 strict
        # ④/⑤ 要把界面上的正文与用例集落成文件再交给 CLI；生命周期跟着窗口走，
        # 关窗时显式清（只靠 TemporaryDirectory 的 finalizer 会在子进程还在读时炸）。
        self.tmpdir = tempfile.TemporaryDirectory(prefix="pm-desktop-")
        self._tab_check(nb)
        self._tab_cases(nb)
        self._tab_diff(nb)
        self._tab_run(nb)
        self._tab_ledger(nb)
        self._keys()
        nb.bind("<<NotebookTabChanged>>", lambda _e: self._on_tab_changed())
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._on_tab_changed()
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

    # ---- 外观：令牌 → widget ----
    def _skin(self, w: Any, token: str) -> None:
        """记住这个 widget 该用哪一层底色（换肤时按名字重取，不按当时那个色值）。"""
        self._skins[id(w)] = token

    def _skin_of(self, w: Any) -> str:
        return self._skins.get(id(w), "bg")

    def _token_bg(self, token: str) -> str:
        return {
            "bg": BG,
            "panel": PANEL,
            "card": CARD,
            "grid": GRID,
            "accent": ACCENT,
            "navsel": NAVSEL,
        }.get(token, BG)

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
            font=FONT_SM,
        )
        st.configure("TNotebook", background=BG, borderwidth=0, tabmargins=[0, 0, 0, 0])
        # 顶部那排标签由左侧导航栏取代（五个中文长标签挤在一行，且它们没有副标题的余地）。
        # 布局置空而不是删掉 Notebook：select/index/<<NotebookTabChanged>> 都还要用它做状态机。
        try:
            st.layout("TNotebook.Tab", [])
            st.configure("TNotebook.Tab", padding=(0, 0), borderwidth=0)
        except tk.TclError:  # pragma: no cover - 老 Tk 不吃空布局，那就留着标签条
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
            bordercolor=GRID,
            lightcolor=CARD,
            darkcolor=CARD,
            focuscolor=ACCENT,
        )
        st.map(
            "TButton",
            background=[("active", GRID), ("disabled", PANEL)],
            foreground=[("active", INK), ("disabled", MUTED)],
        )
        # 每张卡只有**一个**主动作：它拿霓虹底，其余一律次级。三级以内，不许第四级。
        st.configure(
            "Primary.TButton",
            padding=(11, 5),
            background=ACCENT,
            foreground=ON_ACCENT,
            bordercolor=ACCENT,
            lightcolor=ACCENT,
            darkcolor=ACCENT,
            focuscolor=ACCENT,
        )
        # 禁用时连描边一起收掉：只把底色刷灰、边框仍留着霓虹青的话，
        # 灰掉的主动作看起来比旁边能点的次级按钮更显眼（截图实测）。
        st.map(
            "Primary.TButton",
            background=[("active", ACCENT), ("disabled", GRID)],
            foreground=[("active", ON_ACCENT), ("disabled", MUTED)],
            bordercolor=[("disabled", GRID)],
            # clam 的立体边是 lightcolor / darkcolor 画的：只改 bordercolor，
            # 禁用态仍会留一圈霓虹青的斜面（截图实测）。
            lightcolor=[("disabled", GRID)],
            darkcolor=[("disabled", GRID)],
        )
        # 中止是唯一会掐掉真金白银的动作：描边与字色是红的，底色仍留在这一层（不抢主动作）。
        st.configure(
            "Danger.TButton",
            padding=(11, 5),
            background=PANEL,
            foreground=BAD,
            bordercolor=BAD,
            lightcolor=PANEL,
            darkcolor=PANEL,
            focuscolor=BAD,
        )
        st.map(
            "Danger.TButton",
            background=[("active", BAD), ("disabled", PANEL)],
            foreground=[("active", ON_ACCENT), ("disabled", MUTED)],
            bordercolor=[("disabled", GRID)],
            # clam 的立体边是 lightcolor / darkcolor 画的：只改 bordercolor，
            # 禁用态仍会留一圈霓虹青的斜面（截图实测）。
            lightcolor=[("disabled", GRID)],
            darkcolor=[("disabled", GRID)],
        )
        st.configure("TLabel", background=BG, foreground=INK)
        st.configure("Muted.TLabel", background=BG, foreground=MUTED)
        st.configure("TFrame", background=BG)
        st.configure(
            "TSpinbox", fieldbackground=CARD, foreground=INK, arrowcolor=INK, bordercolor=GRID
        )
        for name in ("TCombobox", "Card.TCombobox"):
            st.configure(
                name, fieldbackground=CARD, background=CARD, foreground=INK, arrowcolor=INK
            )
            st.map(name, fieldbackground=[("readonly", CARD)], foreground=[("readonly", INK)])
        for name in ("TEntry", "Card.TEntry"):
            st.configure(name, fieldbackground=CARD, foreground=INK, bordercolor=GRID)
        for name in ("TSpinbox", "Card.TSpinbox"):
            st.configure(name, fieldbackground=CARD, foreground=INK, bordercolor=GRID)
        # 卡里的复选：底色要跟着卡片（PANEL），跟页面（BG）会浮出一块色差
        st.configure("Card.TCheckbutton", background=PANEL, foreground=INK, focuscolor=ACCENT)
        st.map("Card.TCheckbutton", background=[("active", PANEL)])
        st.configure(
            "TScrollbar",
            troughcolor=BG,
            background=GRID,
            bordercolor=BG,
            arrowcolor=MUTED,
            lightcolor=GRID,
            darkcolor=GRID,
        )
        st.map("TScrollbar", background=[("active", MUTED)])
        st.configure(
            "Treeview",
            background=CARD,
            fieldbackground=CARD,
            foreground=INK,
            bordercolor=GRID,
            lightcolor=CARD,
            darkcolor=CARD,
        )
        st.configure("Treeview.Heading", background=PANEL, foreground=MUTED, font=FONT_SM_B)
        st.map("Treeview", background=[("selected", GRID)], foreground=[("selected", INK)])
        self.root.option_add("*TCombobox*Listbox.background", CARD)
        self.root.option_add("*TCombobox*Listbox.foreground", INK)
        self.root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)

    def _label(
        self,
        parent: Any,
        text: str,
        fg: str = "",
        font: Any = FONT_BODY,
        token: str = "",
        **kw: Any,
    ) -> tk.Label:
        """所有 tk.Label 都从这里出生：底色与字色都按令牌登记，换肤时才换得对。

        字色必须记**语义**而不是色值：浅色主题下 `INK` 是近黑、深色主题下是近白，
        若把创建那一刻的色值焊在 widget 上，切到浅色就会得到"白字白底"——
        这个缺陷只在点过"主题"之后现形，默认深色起的那一路永远看不见。
        """
        tok = token or self._skin_of(parent)
        w = tk.Label(parent, text=text, bg=self._token_bg(tok), fg=fg or INK, font=font, **kw)
        self._skin(w, tok)
        self._set_fg(w, fg or INK, repaint=False)
        return w

    def _button(
        self, parent: Any, text: str, command: Any, primary: bool = False, danger: bool = False
    ) -> Any:
        style = "Primary.TButton" if primary else ("Danger.TButton" if danger else "TButton")
        return ttk.Button(parent, text=text, command=command, style=style)

    def _row(self, parent: Any, pady: tuple[int, int] = (0, 0)) -> tk.Frame:
        tok = self._skin_of(parent)
        f = tk.Frame(parent, bg=self._token_bg(tok))
        f.pack(fill="x", pady=pady)
        self._skin(f, tok)
        return f

    def _page(self, nb: Any, key: str) -> tk.Frame:
        """一张卡的外框：ttk 外框贴 Notebook，里面换成 tk.Frame 才有完整着色权。"""
        outer = ttk.Frame(nb)
        title, _sub = NAV_META[key]
        nb.add(outer, text=title)
        page = tk.Frame(outer, bg=BG)
        page.pack(fill="both", expand=True)
        self._skin(page, "bg")
        return page

    def _card(self, parent: Any, title: str, hint: str = "", expand: bool = False) -> tk.Frame:
        """一块带标题与提示的卡：PANEL 底 + GRID 1px 描边。

        为什么非要这层壳：五个标签的控件原本全平铺在页面底色上，"输入 / 动作 / 读数"
        三段只能靠间距猜。分块之后一眼看得出"这一段是让我填的、那一段是给它答的"。
        返回的是卡体（往里塞控件），标题行由本方法自己收着。
        """
        f = tk.Frame(parent, bg=PANEL, highlightthickness=1, highlightbackground=GRID)
        f.pack(fill="both" if expand else "x", expand=expand, pady=(0, S_GAP))
        self._skin(f, "panel")
        head = tk.Frame(f, bg=PANEL)
        head.pack(fill="x", padx=S_CARD, pady=(8, 0))
        self._skin(head, "panel")
        self._label(head, title, INK, FONT_BODY_B, "panel").pack(side="left")
        if hint:
            self._label(head, hint, MUTED, FONT_XS, "panel").pack(side="right")
        body = tk.Frame(f, bg=PANEL)
        body.pack(fill="both" if expand else "x", expand=expand, padx=S_CARD, pady=(S_ROW, S_CARD))
        self._skin(body, "panel")
        return body

    def _input(self, parent: Any, height: int, placeholder: str = "") -> tk.Text:
        """多行输入框：CARD 底 + 1px 描边 + 空态占位句。

        占位句是一个**盖在上面**的 Label，不是 Text 里的文本 —— 写进 Text 会跟着
        `get("1.0","end")` 一起被送去体检、被导出、被复制，"提示"就变成了"内容"。
        """
        box = tk.Frame(parent, bg=CARD, highlightthickness=1, highlightbackground=GRID)
        box.pack(fill="x", pady=(0, S_ROW))
        self._skin(box, "card")
        txt = tk.Text(
            box,
            height=height,
            wrap="word",
            bg=CARD,
            fg=INK,
            insertbackground=INK,
            selectbackground=ACCENT,
            selectforeground=ON_ACCENT,
            relief="flat",
            font=MONO,
            padx=6,
            pady=6,
            spacing1=2,
        )
        txt.pack(fill="x", padx=1, pady=1)
        self._skin(txt, "card")
        if placeholder:
            hint = tk.Label(box, text=placeholder, bg=CARD, fg=MUTED, font=FONT_SM)
            hint.place(relx=0.0, rely=0.0, x=8, y=6, anchor="nw")
            hint.bind("<Button-1>", lambda _e: txt.focus_set())
            self._skin(hint, "card")
            self._input_hints.append((txt, hint))
        txt.bind("<KeyRelease>", lambda _e: self._sync_input_hints())
        return txt

    def _text_pane(self, key: str, parent: Any, height: int, empty: str) -> tk.Text:
        """读数窗格：只读、带滚动条与空态提示。"""
        box = tk.Frame(parent, bg=CARD, highlightthickness=1, highlightbackground=GRID)
        box.pack(fill="both", expand=True)
        self._skin(box, "card")
        frm = tk.Frame(box, bg=CARD)
        frm.pack(fill="both", expand=True, padx=1, pady=1)
        self._skin(frm, "card")
        txt = tk.Text(
            frm,
            height=height,
            wrap="word",
            bg=CARD,
            fg=INK,
            insertbackground=INK,
            selectbackground=ACCENT,
            selectforeground=ON_ACCENT,
            relief="flat",
            font=MONO,
            padx=6,
            pady=6,
            spacing1=2,
            state="disabled",
        )
        bar = ttk.Scrollbar(frm, command=txt.yview)
        txt.configure(yscrollcommand=bar.set)
        txt.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        self._skin(txt, "card")
        self._pane_tags(txt)
        hint = tk.Label(frm, text=empty, bg=CARD, fg=MUTED, font=FONT_SM)
        hint.place(relx=0.5, rely=0.5, anchor="center")
        hint.bind("<Button-1>", lambda _e: txt.focus_set())
        self._skin(hint, "card")
        self._pane_hints[key] = hint
        return txt

    def _pane_tags(self, w: Any) -> None:
        """读数窗格的五种行色。随主题重建（换肤时 tag 的颜色不会自己跟着变）。"""
        if w not in self._panes_tagged:
            self._panes_tagged.append(w)
        w.tag_configure("ok", foreground=OK)
        w.tag_configure("bad", foreground=BAD)
        w.tag_configure("warn", foreground=WARN)
        w.tag_configure("muted", foreground=MUTED)
        w.tag_configure("hdr", foreground=ACCENT)

    def _show(self, key: str, lines: list[str]) -> None:
        w = self.panes[key]
        w.configure(state="normal")
        w.delete("1.0", "end")
        if lines:
            w.insert("1.0", "\n".join(lines).rstrip())
            for i, line in enumerate(lines, start=1):
                tag = line_tag(line)
                if tag:
                    w.tag_add(tag, f"{i}.0", f"{i}.0 lineend")
        w.configure(state="disabled")
        self._toggle(self._pane_hints.get(key), lines)
        self._sync_input_hints()

    def _toggle(self, hint: Any, has_content: list[str] | bool) -> None:
        """空态提示的升降：有内容就压到文字下面，没内容就浮出来。"""
        if hint is None:
            return
        try:
            hint.lower() if has_content else hint.lift()
        except tk.TclError:  # pragma: no cover - 窗口正在销毁
            self._alive = False

    def _sync_input_hints(self) -> None:
        for txt, hint in self._input_hints:
            self._toggle(hint, bool(str(txt.get("1.0", "end")).strip()))

    def _status(self, text: str, color: str = MUTED) -> None:
        self._safe_text(self.status_lbl, text)
        self._set_fg(self.status_lbl, color)

    def _token_color(self, name: str) -> str:
        value = globals().get(name)
        return value if isinstance(value, str) else INK

    def _fg_token(self, color: str) -> str:
        """把一个色值反查回它的令牌名（反查不到 = 这个颜色不是令牌，换肤时不碰它）。"""
        low = color.lower()
        for name in FG_TOKENS:
            if self._token_color(name).lower() == low:
                return name
        return ""

    def _set_fg(self, w: Any, color: str, repaint: bool = True) -> None:
        tok = self._fg_token(color)
        if tok:
            self._fg_skins[id(w)] = tok
        if not repaint:
            return
        try:
            w.configure(fg=color)
        except tk.TclError:  # pragma: no cover - 窗口正在销毁
            self._alive = False

    def _safe_text(self, w: Any, text: str) -> None:
        try:
            w.configure(text=text)
        except tk.TclError:  # pragma: no cover - 窗口正在销毁
            self._alive = False

    def _safe_state(self, w: Any, state: str) -> None:
        try:
            w.configure(state=state)
        except tk.TclError:  # pragma: no cover - 窗口正在销毁
            self._alive = False

    def _spin_start(self) -> None:
        """忙指示：这些动作报不出可信的百分比，所以只报"还在跑"。"""
        if self._spinning:
            return
        self._spinning = True
        self._spin_step()

    def _spin_step(self) -> None:
        if not self._alive:
            self._spinning = False
            return
        if self.inflight <= 0:
            self._spinning = False
            self._safe_text(self.busy_lbl, "")
            return
        self._safe_text(self.busy_lbl, SPIN[self._spin_i % len(SPIN)])
        self._spin_i += 1
        self.root.after(200, self._spin_step)

    # ---- 三块常驻区：表头 / 导航 / 底栏 ----
    def _header(self) -> None:
        top = tk.Frame(self.root, bg=BG)
        top.pack(fill="x", padx=S_PAD, pady=(S_PAD, S_ROW))
        self._skin(top, "bg")
        self.logo = tk.Label(
            top, text="PM", bg=ACCENT, fg=ON_ACCENT, font=FONT_LOGO, padx=7, pady=3
        )
        self.logo.pack(side="left")
        self._skin(self.logo, "accent")
        self._label(top, "PromptMaster", INK, FONT_TITLE).pack(side="left", padx=(10, 0))
        self._label(top, "桌面面板 · 判据同源 run.py", MUTED, FONT_SM).pack(
            side="left", padx=(10, 0)
        )
        # 常驻"这份读数出自哪份代码"：版本 + 仓库路径。同一份读数在两台机器上可能来自
        # 不同代码，这是本产品一贯要暴露的东西。放表头而不是底栏 —— 底栏左右两组挤在一起
        # 时它第一个被裁（截图实测只剩 `projects\pror`），而表头右侧一直是空的。
        self.env_lbl = self._label(top, origin_label(), MUTED, FONT_XS)
        self.env_lbl.pack(side="right")
        sep = tk.Frame(self.root, bg=GRID, height=1)
        sep.pack(fill="x")
        self._skin(sep, "grid")

    def _footer(self) -> None:
        bar = tk.Frame(self.root, bg=PANEL)
        # `side="bottom"` + **先于笔记本 pack**：底部条原本在笔记本之后 pack，
        # 而笔记本是 `expand=True` ⇒ 窗口一矮，整条底栏被挤出可视区（截图实测：
        # 灯、导出、主题、帮助、版本号全都不在画面里，而单测照样绿，因为它直接调方法）。
        bar.pack(side="bottom", fill="x", padx=S_PAD, pady=(0, S_PAD))
        self._skin(bar, "panel")
        self.busy_lbl = self._label(bar, "", ACCENT, FONT_SM, "panel")
        self.busy_lbl.pack(side="left")
        self.led_cli = self._label(bar, "转发检测中…", MUTED, FONT_SM, "panel")
        self.led_cli.pack(side="left")
        self.led_key = self._label(bar, "", MUTED, FONT_SM, "panel")
        self.led_key.pack(side="left", padx=(S_PAD, 0))
        self.status_lbl = self._label(bar, "", MUTED, FONT_SM, "panel")
        self.status_lbl.pack(side="left", padx=S_PAD)
        # 右半区按"结果 / 环境 / 危险"分三组，组间一条 1px 竖线：
        # 六个按钮一字排开时，"中止"和"导出"看起来是同一类动作，而一个会掐任务、一个只写文件。
        self.btn_abort = self._button(bar, "中止", self.act_abort, danger=True)
        self.btn_abort.pack(side="right")
        self._sep(bar)
        self._button(bar, "帮助 F1", self.act_help).pack(side="right", padx=S_ROW)
        # 按钮写"要去哪"而不是"现在在哪"：一个只写"主题"的按钮，点下去之前没人知道会变亮还是变暗
        self.btn_theme = self._button(bar, "切换浅色", self.act_theme)
        self.btn_theme.pack(side="right")
        self._sep(bar)
        self._button(bar, "导出全部", self.act_export_all).pack(side="right", padx=S_ROW)
        self._button(bar, "另存结果…", self.act_save_pane).pack(side="right")
        self._button(bar, "复制结果", self.act_copy_pane).pack(side="right", padx=S_ROW)

    def _sep(self, parent: Any) -> tk.Frame:
        """分组之间那条 1px 竖线。"""
        f = tk.Frame(parent, bg=GRID, width=1)
        f.pack(side="right", fill="y", padx=S_ROW, pady=2)
        self._skin(f, "grid")
        return f

    def _nav(self) -> None:
        """左侧导航：五个模块 + 一句副标题 + 选中态（左侧那条 3px 霓虹就是选中标记）。

        为什么换掉顶部标签：五个中文长标签（"① 提示词体检"…）横排时既挤又没有放
        "这张卡零调用 / 这张卡要花钱"这句最重要信息的地方，而副标题恰恰是让人不看文档
        就知道"点下去会不会花钱"的那一行。
        """
        wrap = tk.Frame(self.root, bg=PANEL, width=NAV_W)
        wrap.pack(side="left", fill="y", padx=(S_PAD, 0), pady=(0, S_PAD))
        wrap.pack_propagate(False)  # 不让它被按钮文字撑宽，NAV_W 是量出来的
        self._skin(wrap, "panel")
        for key in self.tab_keys:
            title, sub = NAV_META[key]
            row = tk.Frame(wrap, bg=PANEL)
            row.pack(fill="x", pady=2)
            self._skin(row, "panel")
            mark = tk.Frame(row, bg=PANEL, width=3)
            mark.pack(side="left", fill="y")
            self._skin(mark, "panel")
            btn = tk.Button(
                row,
                text=f"{title}\n{sub}",
                anchor="w",
                justify="left",
                relief="flat",
                bd=0,
                bg=PANEL,
                fg=INK,
                activebackground=CARD,
                activeforeground=ACCENT,
                font=FONT_SM,
                padx=S_CARD,
                pady=8,
                command=self._nav_go(key),
            )
            btn.pack(side="left", fill="x", expand=True)
            self._skin(btn, "panel")
            self._nav_items.append((key, btn, mark))
        # 两行而不是一行：168px 宽装不下 "Alt+1..5 切卡 · Ctrl+Enter 执行" 这一整句，
        # 单行时尾巴被导航栏右边缘切掉（截图实测只剩 "Ctrl+Enter 执"）。
        self._label(
            wrap, "Alt+1..5 切卡\nCtrl+Enter 执行", MUTED, FONT_XS, "panel", justify="left"
        ).pack(side="bottom", anchor="w", padx=S_CARD, pady=(0, 8))
        sep = tk.Frame(self.root, bg=GRID, width=1)
        sep.pack(side="left", fill="y", pady=(0, S_PAD))
        self._skin(sep, "grid")

    def _nav_go(self, key: str) -> Callable[[], None]:
        """导航按钮的回调。**不能用 `lambda: self.select_tab(key)`**：闭包取的是循环变量的
        最终值，五个按钮会一起切到⑤。显式工厂函数才把这一刻的 key 钉住。"""

        def go() -> None:
            self.select_tab(key)

        return go

    def _paint_nav(self) -> None:
        """选中态只走一处：底色抬一层 + 字加粗 + 左侧霓虹条。"""
        current = self._current_pane_key()
        for key, btn, mark in self._nav_items:
            on = key == current
            try:
                btn.configure(
                    bg=NAVSEL if on else PANEL,
                    fg=ACCENT if on else INK,
                    font=FONT_SM_B if on else FONT_SM,
                    activebackground=NAVSEL,
                    activeforeground=ACCENT,
                )
                mark.configure(bg=ACCENT if on else PANEL)
            except tk.TclError:  # pragma: no cover - 窗口正在销毁
                self._alive = False

    def select_tab(self, key: str) -> None:
        """按 `--tab` 停在指定标签；未知键由 argparse 的 choices 挡住，这里不再兜底。

        这里**同步**刷一次导航高亮，而不是等 `<<NotebookTabChanged>>`：那个虚拟事件要等
        事件循环下一拍，点了导航之后有 1~2 帧是"新卡已显示、导航还指着旧卡"。
        """
        self.nb.select(self.tab_keys.index(key))
        self._paint_nav()

    def _led(self, w: tk.Label, text: str, good: bool) -> None:
        self._safe_text(w, ("● " if good else "× ") + text)
        self._set_fg(w, OK if good else BAD)

    def _on_tab_changed(self) -> None:
        """切卡时顺手做两件零成本的事：导航高亮跟手、④的可用性现读一次。"""
        self._paint_nav()
        self._sync_run_gate()

    def _sync_run_gate(self) -> None:
        """④的可用性写在界面上，而不只在状态行里一闪而过（README 承诺的"置灰并写明原因"）。

        现读而不是缓存：`run_available()` 是晚绑定的（Key 可以运行期补进环境），
        缓存起来就变成"补了 Key 还得重启窗口"。
        """
        self._key_ok = run_available()
        if self._key_ok:
            self._safe_text(
                self.run_gate_lbl, "Key 就绪：提交前会先报价，确认框里写的是这次参数的预估区间"
            )
            self._set_fg(self.run_gate_lbl, MUTED)
        else:
            self._safe_text(
                self.run_gate_lbl, "没配 Key（或没设 PM_FAKE_BACKEND）：④不可用 —— ①②③⑤ 不受影响"
            )
            self._set_fg(self.run_gate_lbl, BAD)
        self._sync_buttons()

    def _sync_buttons(self) -> None:
        """三处禁用态：没 Key 不许提交、有任务在跑不许再提交、没有在跑的任务不许按中止。"""
        self._safe_state(self.btn_abort, "normal" if self.run_proc is not None else "disabled")
        locked = "disabled" if (self.inflight or not self._key_ok) else "normal"
        for b in (self.btn_submit, self.btn_preflight):
            self._safe_state(b, locked)

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
        self._sync_buttons()
        self._spin_start()
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
                self._sync_buttons()
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
        self._key_ok = key_ready
        self._sync_buttons()
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
    def _tab_check(self, nb: Any) -> None:
        page = self._page(nb, "check")
        body = self._card(page, "输入", "零调用 · 不联网")
        self.in_check = self._input(body, 8, "把要体检的提示词贴到这里（Ctrl+Enter 直接体检）")
        row = self._row(body)
        self._button(row, "体检", self.act_check, primary=True).pack(side="left")
        self._button(row, "从文件载入…", self.act_load_check).pack(side="left", padx=S_ROW)
        self._button(row, "清空", lambda: self._fill(self.in_check, "")).pack(side="left")
        self._label(row, "只答「会不会出事」，不答「能不能完成任务」", MUTED, FONT_XS).pack(
            side="right"
        )
        out = self._card(page, "读数", "来自 run.py check", expand=True)
        self.panes["check"] = self._text_pane(
            "check", out, 4, "还没有读数：贴一段提示词，然后点「体检」"
        )

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
    def _tab_cases(self, nb: Any) -> None:
        page = self._page(nb, "cases")
        body = self._card(page, "输入", "花钱之前先量考卷")
        row = self._row(body)
        self._label(row, "自带模板", INK, FONT_SM).pack(side="left")
        names = [t["name"] for t in list_templates()]
        self.tpl = ttk.Combobox(
            row, width=34, state="readonly", values=names, style="Card.TCombobox"
        )
        self.tpl.pack(side="left", padx=S_ROW)
        if names:
            self.tpl.current(0)
        self._button(row, "体检这份考卷", self.act_cases_tpl, primary=True).pack(side="left")
        self.in_cases = self._input(
            body, 5, '或粘贴一个 JSON 数组：[{"input": "…", "expected": "…"}]'
        )
        row2 = self._row(body)
        self._button(row2, "体检粘贴的 JSON 数组", self.act_cases_paste, primary=True).pack(
            side="left"
        )
        self._label(row2, "自带模板与粘贴内容二选一", MUTED, FONT_XS).pack(side="left", padx=S_ROW)
        out = self._card(page, "读数", "来自 run.py check --cases-file", expand=True)
        self.panes["cases"] = self._text_pane(
            "cases", out, 4, "还没有读数：选一份自带模板，或粘贴一份 JSON 数组"
        )

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
    def _tab_diff(self, nb: Any) -> None:
        page = self._page(nb, "diff")
        body = self._card(page, "两版正文", "零调用 · 只拦新引入")
        # 并排而不是上下叠：这一段原本上下两条 6 行输入，把③顶到 700px 装不下，
        # 读数窗格被挤成 1px（截图实测）。左右分栏顺带把"改动前 → 改动后"的方向
        # 画成了它本来的样子 —— 下方那个并排 diff 视图也是同一个方向。
        cols = tk.Frame(body, bg=PANEL)
        cols.pack(fill="x")
        self._skin(cols, "panel")
        left = tk.Frame(cols, bg=PANEL)
        left.pack(side="left", fill="both", expand=True, padx=(0, S_ROW))
        self._skin(left, "panel")
        right = tk.Frame(cols, bg=PANEL)
        right.pack(side="left", fill="both", expand=True)
        self._skin(right, "panel")
        self._label(left, "改动前", MUTED, FONT_XS).pack(anchor="w")
        self.in_a = self._input(left, 6, "改动前的那一版")
        self._label(right, "改动后", MUTED, FONT_XS).pack(anchor="w")
        self.in_b = self._input(right, 6, "改动后的那一版")
        row = self._row(body)
        self._button(row, "差分", self.act_diff, primary=True).pack(side="left")
        self._button(row, "把①的正文填到「前」", self.act_fill_a).pack(side="left", padx=S_ROW)
        self._button(row, "按 run_id 差分", self.act_diff_runs).pack(side="left")
        row2 = self._row(body, pady=(S_ROW, 0))
        self._label(row2, "run A", INK, FONT_SM).pack(side="left")
        self.diff_run_a = ttk.Entry(row2, width=16, style="Card.TEntry")
        self.diff_run_a.pack(side="left", padx=4)
        self._label(row2, "run B", MUTED, FONT_SM).pack(side="left", padx=(S_ROW, 0))
        self.diff_run_b = ttk.Entry(row2, width=16, style="Card.TEntry")
        self.diff_run_b.pack(side="left", padx=4)
        self._label(row2, "（④跑完会自动填进来）", MUTED, FONT_XS).pack(side="left", padx=S_ROW)
        # 并排视图：竞品（promptfoo 的 side-by-side、forge 的 diff）第一眼的形状。
        # 判据仍是 CLI 的 diff --json，这里只把 unified 拆成左右两列着色。
        side = self._card(page, "并排视图", "加=绿 · 删=红", expand=True)
        self.diff_view: Any = ttk.Treeview(side, columns=("l", "r"), show="headings", height=3)
        self.diff_view.heading("l", text="改动前")
        self.diff_view.heading("r", text="改动后")
        self.diff_view.column("l", width=330, anchor="w")
        self.diff_view.column("r", width=330, anchor="w")
        bar = ttk.Scrollbar(side, orient="vertical", command=self.diff_view.yview)
        self.diff_view.configure(yscrollcommand=bar.set)
        self.diff_view.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        self._diff_tags(self.diff_view)
        out = self._card(page, "读数", "来自 run.py diff", expand=True)
        self.panes["diff"] = self._text_pane(
            "diff", out, 3, "还没有读数：填两版正文，然后点「差分」"
        )

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
    def _tab_run(self, nb: Any) -> None:
        page = self._page(nb, "run")
        note = tk.Frame(page, bg=PANEL, highlightthickness=1, highlightbackground=GRID)
        note.pack(fill="x", pady=(0, S_GAP))
        self._skin(note, "panel")
        self._label(
            note,
            f"{COST_NOTE}。\n{HONESTY_NOTE} —— 界面给的是自评分 Δ 与成对盲评，请看结论别只看分数。",
            MUTED,
            FONT_SM,
            "panel",
            justify="left",
            anchor="w",
        ).pack(fill="x", padx=S_CARD, pady=6)
        task = self._card(page, "任务", "要 Key · 会真花钱")
        self.in_task = self._input(task, 2, "用一句话说清要让模型干什么（至少 4 个字）")
        # 这一行是"④现在能不能用"的唯一出处：无 Key 时按钮已置灰，但**为什么灰**必须写出来，
        # 否则置灰本身只是一次沉默的拒绝。anchor=w 不是审美 —— 居中会让它读起来像标题。
        self.run_gate_lbl = self._label(task, "检查 Key…", MUTED, FONT_SM, "panel", anchor="w")
        self.run_gate_lbl.pack(fill="x", pady=(S_ROW, 0))
        cfg = self._card(page, "参数", "勾了快速档就不传采样与迭代")
        r1 = self._row(cfg)
        self._label(r1, "目标模型", INK, FONT_SM).pack(side="left")
        self.model = ttk.Entry(r1, width=20, style="Card.TEntry")
        self.model.pack(side="left", padx=S_ROW)
        self._label(r1, "用例", INK, FONT_SM).pack(side="left")
        self.cases_n = ttk.Spinbox(r1, from_=1, to=8, width=4, style="Card.TSpinbox")
        self.cases_n.set("4")
        self.cases_n.pack(side="left", padx=4)
        self._label(r1, "最大迭代", INK, FONT_SM).pack(side="left")
        self.iter_n = ttk.Spinbox(r1, from_=0, to=10, width=4, style="Card.TSpinbox")
        self.iter_n.set("2")
        self.iter_n.pack(side="left", padx=4)
        self._label(r1, "采样", INK, FONT_SM).pack(side="left")
        self.samples_n = ttk.Spinbox(r1, from_=1, to=5, width=4, style="Card.TSpinbox")
        self.samples_n.set("2")
        self.samples_n.pack(side="left", padx=4)
        r2 = self._row(cfg, pady=(S_ROW, 0))
        self._label(r2, "用例集", INK, FONT_SM).pack(side="left")
        self.run_cases = ttk.Combobox(
            r2,
            width=30,
            state="readonly",
            style="Card.TCombobox",
            values=[NO_CASES, PASTED] + [t["name"] for t in list_templates()],
        )
        self.run_cases.set(NO_CASES)
        self.run_cases.pack(side="left", padx=S_ROW)
        self._label(r2, "断言", INK, FONT_SM).pack(side="left")
        self.assert_mode = ttk.Combobox(
            r2,
            width=9,
            state="readonly",
            style="Card.TCombobox",
            values=["contains", "exact", "regex", "rule"],
        )
        self.assert_mode.set("contains")
        self.assert_mode.pack(side="left", padx=4)
        self.seed_var = tk.BooleanVar(value=False)
        self.fast_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            r2, text="把①的正文当原稿改", variable=self.seed_var, style="Card.TCheckbutton"
        ).pack(side="left", padx=(S_CARD, 0))
        ttk.Checkbutton(r2, text="快速档", variable=self.fast_var, style="Card.TCheckbutton").pack(
            side="left", padx=S_ROW
        )
        act = self._card(page, "动作", "提交前会先报价")
        r3 = self._row(act)
        self._button(r3, "先报价（零调用）", self.act_estimate).pack(side="left")
        self.btn_preflight = self._button(r3, "端点预检（真调用）", self.act_preflight)
        self.btn_preflight.pack(side="left", padx=S_ROW)
        self.btn_submit = self._button(r3, "提交任务", self.act_run, primary=True)
        self.btn_submit.pack(side="left")
        self._button(r3, "取报告", self.act_report).pack(side="left", padx=S_ROW)
        r4 = self._row(act, pady=(S_ROW, 0))
        self._button(r4, "取用交付提示词 → ①", self.act_take_prompt).pack(side="left")
        self._button(r4, "把它填到③的「后」", self.act_prompt_to_diff_b).pack(
            side="left", padx=S_ROW
        )
        self._label(r4, "跑完才有内容", MUTED, FONT_XS).pack(side="left", padx=S_ROW)
        out = self._card(page, "读数", "来自 run.py --json", expand=True)
        self.panes["run"] = self._text_pane(
            "run", out, 4, "还没有读数：先「报价」，确认后再「提交任务」"
        )

    # ---- 标签⑤：账本与仪表（四个按钮全零调用） ----
    def _tab_ledger(self, nb: Any) -> None:
        page = self._page(nb, "ledger")
        note = tk.Frame(page, bg=PANEL, highlightthickness=1, highlightbackground=GRID)
        note.pack(fill="x", pady=(0, S_GAP))
        self._skin(note, "panel")
        self._label(
            note,
            "这四个按钮都不花一分钱：读的是本地账本与运行归档。\n"
            "它们回答的是「这套仪表现在可信吗」，不是「这次优化成功了吗」。",
            MUTED,
            FONT_SM,
            "panel",
            justify="left",
            anchor="w",
        ).pack(fill="x", padx=S_CARD, pady=6)
        body = self._card(page, "读数动作", "全部零调用")
        row = self._row(body)
        self._button(row, "历史与 Δ", self.act_history, primary=True).pack(side="left")
        self._button(row, "评委仪表", self.act_calibrate).pack(side="left", padx=S_ROW)
        self._button(row, "资产库检索", self.act_library).pack(side="left")
        self._button(row, "改动门禁", self.act_gate).pack(side="left", padx=S_ROW)
        self._label(row, "历史条数", INK, FONT_SM).pack(side="left", padx=(S_CARD, 2))
        self.hist_n = ttk.Spinbox(row, from_=5, to=200, width=5, style="Card.TSpinbox")
        self.hist_n.set("40")
        self.hist_n.pack(side="left")
        row2 = self._row(body, pady=(S_ROW, 0))
        self._label(row2, "过滤", INK, FONT_SM).pack(side="left")
        self.hist_filter = ttk.Entry(row2, width=26, style="Card.TEntry")
        self.hist_filter.pack(side="left", padx=S_ROW)
        self._button(row2, "应用过滤", self.act_filter_ledger).pack(side="left")
        self._label(row2, "取 run_id 进③", INK, FONT_SM).pack(side="left", padx=(S_CARD, 2))
        self.ledger_run = ttk.Entry(row2, width=16, style="Card.TEntry")
        self.ledger_run.pack(side="left", padx=4)
        self._button(row2, "填入", self.act_run_to_diff).pack(side="left")
        lib = self._card(page, "资产库检索", "按任务相似度找已达标提示词")
        self.in_libq = self._input(lib, 2, "一句任务描述，用来检索已达标资产")
        out = self._card(page, "读数", "来自本地账本与归档", expand=True)
        self.panes["ledger"] = self._text_pane(
            "ledger", out, 4, "还没有读数：点上面四个按钮里的任意一个"
        )

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
        view.tag_configure("add", background=GRID, foreground=OK)
        view.tag_configure("del", background=GRID, foreground=BAD)
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
        self.btn_theme.configure(text="切换深色" if THEME == "light" else "切换浅色")
        self._status("主题：" + ("浅色" if THEME == "light" else "深色"), OK)

    def _retheme(self) -> None:
        """换肤：令牌先换，再按登记过的"这块该用哪一层"逐个补色。

        为什么不能一律刷成 BG：卡片是 PANEL、输入面是 CARD、分隔线是 GRID，
        一律刷平就把刚搭起来的三层层次抹掉了 —— 而层次正是这套界面唯一的空间语言。
        """
        self._style()
        for w in self._descendants(self.root):
            try:
                cls = w.winfo_class()
            except Exception:  # noqa: BLE001 - 窗口正在销毁时问类名会抛
                continue
            if cls == "Text":
                self._repaint_text(w)
            elif cls == "Label":
                self._repaint_plain(w)
            elif cls == "Button":
                self._repaint_button(w)
            elif cls == "Frame":
                self._repaint_frame(w)
        self.logo.configure(bg=ACCENT, fg=ON_ACCENT)
        for t in self._panes_tagged:
            self._pane_tags(t)
        self._paint_nav()
        view = getattr(self, "diff_view", None)
        if view is not None:
            self._diff_tags(view)

    def _repaint_frame(self, w: Any) -> None:
        w.configure(bg=self._token_bg(self._skin_of(w)))
        if str(w.cget("highlightthickness")) not in ("0", ""):
            w.configure(highlightbackground=GRID)

    def _repaint_text(self, w: Any) -> None:
        # 只换底色与光标/选区色：正文色留给各行的语义 tag（见 `line_tag`）
        w.configure(
            bg=CARD,
            fg=INK,
            insertbackground=INK,
            selectbackground=ACCENT,
            selectforeground=ON_ACCENT,
        )
        if str(w.cget("highlightthickness")) not in ("0", ""):
            w.configure(highlightbackground=GRID)

    def _repaint_plain(self, w: Any) -> None:
        # 底色按层、字色按语义：绿=可用、红=坏了这些**语义**要留住，但同一语义在
        # 两套主题下的色值不同（浅色下那支霓虹绿在白底上几乎看不见）。
        w.configure(bg=self._token_bg(self._skin_of(w)))
        tok = self._fg_skins.get(id(w))
        if tok:
            w.configure(fg=self._token_color(tok))

    def _repaint_button(self, w: Any) -> None:
        w.configure(bg=PANEL, fg=INK, activebackground=CARD, activeforeground=ACCENT)

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
            "左侧导航切五张卡（Alt+1..5） · Ctrl+Enter 跑当前卡 · Ctrl+S 另存窗格 · Ctrl+L 清空窗格\n\n"
            "①②③⑤ 零调用不花钱；④ 要 Key，提交前会先报价。\n"
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
        self._alive = False
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
        self._sync_input_hints()


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
