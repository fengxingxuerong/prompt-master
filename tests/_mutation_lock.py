"""变异类用例的**跨进程互斥**与**唯一备份名**（2026-10-17）。

## 为什么需要这个助手

变异类用例（`test_guard_mutation.py` / `test_hooks_actually_block.py`）的判据形态是
"**把真文件改坏 → 跑工具 → 断言工具能红 → 还原**"。这是本仓刻意的设计：变异必须打在
真判据上，否则测的是影子。代价是它们**会写仓库内的被跟踪文件**。

于是两个 pytest 进程并发时必然互踩（本机实测：两个全量套件并发，两边各自红一片，
而**单跑每个文件都是绿的**）：

- A 进程把 `pm/prompts.py` 污染到命中规则；
- B 进程的 `prompt-gate` 恰好这时读文件 ⇒ B 报"工作树是干净的，prompt-gate 却 rc=1"
  （把自己的环境误判成回归）；
- A 先还原并删掉 `.hooksbak`，B 随后想还原却发现备份没了。

这与本仓反复修过的同族缺陷同形（vkeys 2026-09-25 固定临时名互踩、usage_store
2026-10-15、ledger 2026-10-16 追加写无跨进程锁），只是这次落在测试基建上。

## 用法

    from _mutation_lock import mutate   # tests/ 已在 sys.path（pytest rootdir）

    with mutate(PROMPTS):          # 持跨进程锁 + 按字节还原
        PROMPTS.write_text(poisoned, encoding="utf-8", newline="")
        proc = run_hook("prompt-gate")
    assert proc.returncode != 0

锁包住的是**整段**（改 + 跑 + 还原）。只包还原等于没包 —— 污染的窗口正是
"改完之后、还原之前"，另一个进程恰在那段时间读文件。

不另写一份锁实现：直接复用生产侧的 `pm.modelhub.filelock._process_lock`
（同族实现抄第二遍必然分叉，本仓为此付过学费）。也刻意**不**用 pytest 夹具做这件事：
夹具的作用域挡不住另一个 pytest 进程。
"""

from __future__ import annotations

import contextlib
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from pm.modelhub.filelock import _process_lock

# 锁的等待预算：一个变异用例最多改一个文件、跑一次子进程（实测单条 <10s），
# 给足 120s 免得正常排队被判超时；超时是显式报错（ConfigError），不静默降级成"没锁"。
_LOCK_TIMEOUT = 120.0


def mutation_lock_path(target: Path) -> Path:
    """锁挂在**仓库根**下一个稳定名字上，而不是目标文件旁边。

    为什么不用目标文件同级：变异过程会临时替换被跟踪文件，而 `git diff --exit-code`
    那类守卫（`releases-data-clean`、运营数据检查）会把多出来的 `.lock` 当成脏树。
    锁文件放仓库根、且名字固定，就对"文件内容"这件事完全无痕。
    """
    return Path(__file__).resolve().parents[1] / ".mutation.lock"


@contextmanager
def mutate(*targets: Path) -> Iterator[None]:
    """持跨进程锁，把「改 → 跑 → 还原」整段关进临界区；退出时按**字节**还原。

    按字节还原是硬要求（不是洁癖）：Windows 上 `write_text` 默认把 `\\n` 写成
    `\\r\\n`，于是"还原"出来的文件与 HEAD 有差异 —— `git status` 一直显示 modified，
    而 `git diff` 看不到内容变化（本仓 2026-10-05 实际踩过，探针跑三轮才发现污染了工作树）。
    所以这里读 `read_bytes()`、写回用 `write_bytes()`，并在写入前也**不许**经过文本层。

    ⚠️ **原文必须在锁内读**（不是加锁前）：变异用例的原文来自各自的 fixture，而 fixture
    在拿锁**之前**就执行了。若另一个进程正持锁改着同一个文件，先执行的那个 fixture 读到的
    可能是**被污染版本** ⇒ 等锁后一断言就成了"锚点已变，变异测试失效"（本机实测到过）。
    所以 `mutate()` 把"进锁 → 记录原文 → 交给调用方 → 还原"做成一个原子段，
    并额外提供 `original` 属性**在锁内**给调用方，避免用 fixture 里那份过期原文。
    """
    originals: dict[Path, bytes] = {}
    lock = mutation_lock_path(targets[0])
    with _process_lock(lock, timeout=_LOCK_TIMEOUT):
        try:
            for t in targets:
                originals[t] = t.read_bytes()
            yield LockedOriginals(originals)
        finally:
            for t, raw in originals.items():
                # 还原本身也不许抛：真失败要让原异常继续往上走，别被这里盖掉。
                # 用唯一临时名 + os.replace，避免"写一半被杀"把原文件留成半截。
                tmp = t.with_name(f"{t.name}.{os.getpid()}.{threading.get_ident()}.restore")
                try:
                    tmp.write_bytes(raw)
                    os.replace(tmp, t)
                except OSError:  # pragma: no cover - 还原失败属环境故障，尽力而为
                    with contextlib.suppress(OSError):
                        tmp.unlink()


class LockedOriginals:
    """锁内读到的原文快照。

    `text(path)` 返回**归一化换行**后的文本（LF），`raw(path)` 返回原始字节。
    两者都必须取自**进锁之后**那一刻。

    ⚠️ 为什么 `text()` 要归一化换行（2026-10-17 实测踩到）：`ci.yml` 与 `conftest.py`
    在本机是 **CRLF 检出**，而变异锚点按 LF 写死。早先直接 `read_bytes().decode()`
    给出的是 CRLF 文本 ⇒ `anchor in text` 全部失配，报成"锚点已变，变异测试失效"
    （看起来像锚点过期，其实是换行风格问题）。
    归一化只影响**读**；写回由 `mutate()` 用原始字节 `raw()` 负责，所以磁盘上的
    换行风格不会被这次读写改掉。
    """

    def __init__(self, originals: dict[Path, bytes]) -> None:
        self._originals = originals

    def raw(self, path: Path) -> bytes:
        """该文件进锁时的原始字节（写坏时用它做基底，换行风格原样保留）。"""
        return self._originals[path]

    def text(self, path: Path) -> str:
        """该文件进锁时的原文，换行已归一化为 LF（便于按 LF 锚点做替换）。"""
        return self._originals[path].decode("utf-8").replace("\r\n", "\n")


def restore_bytes(path: Path, raw: bytes) -> None:
    """按字节还原一个文件（`mutate()` 之外的场景，例如夹具里已经存了原文）。"""
    path.write_bytes(raw)


def cleanup_backup(path: Path) -> None:
    """删除遗留的备份文件（旧实现用固定名 `.hooksbak`，并发时会互相删）。

    保留这个函数只为兼容旧调用点的迁移；新代码不该再用固定名备份 ——
    `mutate()` 在内存里保存原文，根本不落备份文件。
    """
    with contextlib.suppress(OSError):
        path.unlink()


__all__ = ["cleanup_backup", "mutate", "mutation_lock_path", "restore_bytes"]
