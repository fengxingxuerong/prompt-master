"""`run.py panel` —— 起一个本地小面板（插件形态），零调用三张卡不需要 Key。

为什么不复用 `run_server.py`：那个是"常驻服务"（默认 8080、可多 worker、可挂 PM_TASK_DB
共享任务表），面向"长期跑着给 Agent 提交长任务"。面板是另一件事：一次点开、量一份提示词、
数一张考卷、比两版差异，用完关掉。所以默认端口分开（8765），且**默认只绑 127.0.0.1**。

一条命令的边界要说清：
- ①②③ 三张卡（体检 / 考卷有几票 / 两版差分）走 subprocess 打 `run.py`，**不需要 Key、不出网**；
- ④ 那张卡要真 Key（或 `PM_FAKE_BACKEND`），一轮实测 60 分钟 / 74~77 次调用；没 Key 时面板
  会把整卡置灰并写明原因，不会让你点了之后只看到一个 500。
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import webbrowser
from typing import Any

DEFAULT_PORT = 8765


def _panel_url(host: str, port: int) -> str:
    # 浏览器里 0.0.0.0 不是"本机"的写法，得换成 127.0.0.1 才点得开
    shown = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    return f"http://{shown}:{port}/panel"


def _guard_open_host(host: str, port: int) -> list[str]:
    """把"绑到局域网但没设 token"这件事在启动时就吵出来，而不是等别人提交任务。"""
    notes = []
    if host not in ("127.0.0.1", "localhost", "::1"):
        if not (os.getenv("PM_API_TOKEN") or "").strip():
            notes.append(
                f"⚠️ 监听 {host} 且未设 PM_API_TOKEN：同网段任何人都能打开面板并调用端点。"
                "请设 PM_API_TOKEN，或回到 --host 127.0.0.1"
            )
        notes.append(
            f"⚠️ 面板暴露的是本机仓库：②那张卡能读 case_templates/ 的文件名，"
            f"①③ 会在本机起子进程。绑到 {host} 等于把这些交给同网段。"
        )
    if not notes:
        notes.append(f"面板只监听 {host}：本机的浏览器才能访问（这正是想要的形状）。")
    return notes


def panel_command(argv: list[str] | None = None) -> int:
    p = _build_parser()
    args = p.parse_args(sys.argv[2:] if argv is None else argv)
    return _serve(args)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=f"{os.path.basename(sys.argv[0])} panel",
        description="本地插件面板：零调用体检 / 考卷有几票 / 两版差分；④优化闭环要 Key",
    )
    p.add_argument("--host", default="127.0.0.1", help="监听地址；缺省只绑回环，别轻易写 0.0.0.0")
    p.add_argument(
        "--port", type=int, default=DEFAULT_PORT, help=f"监听端口（缺省 {DEFAULT_PORT}）"
    )
    p.add_argument("--no-browser", action="store_true", help="只起服务，不自动开浏览器")
    return p


def _serve(args: argparse.Namespace) -> int:
    try:
        import uvicorn
    except ImportError:
        print("面板需要 uvicorn：pip install -r requirements.txt", file=sys.stderr)
        return 2

    url = _panel_url(args.host, args.port)
    print(f"PromptMaster 面板：{url}", flush=True)
    print(
        f"完整 API 文档：{_panel_url(args.host, args.port).replace('/panel', '/docs')}", flush=True
    )
    for note in _guard_open_host(args.host, args.port):
        print(note, flush=True)
    if not args.no_browser:
        _schedule_open(url)
    try:
        uvicorn.run("pm.server:app", host=args.host, port=args.port, log_level="warning")
    except SystemExit as e:
        # uvicorn 端口占用等启动失败走的是 sys.exit(1)：不接住的话用户只看到一行
        # "error while attempting to bind" 的栈式日志，退出码还是 1（运行失败），
        # 分不清是面板坏了还是端口占了。这里翻成一句能照做的话 + 配置错误码 2。
        code = e.code if isinstance(e.code, int) else 1
        if code == 0:
            return 0
        print(
            f"面板没起来（uvicorn 退出码 {code}）。端口 {args.port} 可能已被占用："
            f"换一个 --port {args.port + 1} 重试，或先关掉占用 {args.port} 的进程。",
            file=sys.stderr,
        )
        return 2
    return 0


def _schedule_open(url: str, delay: float = 0.8) -> Any:
    """延迟打开浏览器：uvicorn 还没起来就 open，第一下会连不上。

    `webbrowser.open` 在无桌面环境的机器上（容器/CI）返回 False 而不抛异常，
    所以这里不把它当成功信号 —— 地址已经打印在上一行了，点了就自己去开。
    """
    timer = threading.Timer(delay, lambda: webbrowser.open(url))
    timer.daemon = True
    timer.start()
    return timer
