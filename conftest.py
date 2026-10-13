"""仓库根 conftest：把 pytest 的临时目录根挪到一个**当场验证过能用**的位置。

现象一（原始事故）：本机任何 `pytest` 调用都返回 rc=1，哪怕用例 100% 通过。
根因：pytest 在 sessionfinish 清理临时目录时会 stat `%TEMP%\\pytest-of-<用户>\\pytest-current`
这个软链，而本机它已坏到连 readlink 都抛 PermissionError [WinError 5]（成因未定：
符号链接创建权限或杀软干预，重命名/删除同样被拒）。清理函数自己抛异常 ⇒ 退出码被污染，
"只认退出码"的本地门禁从来没工作过（CI 在 ubuntu 上不复现，所以一直没人看见）。
当时的修法：临时根改到检出目录内的 `.pytest_tmp/`，由本进程自己创建，绕开坏软链。

现象二（2026-10-13，上一修的副作用）：`.pytest_tmp/` 自己的 ACL 坏掉之后，
`ls` / `icacls` 都"拒绝访问"，pytest 建编号目录时在 **setup** 就被拒 ——
实测一次全量跑 **1693 条全部 ERROR**，`--cov` 出来 23%，而 ruff / mypy / selftest 全绿。
"门禁只认退出码"这条铁律当时把一个环境故障读成了一场代码雪崩。
所以临时根不能"写死一个路径就信它"，必须**先探再选**，探不到就退到检出外，
并且大声说清退到了哪里 —— 静默退让会让人以为那 23% 是覆盖率真值。

调用方显式设了 `PYTEST_DEBUG_TEMPROOT` 时尊重其选择（`tests/test_env_isolation_meta.py`
的夹具靠这条把临时根关在自己的 tmp_path 里）。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

_REPO = Path(__file__).resolve().parent
_DEFAULT_NAME = ".pytest_tmp"


def _usable(root: Path) -> bool:
    """能不能真的在这里跑出 pytest 需要的那套目录（不是"能不能 stat 到"）。

    三步都必须过，缺一步就会假绿：
    1. 建得出编号目录的落点（`pytest-of-<用户>` 就建在这里）；
    2. 在**里面**再建一层（pytest 建 `pytest-N` 时不带 parents，坏 ACL 在这一步炸）；
    3. 能枚举同级条目 —— pytest 排编号时要 iterdir 父目录，而本机那次事故正是
       "能 stat 到、读不动"（`ls` 与 `icacls` 都拒绝访问），只测前两步会把它判成可用，
       于是 1693 条继续全卡在 setup，而 ruff/mypy 全绿。
    """
    probe = root / f"_probe-{os.getpid()}"
    try:
        probe.mkdir(parents=True, exist_ok=True)
        (probe / "inner").mkdir(parents=False, exist_ok=False)
        list(root.iterdir())
    except OSError:
        return False
    finally:
        shutil.rmtree(probe, ignore_errors=True)
    return True


def _candidates() -> list[Path]:
    # ⚠️ 盘符必须带分隔符：`Path("D:") / "tmp"` 是**盘相对**路径（等价于"当前在 D: 的
    # 工作目录下的 tmp"），实测会把退路目录建进检出目录里 —— 正是这条改动要避免的事。
    drive, _tail = os.path.splitdrive(str(_REPO))
    drive_root = Path(drive + os.sep) if drive else Path(os.sep)
    return [
        _REPO / _DEFAULT_NAME,
        drive_root / "tmp" / "pytest-prompt-master",
        Path(tempfile.gettempdir()) / "pytest-prompt-master",
    ]


def choose_temp_root(environ: dict[str, str]) -> tuple[Path, str | None]:
    """返回 (临时根, 警告文案或 None)。文案非空当且仅当默认位不可用、真退走了。"""
    explicit = environ.get("PYTEST_DEBUG_TEMPROOT")
    if explicit:
        return Path(explicit), None  # 调用方负责，与旧 setdefault 语义逐字一致

    cands = _candidates()
    default = cands[0]
    for cand in cands:
        if _usable(cand):
            if cand == default:
                return cand, None
            return cand, (
                f"pytest 临时根 {default} 不可写（多半是宿主 ACL/杀软把它打坏了），"
                f"已退到 {cand}。不退的后果：全部用例在 setup 就 ERROR，"
                "而静态门禁全绿 —— 别把那种红读成代码坏了。"
            )
    return default, (
        f"pytest 临时根 {default} 不可写，且检出外也没有可用位置 —— "
        "用例会大面积卡在 setup，请手工设 PYTEST_DEBUG_TEMPROOT 指向一个 ASCII 可写目录。"
    )


if os.name == "nt":
    _root, _warn = choose_temp_root(os.environ)
    os.environ.setdefault("PYTEST_DEBUG_TEMPROOT", str(_root))
    if _warn:
        print(f"[conftest] {_warn}", file=sys.stderr)
