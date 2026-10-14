"""本套件的 pytest 临时根：**复用仓根那份"先探再选"**，不再写死路径。

为什么这里要单独一个 conftest：本目录自带 `pytest.ini` ⇒ pytest 的 rootdir 变成
`releases/triage`，仓根的 `conftest.py` 收不到。上一版的修法是 `--basetemp=.pytest_tmp`
把临时根**写死**在检出目录里，于是宿主把那个目录的 ACL 打坏之后（2026-10-13 全量跑
1693 条全卡 setup 的那次），本套件 17/38 条同样卡在 setup ERROR，而 ruff/mypy/selftest 全绿
—— 环境故障被读成了代码雪崩。写死一个路径并相信它，等于把验证能力挂在别人手里。

判据只有一份：探测函数从仓根 `conftest.py` 加载（那份文件 import 时自己就会
`setdefault("PYTEST_DEBUG_TEMPROOT", …)`），这里不复制它的三步探测与退路清单。
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

_ROOT_CONFTEST = Path(__file__).resolve().parents[2] / "conftest.py"


def _apply_root_temp_root() -> tuple[str, str]:
    """加载仓根 conftest 并拿回 (临时根, 退让文案)。非 Windows 上仓根那份不做任何事。"""
    if os.name != "nt":
        return "", ""
    spec = importlib.util.spec_from_file_location("_root_conftest_for_release", _ROOT_CONFTEST)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"加载不到仓根 conftest：{_ROOT_CONFTEST}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # 副作用就是 setdefault PYTEST_DEBUG_TEMPROOT（先探再选）
    root, warn = mod.choose_temp_root(os.environ)
    os.environ.setdefault("PYTEST_DEBUG_TEMPROOT", str(root))
    return str(root), str(warn or "")


_root, _warn = _apply_root_temp_root()
if _warn:
    print(f"[releases/triage/conftest] {_warn}", file=sys.stderr)
