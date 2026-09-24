"""仓库根 conftest：把 pytest 的临时目录根挪进本次检出（只影响 Windows）。

现象：本机任何 `pytest` 调用都返回 rc=1，哪怕用例 100% 通过。
根因：pytest 在 sessionfinish 清理临时目录时会 stat `%TEMP%\\pytest-of-<用户>\\pytest-current`
这个软链，而本机它已坏到连 readlink 都抛 PermissionError [WinError 5]（成因未定：
符号链接创建权限或杀软干预，重命名/删除同样被拒）。清理函数自己抛异常 ⇒ 退出码被污染，
"只认退出码"的本地门禁从来没工作过（CI 在 ubuntu 上不复现，所以一直没人看见）。
修法：临时根改到检出目录内的 `.pytest_tmp/`，由本进程自己创建，绕开坏软链。
调用方显式设了 PYTEST_DEBUG_TEMPROOT 时尊重其选择。
"""

from __future__ import annotations

import os
from pathlib import Path

if os.name == "nt":
    _tmp_root = Path(__file__).resolve().parent / ".pytest_tmp"
    os.environ.setdefault("PYTEST_DEBUG_TEMPROOT", str(_tmp_root))
    _tmp_root.mkdir(parents=True, exist_ok=True)
