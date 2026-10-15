"""JSON 台账的跨进程写设施：跨进程锁 + 临时文件唯一的原子写。

为什么单独成模块：这两件事在仓里已经**各出过一次事**，且两次是同族事故的不同形——

1. `data/vkeys.json`（2026-09-25 / 2026-10-13）：固定临时文件名让两个写者互踩，
   加上没有跨进程锁 ⇒ 两进程各发 150 把，盘上只剩 126~151 把，3 轮里 2 轮有一路崩在
   "文件不可读"。
2. `data/usage_daily.json`（2026-10-15 凌晨实测）：`usage_store` 自己又写了一遍固定名字
   的 `.json.tmp`、又没上跨进程锁 ⇒ 两个进程交错写同一个临时文件，盘上出现
   `Expecting property name enclosed in double quotes: line 1 column 3` 这种
   "两个对象首尾相接"的半截 JSON，`_load()` 只能整份丢弃重建，当天 22:25~23:02 共 5 条
   `usage_store ALERT`。

同一份实现抄第三遍就会漂，所以搬到一处：`vkeys` 与 `usage_store` 都从这里取。
⚠️ 本模块只管"怎么写得安全"，不管"写什么"——损坏后的处置（告警/重建/抛错）是各自的
业务口径，留在调用侧（见 `usage_store._load` 与 `vkeys._load_json_or` 的相反选择）。
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .pool import ConfigError

__all__ = ["_atomic_write", "_lock_impl", "_process_lock", "_try_lock", "_unlock"]


def _lock_impl() -> Any:
    """跨进程锁的原语：Windows 用 msvcrt 字节锁，其余用 fcntl.flock。

    返回模块对象而不是就地调用：本机 mypy 看不见 `fcntl.flock` / `msvcrt.locking`
    的属性（桩不全），而运行期这两个模块一定都在（标准库自带）。
    """
    if os.name == "nt":
        import msvcrt

        return msvcrt
    import fcntl

    return fcntl


def _try_lock(fd: int) -> bool:
    """试着拿一次锁，被别的进程占着就返回 False —— 退避节奏由调用方控。

    不用 `msvcrt.LK_LOCK`：它内置"每 1 秒重试、共 10 次"，粒度比我们的写入耗时大三个数量级。
    """
    mod = _lock_impl()
    try:
        if os.name == "nt":
            mod.locking(fd, mod.LK_NBLCK, 1)
        else:
            mod.flock(fd, mod.LOCK_EX | mod.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock(fd: int) -> None:
    mod = _lock_impl()
    try:
        if os.name == "nt":
            mod.locking(fd, mod.LK_UNLCK, 1)
        else:
            mod.flock(fd, mod.LOCK_UN)
    except OSError:
        pass  # 没拿到过 / 已被回收：释放失败也不能盖掉真正的写失败


@contextlib.contextmanager
def _process_lock(path: Path, *, timeout: float = 2.0) -> Iterator[None]:
    """跨进程独占地把"读整份 → 改 → 写整份"关进一个临界区。

    同进程的那把锁只串得起一个进程里的写者；`--workers > 1` 时每个进程各有一份锁，
    于是两个 worker 同时写会互相整份覆盖。锁挂在同名 `.lock` 文件上（OS 在进程退出时
    自动释放，所以不需要陈旧检测），拿不到就退避重试；超时显式报错，不静默降级成"没锁"。

    2026-10-13 补：**打开锁文件本身**也会竞争 —— 两个进程同时创建同一个新文件时，
    实测有一方在 `os.open(O_CREAT)` 上拿到瞬态 `PermissionError(13)`，立刻重试就成功
    （最小探针：两进程各自 open+lock+unlock 200 轮，6 轮里 2 例首个 open 即 EACCES；
    集成侧同形：两进程各发 60 把的压测 5 轮里 2 例签发者当场崩）。
    所以 open 与拿字节锁共用同一个 `timeout` 预算：总等待时间不变，超时给显式错误。
    """
    lock_path = path.with_name(path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    while True:
        try:
            fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
            break
        except PermissionError:
            # 瞬态占用（并发创建/杀软索引器）：让一下，别把合法写者打死。
            if time.monotonic() >= deadline:
                raise ConfigError(
                    f"打不开跨进程锁文件（>{timeout:.1f}s 持续 EACCES）：{lock_path}——"
                    "查它是不是只读属性或被杀软钉着；别改成无限等"
                ) from None
            time.sleep(0.02)
    try:
        while not _try_lock(fd):
            if time.monotonic() >= deadline:
                raise ConfigError(
                    f"拿不到跨进程写锁（>{timeout:.1f}s）：{lock_path}——"
                    "有别的进程长时间占着它；先查是不是卡住的写者，别改成无限等"
                )
            time.sleep(0.02)
        yield
    finally:
        _unlock(fd)
        os.close(fd)


def _atomic_write(path: Path, data: dict[str, Any]) -> None:
    """写盘：临时文件名带 pid+线程 id，`os.replace` 对瞬时占用退避重试。

    固定写 `<file>.json.tmp` 时，同目录下两个写者（多进程部署就是两个 worker，
    或本进程里两个 store 实例）用的是**同一个临时文件**——互相踩完再 replace，
    于是盘上出现"两个 JSON 首尾相接"的半截内容（usage_store 2026-10-15 那 5 条
    ALERT 就是这个形状），vkeys 侧则实测抛 `PermissionError(13)`：两个实例各发 200 把，
    一路 worker 当场死掉，另一把密钥静默消失（发出去了、盘上没有）。
    名字唯一之后两路写互不相干；replace 仍可能被杀软/索引器瞬时占用，所以退避几次，
    全失败则删掉自己的临时文件并把最后一次错误抛出去（不静默）。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    last: OSError | None = None
    for attempt in range(5):
        try:
            os.replace(tmp, path)
            return
        except PermissionError as e:  # Windows 共享冲突是瞬时的：让一秒，别丢数据
            last = e
            time.sleep(0.02 * (attempt + 1))
    with contextlib.suppress(OSError):
        tmp.unlink()
    assert last is not None
    raise last
