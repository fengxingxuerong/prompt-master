"""PromptMaster 桌面端 —— 一个窗口、一条路：读项目，写提示词。

给第一次用的人只需要三个输入：

    项目文件夹（可选）  +  目标（可选）  +  已有提示词（可选）
    →  读取项目 → 模型分析进度 → 给出一版可直接复制的提示词

这里刻意不再放考卷、差分、账本、评委仪表，也不放主题切换和参数旋钮。那些是
CLI / REST / MCP 的集成面；这个窗口只服务“我想让 AI 接着我的项目干活，帮我把
提示词写好”这一件事。

四条边界：
1. **只读**：窗口与后端都不修改用户项目；`.env`、私钥、依赖目录由扫描层跳过。
2. **无 Key 也能用**：没有 Key 时只读取项目并给出进度读数，不会偷偷调用模型。
3. **一次分析、按请求计费**：生成提示词做一次模型分析。**结构化解析失败**会在
   `pm.llm.structured_call` 里自动重试（可能产生多次请求）；而**模型返回空正文**是
   报错、交回你来决定要不要再点一次 —— 那种情况下自动重试等于替你再花一次钱，
   而空正文多半是端点/额度问题，不是换个说法就能好（2026-10-31 更正：此前这里与
   界面文案都写成"格式/空正文都会自动重试"，与实现不符）。
4. **判据不在这里**：新提示词的结构体检来自 `pm.quality`，由后端进程执行；
   窗口只渲染 JSON，不自己发明分数。
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
from collections.abc import Callable
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any, Literal, cast

from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# 直接 `python extras/desktop.py` 时也要能读到仓库根的 .env：
# `run.py desktop` 走 prepare_console 会加载，但附件可以单独启动。
load_dotenv(_REPO_ROOT / ".env")

APP_TITLE = "PromptMaster · 读项目，写提示词"
AGENT_SCRIPT = _REPO_ROOT / "extras" / "project_agent.py"
BASE_WIDTH = 1040
# 真窗实测：900 高时「项目分析」只剩 6 行、`原提示词的问题` 被截在半句上。
# 结果区两块都要读，所以往上要高度；Screen 不够时下面的 min() 会自己退。
BASE_HEIGHT = 1000
MIN_GEOMETRY = (840, 700)
LONG_TIMEOUT = 900.0
HONESTY_NOTE = "这是模型直接改写的一版提示词；没有跑基线对照，不能保证它一定比原稿好。"

# 一套配色，不做换肤。界面只服务一件事：把输入和结果分清楚。
BG = "#eef2f8"
CARD = "#ffffff"
INK = "#182230"
MUTED = "#667085"
LINE = "#d5deeb"
ACCENT = "#2563eb"
ACCENT_DARK = "#1d4ed8"
ACCENT_SOFT = "#e8f0ff"
OK = "#067647"
BAD = "#b42318"
WARN = "#b54708"

FONT = ("Microsoft YaHei UI", 10)
FONT_SM = ("Microsoft YaHei UI", 9)
FONT_SM_B = ("Microsoft YaHei UI", 9, "bold")
FONT_BODY = ("Microsoft YaHei UI", 10)
FONT_TITLE = ("Microsoft YaHei UI", 18, "bold")
FONT_SUB = ("Microsoft YaHei UI", 10)
FONT_BUTTON = ("Microsoft YaHei UI", 10, "bold")

PAD = 16
GAP = 10


class AgentRunError(RuntimeError):
    """后端进程失败：消息已经是给人看的中文，窗口直接展示。"""


def run_available() -> bool:
    """能不能调用模型：现读环境变量，Key 可以在窗口开着时补上。"""
    for name in ("PM_OPTIMIZER_API_KEY", "PM_API_KEY"):
        if (os.getenv(name) or "").strip():
            return True
    return any((item or "").strip() for item in (os.getenv("PM_API_KEYS") or "").split(","))


def build_request(
    *, project_dir: str = "", prompt: str = "", goal: str = "", scan_only: bool = False
) -> dict[str, Any]:
    """把界面上的三项打成后端请求。纯函数，测试不需要建窗口。"""
    if not scan_only and not prompt.strip() and not goal.strip():
        raise ValueError("目标和已有提示词至少填一个。")
    folder = project_dir.strip()
    if folder and not Path(folder).expanduser().is_dir():
        raise ValueError(f"项目文件夹不存在：{folder}")
    return {
        "project_dir": folder,
        "prompt": prompt.rstrip(),
        "goal": goal.strip(),
        "scan_only": bool(scan_only),
    }


def normalize_project_path(raw: str) -> tuple[str, str]:
    """把用户粘进来的东西收拾成一条文件夹路径，返回 (路径, 说明)。

    为什么要收：资源管理器的「复制文件地址」给的是 `"D:\\proj\\src\\app.py"`（带引号、
    而且常常是**文件**不是文件夹），浏览器拖出来的可能是 `file:///D:/proj`，
    手抄的常带首尾空格。窗口不该让用户先学会这些差别。

    只在粘贴/落焦时做**纯文本**收拾，不判断"能不能读"——那是 `build_request` 与扫描层的事，
    这里多判一次就等于多一处判据口径。
    """
    text = (raw or "").strip().strip("\ufeff").strip()
    if not text:
        return "", ""
    pairs = {'"': '"', "'": "'", "“": "”", "‘": "’", "「": "」"}
    while len(text) >= 2 and pairs.get(text[0]) == text[-1]:
        text = text[1:-1].strip()
    low = text.lower()
    if low.startswith("file:///"):
        text = text[8:]
    elif low.startswith("file://"):
        text = "//" + text[7:]
    if not text:
        return "", ""
    path = Path(text).expanduser()
    try:
        if path.exists() and path.is_file():
            return str(path.parent), "这是文件，已改用它所在的文件夹"
    except OSError:  # pragma: no cover - 极端路径（超长/坏盘符）交给后面校验
        return str(path), ""
    return str(path), ""


def _project_stats(project: dict[str, Any] | None) -> list[str]:
    if not project:
        return ["未读取项目（只根据目标和提示词生成）。"]
    kinds = project.get("by_ext") or {}
    top = list(kinds.items())[:6]
    kind_text = " · ".join(f"{ext} {count}" for ext, count in top) or "（未识别）"
    lines = [
        f"读取 {project.get('file_count', 0)} 个文件（{project.get('dir_count', 0)} 个目录）",
        f"类型：{kind_text}",
    ]
    git = project.get("git")
    if isinstance(git, dict):
        branch = git.get("branch") or "（未命名分支）"
        state = "有未提交改动" if git.get("status") else "工作区干净"
        lines.append(f"Git：{branch} · {state}")
    todos = project.get("todos") or []
    if todos:
        lines.append(f"待办标记：{len(todos)} 条")
    skipped = int(project.get("skipped_secrets") or 0)
    if skipped:
        lines.append(f"已跳过 {skipped} 个疑似密钥文件（未读取内容）")
    for warning in project.get("warnings") or []:
        lines.append(f"注意：{warning}")
    return lines


def render_scan(project: dict[str, Any] | None) -> list[str]:
    """只读取项目时的读数列：统计 + 目录 + 重点文件 + git + TODO。"""
    lines = [f"项目：{project.get('root_name') or '（未选择）'}", ""] if project else []
    lines += _project_stats(project)
    if not project:
        return lines
    tree = project.get("tree") or []
    if tree:
        lines += ["", "目录（部分）：", *(f"  {row}" for row in tree[:40])]
    files = project.get("key_files") or []
    if files:
        lines += ["", "重点文件："]
        for item in files:
            lines.append(f"  · {item.get('path')}（{item.get('reason')}）")
    git = project.get("git")
    if isinstance(git, dict) and git.get("recent_commits"):
        lines += ["", "最近提交："]
        lines.extend(f"  · {row}" for row in git["recent_commits"][:10])
    todos = project.get("todos") or []
    if todos:
        lines += ["", "待办线索："]
        lines.extend(
            f"  · {item.get('path')}:{item.get('line')} {item.get('text')}" for item in todos[:12]
        )
    return lines


def render_checks(checks: dict[str, Any]) -> list[str]:
    findings = checks.get("findings") or []
    if not findings:
        return ["结构体检：未命中已知规则（这只说明没踩已知事故模式，不证明任务能完成）。"]
    lines = [f"结构体检：命中 {len(findings)} 条（不影响交付，但建议看）"]
    lines.extend(f"  · [{item.get('code')}] {item.get('detail')}" for item in findings)
    return lines


def render_analysis(payload: dict[str, Any]) -> list[str]:
    """生成结果的分析页。首段是模型对项目进度的判断，其次是问题与证据。"""
    analysis = payload.get("analysis") or {}
    project = payload.get("project")
    lines: list[str] = []
    if project:
        lines.append(f"项目：{project.get('root_name') or '（未命名）'}")
    lines += ["项目进度", str(analysis.get("project_summary") or "（未读取项目）"), ""]
    gaps = analysis.get("prompt_gaps") or []
    lines.append("原提示词的问题")
    if gaps:
        lines.extend(f"· {item}" for item in gaps)
    else:
        lines.append("· （没有发现明显问题）")
    evidence = analysis.get("evidence") or []
    if evidence:
        lines += ["", "判断依据"]
        lines.extend(f"· {item}" for item in evidence)
    assumptions = analysis.get("assumptions") or []
    if assumptions:
        lines += ["", "需要你确认"]
        lines.extend(f"· {item}" for item in assumptions)
    lines += ["", *render_checks(payload.get("checks") or {})]
    usage = payload.get("usage") or {}
    model = str(usage.get("model") or "").strip()
    requests = usage.get("requests")
    if model or requests:
        parts = [model] if model else []
        if requests:
            parts.append(f"请求 {requests} 次")
        lines += ["", "模型调用：" + " · ".join(parts)]
    if project:
        files = [str(item.get("path")) for item in project.get("key_files") or []]
        if files:
            lines += ["", "读取的重点文件：" + "、".join(files)]
        lines += ["", "项目读数", *_project_stats(project)]
    lines += ["", HONESTY_NOTE]
    return lines


def prompt_of(payload: dict[str, Any]) -> str:
    return str(payload.get("optimized_prompt") or "").strip()


def _format_elapsed(seconds: float) -> str:
    total = max(0, int(seconds))
    return f"{total // 60}:{total % 60:02d}"


def _redact(text: str) -> str:
    # 延迟导入：直接 `python extras/desktop.py` 时仓库根要在 sys.path 里，
    # 而它在模块顶部的引导行才被插入；这里只给错误出口用，不值得再加一条 E402。
    from extras.project_context import redact_secrets

    return cast("str", redact_secrets(text))


def _extract_json(stdout: str) -> dict[str, Any]:
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    tail = _redact(stdout[-300:]) or "（空）"
    raise AgentRunError(f"分析进程没有返回 JSON。stdout 尾部：{tail}")


def run_agent_request(
    request: dict[str, Any], register: Callable[[subprocess.Popen[str] | None], None]
) -> dict[str, Any]:
    """在子进程里跑后端：可取消、不阻塞 UI，也不会把 Tk 拖进模型调用的异常里。"""
    with tempfile.TemporaryDirectory(prefix="pm-desktop-") as tmp:
        request_file = Path(tmp) / "request.json"
        request_file.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
        creationflags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0)) if os.name == "nt" else 0
        proc: subprocess.Popen[str] | None = None
        try:
            proc = subprocess.Popen(
                [sys.executable, str(AGENT_SCRIPT), "--request-file", str(request_file), "--json"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=str(_REPO_ROOT),
                stdin=subprocess.DEVNULL,
                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                creationflags=creationflags,
            )
            register(proc)
            stdout, stderr = proc.communicate(timeout=LONG_TIMEOUT)
        except subprocess.TimeoutExpired as exc:
            if proc is not None:
                proc.terminate()
            raise AgentRunError(f"分析超过 {int(LONG_TIMEOUT)} 秒仍未结束，已停止。") from exc
        except OSError as exc:
            raise AgentRunError(f"无法启动分析进程：{exc}") from exc
        finally:
            register(None)
    try:
        payload = _extract_json(stdout)
    except AgentRunError:
        if proc is not None and proc.returncode not in (0, None) and stderr.strip():
            raise AgentRunError(f"分析进程失败：{_redact(stderr.strip()[-400:])}") from None
        raise
    if not payload.get("ok"):
        raise AgentRunError(_redact(str(payload.get("error") or "分析失败，请重试。")))
    return payload


class App:
    """单页窗口。耗时动作在守护线程里跑，结果经队列回主线程渲染。"""

    def __init__(
        self,
        root: tk.Tk,
        *,
        scale: float | None = None,
        preload: str = "",
        project_dir: str = "",
    ) -> None:
        self.root = root
        self.q: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.inflight = 0
        self.proc: subprocess.Popen[str] | None = None
        self.aborted = False
        self.started_at: float | None = None
        self._alive = True
        if scale:
            try:
                root.tk.call("tk", "scaling", float(scale))
            except tk.TclError:
                pass
        root.title(APP_TITLE)
        # 1080p 上给 900 高；768p 笔记本上退到 700，别让窗口底部跑到屏幕外。
        width = min(BASE_WIDTH, max(MIN_GEOMETRY[0], root.winfo_screenwidth() - 80))
        height = min(BASE_HEIGHT, max(MIN_GEOMETRY[1], root.winfo_screenheight() - 100))
        root.geometry(f"{width}x{height}")
        root.minsize(*MIN_GEOMETRY)
        root.configure(bg=BG)
        self._style()
        self._build()
        if project_dir:
            self._apply_project_path(project_dir, quiet=True)
        if preload:
            self._load_prompt_file(preload, quiet=True)
        self._sync_buttons()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.bind("<Control-Return>", lambda _e: self.act_generate())
        root.bind("<Control-s>", lambda _e: self.act_save())
        self.root.after(80, self._drain)

    # ------------------------------ 外观 ------------------------------
    def _style(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TButton", font=FONT, padding=(12, 7))
        style.configure(
            "Accent.TButton",
            font=FONT_BUTTON,
            padding=(18, 9),
            background=ACCENT,
            foreground="#ffffff",
            borderwidth=0,
        )
        style.map(
            "Accent.TButton",
            # 禁用态要“看得出灰、也读得清”：白字压在中蓝上（#9db4e8/#f5f8ff）真窗截图里
            # 几乎看不见——而按钮上的字正是“为什么不能点”的唯一说明。
            background=[("active", ACCENT_DARK), ("disabled", "#c7d4ea")],
            foreground=[("disabled", "#41506b")],
        )
        style.configure("TEntry", padding=6, fieldbackground="#ffffff")
        style.configure("TScrollbar", background=CARD)

    def _label(
        self,
        parent: Any,
        text: str,
        *,
        fg: str = INK,
        font: tuple[Any, ...] = FONT,
        bg: str = CARD,
        anchor: Literal["nw", "n", "ne", "w", "center", "e", "sw", "s", "se"] = "w",
        justify: Literal["left", "center", "right"] = "left",
    ) -> tk.Label:
        return tk.Label(parent, text=text, fg=fg, bg=bg, font=font, anchor=anchor, justify=justify)

    def _build(self) -> None:
        self.root.grid_columnconfigure(0, weight=1)
        self.root.grid_rowconfigure(2, weight=1)
        self._header()
        self._form()
        self._results()
        self._statusbar()

    def _header(self) -> None:
        top = tk.Frame(self.root, bg=BG)
        top.grid(row=0, column=0, sticky="ew", padx=PAD, pady=(14, 8))
        top.grid_columnconfigure(1, weight=1)
        self._label(top, "PM", fg="#ffffff", font=FONT_SM_B, bg=ACCENT).grid(
            row=0, column=0, sticky="w", ipadx=7, ipady=3
        )
        title = self._label(top, "PromptMaster", font=FONT_TITLE, bg=BG)
        title.grid(row=0, column=1, sticky="w", padx=(10, 0))
        self._label(
            top,
            "选一个项目 → 读文件 → 分析进度 → 写出可直接用的提示词",
            fg=MUTED,
            font=FONT_SUB,
            bg=BG,
        ).grid(row=1, column=1, sticky="w", padx=(11, 0), pady=(2, 0))

    def _card(self, parent: Any, row: int, **kwargs: Any) -> tk.Frame:
        frame = tk.Frame(
            parent,
            bg=CARD,
            highlightthickness=1,
            highlightbackground=LINE,
            padx=PAD,
            pady=GAP,
        )
        frame.grid(row=row, sticky="ew", **kwargs)
        return frame

    def _form(self) -> None:
        form = self._card(self.root, 1, padx=PAD, pady=(0, GAP), column=0)
        form.grid_columnconfigure(1, weight=1)

        self._label(form, "项目文件夹", font=FONT_SM_B).grid(
            row=0, column=0, sticky="w", pady=(0, 6)
        )
        # 输入框与提示竖着叠：提示行切换成“怎么填 / ✓ 已选中 / ✗ 不存在”，
        # 不额外占一行表格（下面的 目标 / 提示词 / 动作 行号都不用挪）。
        project_box = tk.Frame(form, bg=CARD)
        project_box.grid(row=0, column=1, sticky="ew", padx=(GAP, GAP), pady=(0, 6))
        project_box.grid_columnconfigure(0, weight=1)
        self.in_project = ttk.Entry(project_box, font=FONT_BODY)
        self.in_project.grid(row=0, column=0, sticky="ew")
        self.project_hint = self._label(project_box, "", fg=MUTED, font=FONT_SM)
        self.project_hint.grid(row=1, column=0, sticky="w", pady=(4, 0))
        self.in_project.bind("<KeyRelease>", lambda _e: self._sync_project_hint())
        self.in_project.bind("<FocusOut>", lambda _e: self._sync_project_hint())
        # 粘贴完顺手回车就读：绑的是**免费**的只读取项目，不可能是花钱那一步 ——
        # 回车不该成为一次付费动作的触发器。
        self.in_project.bind("<Return>", lambda _e: self.act_scan())

        actions = tk.Frame(form, bg=CARD)
        actions.grid(row=0, column=2, sticky="ne", pady=(0, 6))
        ttk.Button(actions, text="选择文件夹…", command=self.act_pick_project).pack(side="left")
        # 两种方式都留：选择器给不想记路径的人，粘贴给已经复制好了的人。
        ttk.Button(actions, text="粘贴路径", command=self.act_paste_project).pack(
            side="left", padx=(6, 0)
        )
        ttk.Button(actions, text="清除", command=self.act_clear_project).pack(
            side="left", padx=(6, 0)
        )

        self._label(form, "目标", font=FONT_SM_B).grid(row=1, column=0, sticky="w", pady=(0, 6))
        self.in_goal = ttk.Entry(form, font=FONT_BODY)
        self.in_goal.grid(row=1, column=1, sticky="ew", padx=(GAP, GAP), pady=(0, 6))
        self._label(form, "可选：这次最想达成什么", fg=MUTED, font=FONT_SM).grid(
            row=1, column=2, sticky="w", pady=(0, 6)
        )

        self._label(form, "已有提示词", font=FONT_SM_B).grid(row=2, column=0, sticky="nw")
        prompt_box = tk.Frame(form, bg=CARD)
        prompt_box.grid(row=2, column=1, columnspan=2, sticky="ew")
        prompt_box.grid_columnconfigure(0, weight=1)
        self.in_prompt = tk.Text(
            prompt_box,
            height=3,
            wrap="word",
            font=FONT_BODY,
            fg=INK,
            bg="#fbfcfe",
            relief="flat",
            highlightthickness=1,
            highlightbackground=LINE,
            highlightcolor=ACCENT,
            insertbackground=INK,
            undo=True,
            padx=8,
            pady=6,
        )
        self.in_prompt.grid(row=0, column=0, sticky="ew")
        scroll = ttk.Scrollbar(prompt_box, orient="vertical", command=self.in_prompt.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.in_prompt.configure(yscrollcommand=scroll.set)
        under = tk.Frame(prompt_box, bg=CARD)
        under.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        ttk.Button(under, text="从文件载入…", command=self.act_load_prompt).pack(side="left")
        self._label(
            under,
            "留空也能生成：会按“目标”新写一版；贴上原稿则在其基础上改。",
            fg=MUTED,
            font=FONT_SM,
        ).pack(side="left", padx=(GAP, 0))

        action_row = tk.Frame(form, bg=CARD)
        action_row.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(GAP, 0))
        self.btn_generate = ttk.Button(
            action_row,
            text="读取项目并生成提示词",
            style="Accent.TButton",
            command=self.act_generate,
        )
        self.btn_generate.pack(side="left")
        self.btn_scan = ttk.Button(action_row, text="只读取项目（免费）", command=self.act_scan)
        self.btn_scan.pack(side="left", padx=(GAP, 0))
        self.hint = self._label(
            action_row,
            "一次分析（解析失败会自动重试；空正文会报错让你决定，按请求计费）· 只读，不修改项目",
            fg=MUTED,
            font=FONT_SM,
        )
        self.hint.pack(side="left", padx=(GAP, 0))

        privacy = (
            "只读：不会修改项目；自动跳过 .env、私钥和依赖目录。"
            "选中的关键文件内容摘录会发送给模型提供方，请不要在提示词里粘贴密钥。"
        )
        self._label(form, privacy, fg=MUTED, font=FONT_SM).grid(
            row=4, column=0, columnspan=3, sticky="w", pady=(8, 0)
        )

    def _results(self) -> None:
        area = tk.Frame(self.root, bg=BG)
        area.grid(row=2, column=0, sticky="nsew", padx=PAD)
        area.grid_columnconfigure(0, weight=1)
        self.results_area = area

        analysis_card = tk.Frame(
            area,
            bg=CARD,
            highlightthickness=1,
            highlightbackground=LINE,
            padx=PAD,
            pady=GAP,
        )
        analysis_card.grid(row=0, column=0, sticky="nsew", pady=(0, GAP))
        analysis_card.grid_columnconfigure(0, weight=1)
        analysis_card.grid_rowconfigure(1, weight=1)
        # 分析卡也要有自己的导出按钮：免费扫描之后**只有这块有内容**，
        # 而导出按钮原先只挂在提示词卡上 ⇒ 刚读出来的进度一个字也拿不走。
        a_header = tk.Frame(analysis_card, bg=CARD)
        a_header.grid(row=0, column=0, sticky="ew")
        a_header.grid_columnconfigure(0, weight=1)
        self._label(a_header, "项目分析", font=FONT_SM_B).grid(row=0, column=0, sticky="w")
        ttk.Button(a_header, text="复制", command=self.act_copy_analysis).grid(
            row=0, column=1, padx=(6, 0)
        )
        ttk.Button(a_header, text="另存为…", command=self.act_save_analysis).grid(
            row=0, column=2, padx=(6, 0)
        )
        self.out_analysis = tk.Text(
            analysis_card,
            height=8,
            wrap="word",
            font=FONT_BODY,
            fg=INK,
            bg="#fbfcfe",
            relief="flat",
            highlightthickness=1,
            highlightbackground=LINE,
            highlightcolor=ACCENT,
            insertbackground=INK,
            padx=8,
            pady=6,
        )
        self.out_analysis.grid(row=1, column=0, sticky="nsew", pady=(6, 0))
        scroll_a = ttk.Scrollbar(analysis_card, orient="vertical", command=self.out_analysis.yview)
        scroll_a.grid(row=1, column=1, sticky="ns", pady=(6, 0))
        self.out_analysis.configure(yscrollcommand=scroll_a.set)
        self.out_analysis.configure(state="disabled")

        prompt_card = tk.Frame(
            area,
            bg=CARD,
            highlightthickness=1,
            highlightbackground=LINE,
            padx=PAD,
            pady=GAP,
        )
        prompt_card.grid(row=1, column=0, sticky="nsew")
        prompt_card.grid_columnconfigure(0, weight=1)
        prompt_card.grid_rowconfigure(1, weight=1)
        self.prompt_card = prompt_card
        self._layout_result_mode("generate")
        header = tk.Frame(prompt_card, bg=CARD)
        header.grid(row=0, column=0, sticky="ew")
        header.grid_columnconfigure(0, weight=1)
        self._label(header, "优化后的提示词（可直接编辑）", font=FONT_SM_B).grid(
            row=0, column=0, sticky="w"
        )
        ttk.Button(header, text="复制", command=self.act_copy).grid(row=0, column=1, padx=(6, 0))
        ttk.Button(header, text="另存为…", command=self.act_save).grid(row=0, column=2, padx=(6, 0))
        # 两块各有自己的按钮：点哪块就导哪块，不用猜。Ctrl+S 走 `act_save` 的
        # "提示词优先、否则项目进度"兜底 —— 那一路会在状态行写明导的是哪块。
        self.out_prompt = tk.Text(
            prompt_card,
            height=10,
            wrap="word",
            font=FONT_BODY,
            fg=INK,
            bg="#fbfcfe",
            relief="flat",
            highlightthickness=1,
            highlightbackground=LINE,
            highlightcolor=ACCENT,
            insertbackground=INK,
            padx=8,
            pady=6,
        )
        self.out_prompt.grid(row=1, column=0, sticky="nsew", pady=(6, 0))
        scroll_p = ttk.Scrollbar(prompt_card, orient="vertical", command=self.out_prompt.yview)
        scroll_p.grid(row=1, column=1, sticky="ns", pady=(6, 0))
        self.out_prompt.configure(yscrollcommand=scroll_p.set)

    def _layout_result_mode(self, mode: str) -> None:
        """只读取项目时收起提示词卡，把高度全给分析；生成时再按 2:3 分。"""
        if mode == "scan":
            self.prompt_card.grid_remove()
            self.results_area.grid_rowconfigure(0, weight=1)
            self.results_area.grid_rowconfigure(1, weight=0)
            return
        self.prompt_card.grid()
        # 结果区的主角是提示词：分析给 2 份，提示词给 3 份。
        self.results_area.grid_rowconfigure(0, weight=2)
        self.results_area.grid_rowconfigure(1, weight=3)

    def _statusbar(self) -> None:
        bar = tk.Frame(self.root, bg=BG)
        bar.grid(row=3, column=0, sticky="ew", padx=PAD, pady=(8, 12))
        bar.grid_columnconfigure(0, weight=1)
        self.status_lbl = self._label(bar, "准备就绪", fg=MUTED, font=FONT_SM, bg=BG)
        self.status_lbl.grid(row=0, column=0, sticky="w")
        self.elapsed_lbl = self._label(bar, "", fg=MUTED, font=FONT_SM, bg=BG)
        self.elapsed_lbl.grid(row=0, column=1, sticky="e", padx=(GAP, GAP))
        self.btn_cancel = ttk.Button(bar, text="取消", command=self.act_cancel, state="disabled")
        self.btn_cancel.grid(row=0, column=2, sticky="e")
        ttk.Button(bar, text="说明", command=self.act_help).grid(
            row=0, column=3, sticky="e", padx=(6, 0)
        )

    # ------------------------------ 状态 ------------------------------
    def _status(self, text: str, color: str = MUTED) -> None:
        self.status_lbl.configure(text=text, fg=color)

    def _sync_buttons(self) -> None:
        busy = self.inflight > 0
        has_key = run_available()
        self.btn_generate.configure(
            state="disabled" if busy or not has_key else "normal",
            text="读取项目并生成提示词" if has_key else "未配置 Key，无法生成",
        )
        self.btn_scan.configure(state="disabled" if busy else "normal")
        self.btn_cancel.configure(state="normal" if busy else "disabled")
        if has_key:
            self.hint.configure(
                text="一次分析（解析失败会自动重试；空正文会报错让你决定，按请求计费）· 只读，不修改项目",
                fg=MUTED,
            )
        else:
            self.hint.configure(
                text="没配 Key：可以先免费读项目；设置 PM_API_KEY 后可生成提示词",
                fg=WARN,
            )

    def _set_analysis(self, lines: list[str]) -> None:
        text = "\n".join(lines).strip()
        self.out_analysis.configure(state="normal")
        self.out_analysis.delete("1.0", "end")
        self.out_analysis.insert("1.0", text)
        self.out_analysis.configure(state="disabled")

    def _set_prompt(self, text: str) -> None:
        self.out_prompt.delete("1.0", "end")
        self.out_prompt.insert("1.0", text)

    def _clear_results(self) -> None:
        self._set_analysis([])
        self._set_prompt("")

    # ------------------------------ 动作 ------------------------------
    def act_pick_project(self) -> None:
        path = filedialog.askdirectory(title="选择要分析的项目文件夹")
        if not path:
            return
        self._apply_project_path(path)

    def act_paste_project(self) -> None:
        """从剪贴板取路径。资源管理器「复制文件地址」带引号、且常给的是**文件**——都收。"""
        try:
            raw = self.root.clipboard_get()
        except tk.TclError:
            self._status("剪贴板里没有文本：先复制文件夹路径，或点「选择文件夹…」", WARN)
            return
        self._apply_project_path(str(raw))

    def _apply_project_path(self, raw: str, *, quiet: bool = False) -> None:
        """把一段粘贴文本落到输入框：去掉引号 / file:// / 文件→父目录，再刷实时校验。"""
        path, note = normalize_project_path(raw)
        self.in_project.delete(0, "end")
        self.in_project.insert(0, path)
        self._sync_project_hint()
        if quiet:
            return
        if not path:
            self._status("剪贴板里没有可用的路径", WARN)
        elif note:
            self._status(f"{note}：{path}", OK)
        else:
            self._status(f"已选择：{path}", OK)

    def _sync_project_hint(self) -> None:
        """路径的实时读数：怎么填 / ✓ 已选中 / ✗ 不存在。只查目录，不扫内容、不花钱。"""
        raw = self.in_project.get().strip()
        if not raw:
            self.project_hint.configure(
                text="可以直接粘贴路径（带引号的也行）后按回车读取，或点右侧「选择文件夹…」",
                fg=MUTED,
            )
            return
        path, note = normalize_project_path(raw)
        try:
            is_dir = bool(path) and Path(path).is_dir()
        except OSError:  # pragma: no cover - 坏盘符 / 超长路径
            is_dir = False
        if not is_dir:
            self.project_hint.configure(
                text="✗ 路径不存在或不是文件夹（改完会立刻重新检查）", fg=BAD
            )
            return
        count = 0
        try:
            for _ in Path(path).iterdir():
                count += 1
                if count >= 100:
                    break
        except OSError:  # pragma: no cover - 无权限目录：给个 0 项，别把窗口卡住
            count = 0
        text = f"✓ 已选中：{Path(path).name or path} · 顶层 {count}{'+' if count >= 100 else ''} 项"
        if note:
            text += f"（{note}）"
        self.project_hint.configure(text=text, fg=OK)

    def act_clear_project(self) -> None:
        self.in_project.delete(0, "end")
        self._sync_project_hint()
        self._status("已清除项目文件夹：将只根据提示词生成", MUTED)

    def act_load_prompt(self) -> None:
        path = filedialog.askopenfilename(
            title="载入已有提示词",
            filetypes=[("文本", "*.md *.txt"), ("所有文件", "*.*")],
        )
        if not path:
            return
        self._load_prompt_file(path)

    def _load_prompt_file(self, path: str, *, quiet: bool = False) -> None:
        try:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            if not quiet:
                self._status(f"读不到文件：{exc}", BAD)
            return
        self.in_prompt.delete("1.0", "end")
        self.in_prompt.insert("1.0", text)
        if not quiet:
            self._status(f"已载入 {Path(path).name}", OK)

    def _inputs(self) -> tuple[str, str, str]:
        return (
            self.in_project.get().strip(),
            self.in_prompt.get("1.0", "end").strip(),
            self.in_goal.get().strip(),
        )

    def act_scan(self) -> None:
        if self.inflight:
            # 防御性：按钮在 busy 时已置灰，但两处入口（按钮 / 将来的快捷键）都该自己挡一道 ——
            # 否则两个子进程会抢同一个 self.proc，取消只能掐掉后一个。
            self._status("已经有一轮在跑，先等它结束或点取消", WARN)
            return
        project, prompt, goal = self._inputs()
        if not project:
            self._refuse("先选一个项目文件夹，才能读取进度")
            return
        try:
            request = build_request(project_dir=project, prompt=prompt, goal=goal, scan_only=True)
        except ValueError as exc:
            self._refuse(str(exc))
            return
        self._start(request, "读取项目", scan_only=True)

    def act_generate(self) -> None:
        if self.inflight:
            self._status("已经有一轮在跑，先等它结束或点取消", WARN)
            return
        project, prompt, goal = self._inputs()
        try:
            request = build_request(project_dir=project, prompt=prompt, goal=goal, scan_only=False)
        except ValueError as exc:
            self._refuse(str(exc))
            return
        if not run_available():
            self._refuse("没有检测到 API Key：请先免费读取项目，或配置 PM_API_KEY")
            return
        self._start(request, "生成提示词", scan_only=False)

    def _refuse(self, message: str) -> None:
        """本地拒绝（缺输入 / 没 Key / 目录不存在）：**清掉上一轮读数**，把原因写进结果区。

        真窗手测抓到：目录被删后点「只读取项目」，状态行报了「文件夹不存在」，
        而结果区仍留着上一轮的项目进度 —— 用户很容易把它读成"这次也读到了"。
        拒绝不是一次读数，所以这里不留任何上一轮的痕迹。
        """
        self._clear_results()
        self._set_analysis(
            [f"⚠️ {message}", "", "（上一轮的读数已清空，不要把它当成这一次的结果。）"]
        )
        self._status(message, BAD)

    def _start(self, request: dict[str, Any], label: str, *, scan_only: bool) -> None:
        self.aborted = False
        self.inflight += 1
        self.started_at = time.monotonic()
        self._clear_results()
        self._sync_buttons()
        self._layout_result_mode("scan" if scan_only else "generate")
        self.elapsed_lbl.configure(text="已用 0:00")
        phase = "正在读取项目…" if scan_only else "正在读取项目，随后分析…"
        self._status(phase, ACCENT)
        t0 = time.monotonic()

        def worker() -> None:
            def register(proc: subprocess.Popen[str] | None) -> None:
                self.proc = proc
                # 用户可能在子进程还没起来时就按了取消/关窗；句柄一出现就补上一刀。
                if proc is not None and self.aborted:
                    try:
                        proc.terminate()
                    except OSError:
                        pass

            try:
                if self.aborted:
                    self.q.put(("aborted", scan_only))
                    return
                payload = run_agent_request(request, register)
            except Exception as exc:  # noqa: BLE001 - 后台线程的异常必须回投给 UI
                if self.aborted:
                    self.q.put(("aborted", scan_only))
                else:
                    message = str(exc) or f"{type(exc).__name__}: {exc}"
                    self.q.put(("error", message))
                return
            if self.aborted:
                self.q.put(("aborted", scan_only))
                return
            self.q.put(("done", (payload, scan_only, time.monotonic() - t0)))

        threading.Thread(target=worker, daemon=True, name="pm-desktop-agent").start()

    def _drain(self) -> None:
        if not self._alive:
            return
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "done":
                    result, scan_only, elapsed = payload
                    self.inflight = max(0, self.inflight - 1)
                    self._render(result, scan_only=scan_only)
                    self._status(f"完成 · {elapsed:.1f}s", OK)
                    self._sync_buttons()
                elif kind == "error":
                    self.inflight = max(0, self.inflight - 1)
                    self._layout_result_mode("scan")
                    # 只写“为什么没有结果”。**不贴 HONESTY_NOTE**：那句是给交付物打的口径说明，
                    # 贴在失败页上会读成“这一版虽然没成，但至少给了提示词”（真窗手测抓到）。
                    self._set_analysis([f"⚠️ 这次没有结果：{payload}"])
                    self._set_prompt("")
                    self._status("失败：看上方原因", BAD)
                    self._sync_buttons()
                elif kind == "aborted":
                    self.inflight = max(0, self.inflight - 1)
                    # 取消文案必须跟着**这一轮实际干了什么**：只读取项目没有模型调用，
                    # 照抄“调用不退还不退还”就是一句技术性的谎（真窗手测抓到）。
                    note = (
                        [
                            "这一轮是你按「取消」停下的。",
                            "只读取项目不调用模型：没有产生费用，项目也没有被修改。",
                        ]
                        if payload
                        else [
                            "这一轮是你按「取消」停下的。",
                            "已经发出的模型调用不退还；没有生成结果。",
                        ]
                    )
                    self._set_analysis(note)
                    self._status("已取消", WARN)
                    self._sync_buttons()
        except queue.Empty:
            pass
        if not self._alive:
            return
        if self.inflight == 0:
            self.started_at = None
            self.elapsed_lbl.configure(text="")
        elif self.started_at is not None:
            self.elapsed_lbl.configure(
                text=f"已用 {_format_elapsed(time.monotonic() - self.started_at)}"
            )
        self.root.after(80, self._drain)

    def _render(self, payload: dict[str, Any], *, scan_only: bool) -> None:
        mode = payload.get("mode")
        if mode == "project_scan" or scan_only:
            self._layout_result_mode("scan")
            lines = render_scan(payload.get("project"))
            if not run_available():
                lines += [
                    "",
                    "未配置 API Key，所以只完成了项目读取。",
                    "设置 PM_API_KEY（或 PM_OPTIMIZER_API_KEY）后，再点“读取项目并生成提示词”。",
                ]
            self._set_analysis(lines)
            self._set_prompt("")
            return
        self._layout_result_mode("generate")
        self._set_analysis(render_analysis(payload))
        self._set_prompt(prompt_of(payload))

    def act_cancel(self) -> None:
        self.aborted = True
        proc = self.proc
        if proc is not None:
            try:
                proc.terminate()
            except OSError as exc:
                self._status(f"取消失败：{exc}", BAD)
                return
            self._status("正在取消，等进程收尾…", WARN)
        else:
            self._status("正在读取，结束这一小步后会停下…", WARN)

    # ------------------------------ 导出 ------------------------------
    def _export_blocks(self) -> list[tuple[str, str, str]]:
        """可导出的结果块，按"哪块更像本次要交付的东西"排序：(名字, 文件名, 正文)。

        真窗手测（2026-10-21）：免费扫描之后**只有项目进度那块有内容**，而复制/另存
        只认提示词 ⇒ 点「另存为…」得到一句"还没有可保存的提示词"，刚读出来的进度
        一个字也拿不走，而 README 承诺的是"结果可直接复制或另存"。
        提示词排第一是因为它是这个窗口的交付物；项目进度是过程读数，但也是结果。
        """
        return [
            ("提示词", "prompt.md", str(self.out_prompt.get("1.0", "end"))),
            ("项目进度", "project-analysis.md", str(self.out_analysis.get("1.0", "end"))),
        ]

    def _pick_block(self, want: str = "") -> tuple[str, str, str] | None:
        """挑一块有内容的：指定了 `want` 就只认那一块，否则取排在最前的非空块。"""
        for name, filename, text in self._export_blocks():
            if want and name != want:
                continue
            if text.strip():
                return name, filename, text.strip()
        return None

    def _copy_block(self, want: str = "") -> None:
        picked = self._pick_block(want)
        if picked is None:
            self._status(f"还没有可复制的{want or '结果'}", MUTED)
            return
        name, _filename, text = picked
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self._status(f"已复制{name} {len(text)} 字", OK)

    def _save_block(self, want: str = "") -> None:
        picked = self._pick_block(want)
        if picked is None:
            self._status(f"还没有可保存的{want or '结果'}", MUTED)
            return
        name, filename, text = picked
        path = filedialog.asksaveasfilename(
            title=f"保存{name}",
            initialfile=filename,
            defaultextension=".md",
            filetypes=[("Markdown", "*.md"), ("文本", "*.txt"), ("所有文件", "*.*")],
        )
        if not path:
            return
        try:
            Path(path).write_text(text + "\n", encoding="utf-8")
        except OSError as exc:
            self._status(f"写不出去：{exc}", BAD)
            return
        self._status(f"已保存{name}：{path}", OK)

    def act_copy(self) -> None:
        """提示词卡上的「复制」与 Ctrl+C 那一族：有提示词就给提示词，否则给项目进度。"""
        self._copy_block()

    def act_save(self) -> None:
        """Ctrl+S 走这条：提示词优先，没有就存项目进度 —— 存了哪块会在状态行写明。"""
        self._save_block()

    def act_copy_analysis(self) -> None:
        self._copy_block("项目进度")

    def act_save_analysis(self) -> None:
        self._save_block("项目进度")

    def act_help(self) -> None:
        messagebox.showinfo(
            "怎么用",
            "1. 选择项目文件夹：可以直接粘贴路径（资源管理器「复制文件地址」带引号也行、\n"
            "   复制的是文件会自动改用所在文件夹），也可以点「选择文件夹…」。也可以不选，只改提示词。\n"
            "2. 写一句目标；已经有一版提示词就贴在下面。\n"
            "3. 点“读取项目并生成提示词”。\n\n"
            "· “只读取项目”不调用模型、不花钱，会列出文件、Git 进度和 TODO。\n"
            "· 生成时做一次模型分析（解析失败会自动重试，可能产生多次请求）。\n"
            "· 两块结果各有自己的「复制 / 另存为…」：项目进度和提示词都导得出来。\n"
            "  Ctrl+S 存提示词；没有提示词时（比如只做了免费扫描）存项目进度。\n"
            "· 扫描只读，自动跳过 .env、私钥、依赖目录；不会修改你的项目。\n"
            "· 没有配置 Key 时只能读取项目，不会偷偷发请求。\n"
            "· Ctrl+Enter 生成 · Ctrl+S 另存。\n\n"
            f"{HONESTY_NOTE}",
        )

    def on_close(self) -> None:
        if self.inflight:
            if not messagebox.askyesno(
                "任务还在进行",
                "关闭会中止本次读取/分析（已经发出的模型调用不退还）。\n确定关闭？",
            ):
                return
            self.aborted = True
            proc = self.proc
            if proc is not None:
                try:
                    proc.terminate()
                except OSError:
                    pass
        self._alive = False
        self.root.destroy()


