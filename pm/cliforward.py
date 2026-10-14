"""CLI 转发与自带用例模板的**共享底层** —— 网页面板与桌面窗口都打这一份。

为什么要有这个文件：产品对"判据只有一份"的承诺是**结构性的**（`run.py check|diff` 是唯一
事实源，MCP 与 REST 都只是它的两个出口）。桌面窗口是第三个出口，如果它自带一份
subprocess 转发与一份模板白名单，那就出现三份"看起来一样"的实现 —— 本仓修过的
"两处同改的名单迟早漏改"这一族，正是这么长出来的。

⚠️ 已知仍是两份的：`pm/mcp_server.py` 的 `_run_cli_json`。没把它并过来是因为有一批用例
直接 monkeypatch `pm.mcp_server.subprocess.run` 并断言 MCP 自己那一份的行为，搬动会把
"哪个出口被谁钉着"这层证据搅浑。两份的加固项（list 参数不走 shell、stdin 接 DEVNULL、
PYTHONIOENCODING、退出码 0~3）由 `tests/test_panel.py` 的逐条对齐用例钉住 —— 漂了就红。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
RUN_PY = ROOT / "run.py"
TEMPLATE_DIR = ROOT / "case_templates"

# 退出码协议：0 达标 / 1 未达标但已交付 / 2 参数或配置 / 3 运行失败。
# 把这四个都当"正常返回"，因为体检命中规则时就是 1 —— 那是一次成功的调用，不是崩溃。
OK_EXIT_CODES = (0, 1, 2, 3)


class ForwardError(RuntimeError):
    """转发本身坏了（异常退出码 / stdout 不是 JSON）。消息里带 stderr 尾，便于自救。"""


class TemplateNotFound(KeyError):
    """模板名不在白名单里，或列举到但读不到（TOCTOU）。

    用中性异常而不是 HTTPException：桌面窗口没有"翻成 404"这一层，它要的是同一句话
    直接显示在界面上。`pm/panel.py` 负责把它翻成 404。
    """


def _spawn_cli(args: list[str], timeout: float | None) -> subprocess.CompletedProcess[str]:
    """唯一的子进程出口。加固三条，每条背后都有过一次事故：

    - **list 参数、绝不 shell=True**：体检的提示词正文里出现 `; rm -rf /` 不该变成命令；
    - **stdin=DEVNULL**：不接的话子进程继承调用方的管道，`run.py` 的 `ensure_utf8_stdio()`
      一碰继承来的空管道就永久阻塞（实测 60s 超时，而同一命令在 shell 里 0.9s）；
    - **PYTHONIOENCODING=utf-8**：这台机器的默认码页是 cp936，不显式设就把中文输出打崩。
    """
    return subprocess.run(
        [sys.executable, str(RUN_PY), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        cwd=str(ROOT),
        stdin=subprocess.DEVNULL,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )


def run_cli_json(args: list[str], timeout: float | None = 60.0) -> Any:
    """subprocess 打 `run.py … --json`，返回解析后的 JSON。"""
    proc = _spawn_cli(args, timeout)
    if proc.returncode not in OK_EXIT_CODES:
        raise ForwardError(f"run.py 退出码异常 {proc.returncode}：{proc.stderr[-300:]}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise ForwardError(
            f"run.py 没有给出合法 JSON（{e}）；stderr 尾部：{(proc.stderr or '')[-300:]}"
        ) from e


def run_cli_text(args: list[str], timeout: float | None = 120.0) -> tuple[str, int]:
    """只打人类表格、没有 `--json` 的入口（如 `--preflight`）走这条：返回 (stdout, 退出码)。

    为什么不给它编一个 JSON 形状：那等于在 UI 侧另立一套口径，而这份输出本身就是给人看的表。
    """
    proc = _spawn_cli(args, timeout)
    return proc.stdout, proc.returncode


def template_names() -> list[str]:
    """当场列举 `case_templates/*.json`，不缓存。

    这里**不写 try/except**：实测 Python 3.12 的 `Path.glob` 对不可读目录只返回空迭代
    （本机那个 ACL 坏掉的 `.pytest_tmp` 就是这样），异常分支够不到；目录不存在时同样退成
    空表，界面少几个选项而不会整页崩。
    """
    return sorted(p.name for p in TEMPLATE_DIR.glob("*.json") if p.is_file())


def template_path(name: str) -> Path:
    """把模板名解析成真实文件 —— **白名单**而不是"看起来没 `..` 就行"。

    贴一个 `../../.env` 就能把仓库外的文件读出来回显，所以只认当场列举得到的名字。
    """
    if not name or name not in template_names():
        raise TemplateNotFound(f"模板不存在：{name}（只认 case_templates/ 里的名字）")
    p = TEMPLATE_DIR / name
    if not p.is_file():
        raise TemplateNotFound(f"模板文件读不到：{name}")
    return p


def list_templates() -> list[dict[str, Any]]:
    """模板清单（只给名字与条数，不给正文）。坏 JSON 记 0 条而不是把整条列举炸掉。"""
    out = []
    for name in template_names():
        try:
            raw = json.loads((TEMPLATE_DIR / name).read_text(encoding="utf-8"))
            count = len(raw) if isinstance(raw, list) else 0
        except (OSError, json.JSONDecodeError):
            count = 0
        out.append({"name": name, "cases": count})
    return out


def run_cli_json_live(
    args: list[str],
    register: Callable[[subprocess.Popen[str] | None], None],
    timeout: float | None = None,
) -> Any:
    """`run_cli_json` 的可中止版：把 `Popen` 交给 `register`，UI 才按得下"中止"。

    为什么单独一条而不是给 `run_cli_json` 加参数：秒级调用不需要这个形状，
    只有④那种"一轮 60 分钟、70~80 次真调用"的才需要 —— 让简单路径保持简单。
    加固项与 `_spawn_cli` 逐条相同（list 参数、stdin=DEVNULL、PYTHONIOENCODING、退出码 0~3）。
    """
    proc = subprocess.Popen(
        [sys.executable, str(RUN_PY), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(ROOT),
        stdin=subprocess.DEVNULL,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    register(proc)
    try:
        out, err = proc.communicate(timeout=timeout)
    finally:
        register(None)
    if proc.returncode not in OK_EXIT_CODES:
        raise ForwardError(f"run.py 退出码异常 {proc.returncode}：{(err or '')[-300:]}")
    try:
        return json.loads(out)
    except json.JSONDecodeError as e:
        raise ForwardError(
            f"run.py 没有给出合法 JSON（{e}）；stderr 尾部：{(err or '')[-300:]}"
        ) from e
