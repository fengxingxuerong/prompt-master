"""入口引导：环境兜底（当前只有控制台编码兜底）。

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

_UTF8_ALIASES = ("utf-8", "utf8")


def prepare_console() -> None:
    """命令行入口的启动步骤，**顺序是行为的一部分**：

    1. `load_dotenv()` 必须早于任何 `pm.llm` 导入（它在模块级读 env，晚一步配置就是空的）；
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
    from dotenv import load_dotenv

    load_dotenv()
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