def _build_parser() -> argparse.ArgumentParser:
    prog = Path(sys.argv[0]).name
    parser = argparse.ArgumentParser(
        prog=prog if prog == "desktop.py" else f"{prog} desktop",
        description="PromptMaster 桌面端：读取项目，分析进度，写出一版可直接使用的提示词。",
        epilog=(
            "不选项目也能只改提示词；“只读取项目”不调用模型。"
            "生成提示词会做一次模型分析；解析失败会自动重试，按实际请求计费。"
            "需要 PM_API_KEY 或 PM_OPTIMIZER_API_KEY。"
        ),
    )
    parser.add_argument("--scale", type=float, default=None, help="Tk 缩放；高 DPI 屏才需要")
    parser.add_argument("--prompt-file", default="", help="启动时预填已有提示词")
    parser.add_argument("--project-dir", default="", help="启动时预填项目文件夹")
    return parser


def app_main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(_argv_of(argv))
    root = tk.Tk()
    App(
        root,
        scale=args.scale,
        preload=args.prompt_file,
        project_dir=args.project_dir,
    )
    root.mainloop()
    return 0


def _argv_of(argv: list[str] | None) -> list[str]:
    if argv is not None:
        return argv
    if len(sys.argv) > 1 and sys.argv[1] == "desktop":
        return sys.argv[2:]
    return sys.argv[1:]


if __name__ == "__main__":
    sys.exit(app_main())
