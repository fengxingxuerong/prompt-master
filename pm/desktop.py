"""桌面窗口的**兼容壳**（2026-10-19 迁出主包）。

实现已移到仓库根的 `extras/desktop.py` —— 它是一份可选附件，不随 `pip install .` 安装：

- 为什么迁出：它是 2087 行 Tkinter 代码，占产品代码 8%，却只服务"想点开看看"的场景；
  主包该背的是 CLI / REST / MCP 三条真正被集成的入口。
- 为什么不删：`run.py desktop` 是既有用户的入口，删掉等于静默拿走功能。

装包态（`extras/` 不在 wheel 里）调用本模块不会抛 `ModuleNotFoundError`，而是给一条
能照做的提示 —— 一个"点了没反应"的窗口比一句错误信息更难排查。
"""

from __future__ import annotations

from typing import Any

_MISSING = (
    "desktop 是可选附件（仓库根的 extras/desktop.py），未随主包安装。\n"
    "请在仓库检出目录运行：python run.py desktop\n"
    "（想长期使用：pip install -e . 后仍从检出目录运行，或直接 python extras/desktop.py）"
)


try:  # 仓库检出内：extras/ 与 pm/ 同级，命名空间包可直接导入
    from extras.desktop import app_main as _app_main
except ModuleNotFoundError:  # 装包态：extras/ 没有被 setuptools 打进去
    _app_main = None


def app_main(argv: list[str] | Any = None) -> int:
    """转发到 `extras.desktop.app_main`；附件缺席时给出可照做的提示并返回配置错误码。"""
    if _app_main is None:
        import sys

        print(_MISSING, file=sys.stderr)
        return 2
    return int(_app_main(argv))
