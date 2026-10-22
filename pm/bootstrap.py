"""入口引导：环境兜底（控制台编码 + `.env` 发现顺序）。

为什么在代码里做而不是靠 README 提醒：
Windows 下把中文日志重定向到文件时，stdout 默认走系统码页（GBK/cp936），
中文直接 UnicodeEncodeError 或乱码 —— 旧版只在 README 里让人
`$env:PYTHONIOENCODING="utf-8"`，忘了设就是乱码（L12）。
现在入口（run.py / run_server.py / 会打印非 ASCII 的脚本与示例）启动时统一调用
`ensure_utf8_stdio()`，对已打开的文本流 reconfigure 成 UTF-8，用户什么都不用记。
显式设置了 PYTHONIOENCODING 的环境不受影响（reconfigure 幂等）。
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from io import StringIO
from pathlib import Path

from .textio import TextDecodeError, read_text_auto

_UTF8_ALIASES = ("utf-8", "utf8")


def _load_one_env(path: str, load_dotenv: Callable[..., bool]) -> None:
    """加载一个 `.env`；GBK 文件按 GB18030 兜底，坏文件只告警不炸启动。"""
    try:
        load_dotenv(path, override=False)
    except UnicodeDecodeError:
        try:
            decoded = read_text_auto(path)
        except (OSError, TextDecodeError) as e:
            print(f"警告：{path} 无法解码，已跳过：{e}", file=sys.stderr)
            return
        print(
            f"提示：{path} 不是 UTF-8，已按 {decoded.encoding} 解码",
            file=sys.stderr,
        )
        load_dotenv(stream=StringIO(decoded.text), override=False)


def load_env_files(repo_root: Path | None = None) -> list[str]:
    """按用户可预期的顺序加载 `.env`，返回实际加载的文件路径。

    主来源是**当前工作目录向上查找**（装包态 `prompt-master` 在用户项目里跑，
    读的就是那个项目的 `.env`）。检出模式下再退回仓库根那份，方便开发。

    为什么不能直接用 `load_dotenv()`（2026-10-22 实测缺陷）：
    python-dotenv 的默认 `find_dotenv()` 从**调用方文件**所在目录向上找，于是
    `pm/bootstrap.py` → 仓库根 / site-packages 根，与用户当前目录无关。
    装包后用户在项目里放 `.env`，程序静默读不到，表现是"配了 Key 却报没 Key"。
    已在 `tests/test_env_discovery.py` 用"从别的目录跑一个 import pm.bootstrap 的脚本"
    钉住；那份用例在修复前读到的值是空。

    已存在于进程环境里的变量不会被 `.env` 覆盖（`override=False`）：
    CI 显式传的环境变量优先于文件，这是既有契约。
    """
    try:
        from dotenv import find_dotenv, load_dotenv
    except ImportError:  # 零调用子命令不依赖 python-dotenv 也能跑
        return []

    loaded: list[str] = []
    cwd_env = find_dotenv(usecwd=True)
    if cwd_env:
        _load_one_env(cwd_env, load_dotenv)
        loaded.append(cwd_env)

    root = repo_root if repo_root is not None else Path(__file__).resolve().parents[1]
    repo_env = root / ".env"
    if repo_env.is_file() and not (cwd_env and Path(cwd_env).resolve() == repo_env.resolve()):
        _load_one_env(str(repo_env), load_dotenv)
        loaded.append(str(repo_env))
    return loaded


def prepare_console() -> None:
    """命令行入口的启动步骤，**顺序是行为的一部分**：

    1. `load_env_files()` 必须早于任何 `pm.llm` 导入（它在模块级读 env，晚一步配置就是空的）；
    2. `ensure_utf8_stdio()` 必须早于任何打印。

    ⚠️ 这里**故意不管 sys.path**：`from pm.bootstrap import ...` 这一行本身就要求 `pm`
    已经可导入，所以"把仓库根插进 sys.path"只能留在调用方、且必须排在最前。
    （`python run.py` 时 sys.path[0] 恰好是仓库根，看不出问题；
    `runpy.run_path('run.py')` 从别的目录启动时就炸成 `ModuleNotFoundError: No module named 'pm'`
    —— 2026-09-25 我把这一步挪进来过，被 tests/test_cli_guards.py 的探针用例逮住。）

    为什么收成函数而不是每个入口各写一遍：这正是本仓库反复吃亏的那类分叉
    （CLI / submit / MCP 三套白名单、主管道与校准两套评分协议…）。两个入口两套引导，
    迟早出现"`--help` 在一个里能跑、在另一个里乱码"。
    """
    load_env_files()
    ensure_utf8_stdio()


def ensure_utf8_stdio() -> None:
    """把 stdout/stderr 重配置为 UTF-8 —— 判据是**流当前的编码**，不是操作系统。

    对管道 / 重定向同样生效：文本包装层的编码在首次写入前都可 reconfigure。
    任何失败（如极端宿主环境不支持 reconfigure）静默跳过，绝不让引导炸掉入口。

    为什么不再按 `os.name != "nt"` 直接返回（2026-10-13）：那句等于假设
    "非 Windows 的控制台一定是 UTF-8"，而这条假设正是 `scripts/logs_usage.py`
    **CI 绿、本机红**的成因 —— GH 的 windows runner 默认码页就是 UTF-8，
    只有真·中文 Windows 控制台（cp936）会崩，于是没有任何一台门禁机替用户崩过一次。
    改判据后 `PYTHONIOENCODING=gbk` 在 linux 上同样能复现，这条门在两侧都有区分度。
    已是 UTF-8 的流原样跳过（不碰宿主配置，与旧行为对 POSIX 的承诺一致）。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is None or not hasattr(stream, "reconfigure"):
                continue
            if (getattr(stream, "encoding", None) or "").lower().replace("_", "-") in _UTF8_ALIASES:
                continue
            stream.reconfigure(encoding="utf-8")
        except Exception:  # noqa: BLE001 - 引导兜底不允许影响启动
            pass
