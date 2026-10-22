"""装包后的命令行入口：`pip install .` 之后可直接敲 `prompt-master`。

与 `python run.py` 共用 `pm.bootstrap.prepare_console()`（同一套启动三步，只差 sys.path 那条：
从安装态跑没有"仓库根"概念），再转发给同一个 `pm.cli.main.main()`。

为什么要它：本产品的定位是"给 Agent 用的提示词质检"，而 Agent 的配置里写
`python /绝对路径/仓库/run.py --json` 是很脆的约定（换机器就断、也没法随 pip 升级）。
之前 `pyproject.toml` 里根本没有 `[project.scripts]`，装完包连一个命令都没有。

`.env` 与 `run.py` 共用同一套发现顺序（2026-10-22 修）：先按**当前工作目录**向上查找，
检出模式下再退回仓库根那份。装包态在用户项目目录里跑会读那个项目的 `.env`；
此前 python-dotenv 的默认按本文件所在目录（site-packages）找，用户配置会被静默忽略。
"""

from __future__ import annotations

import sys

from pm.bootstrap import prepare_console


def main() -> int:
    prepare_console()
    from .main import main as _main

    return _main()


if __name__ == "__main__":  # pragma: no cover - 入口由 [project.scripts] 使用
    sys.exit(main())
