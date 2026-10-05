"""真端点轮次的跨进程串行锁（`PM_LIVE_LOCK`，缺省关闭）。

为什么需要（2026-10-04 实测）：同一工作树里多个会话会各自起真端点 E2E 轮次，
共用同一个 3-Key 池。第六轮 run `ed320306a11d` 的窗口里落了 5 个别的会话的产物
（两轮真调用 11 + 25 次、三次缺 Key 尝试），结果 50 次调用只换来一个**不可引用的噪声带**
（`docs/evaluation.md` §十七·七）。"跑之前确认有没有别人在跑"靠人记是记不住的——
同日 Round 23/24 那两轮并行就是这么发生的。

三条设计决定：

1. **缺省关闭**。产品侧的并发是有意的（target 模型赛马、网关多 worker），
   全局锁会改掉真实行为。所以只在显式 `PM_LIVE_LOCK=1` 时生效，由
   `scripts/run_round.sh` 与手工起轮次的人打开——**这把锁保护的是"一轮"，不是一次调用**。
2. **锁令牌与持有者信息分成两个文件**。这条是 Windows 语义逼出来的（实测）：
   `msvcrt.locking` 是独占锁，连另一个句柄**读**被锁的那一字节都拒
   （PermissionError 13）。所以同一个文件里既写 PID 又加锁做不到"先抢后写"：
   抢到之后的 `ftruncate` 会被拒，而竞争者去读持有者的 PID 也会被拒，
   于是"谁占着"只能报"未知"。现在 `.live_run.lock` 只锁字节 0、从不写内容，
   PID 写进旁边的 `.live_run.holder`（抢到之后写，竞争者不写 ⇒ 不会覆盖持有者的号）。
3. **拿不到就立刻退出**（`PM_LIVE_LOCK_WAIT` 可选排队秒数）。默认不排队：
   一轮真端点跑几十分钟，排队等于无限期占着终端；宁可让调用方看见"占用中"自己决定。

两个文件都放在 `log_dir()` 下：随 `PM_LOG_DIR` 一起搬，且在 `.gitignore` 的 `logs/` 里——
测试可以放心写，不会把被跟踪文件写脏。
"""

from __future__ import annotations

import contextlib
import os
import time
from collections.abc import Iterator
from pathlib import Path

from .support import log_dir

LOCK_NAME = ".live_run.lock"
HOLDER_NAME = ".live_run.holder"
_POLL_SECONDS = 0.05


class LiveRunBusy(RuntimeError):
    """已经有别的进程占着这把锁。"""


def lock_path() -> Path:
    """锁令牌文件路径。每次现取：`PM_LOG_DIR` 是运行期变量，import 期冻结会分叉。"""
    return log_dir() / LOCK_NAME


def holder_path() -> Path:
    """持有者信息文件路径（PID + 起始时间）。"""
    return log_dir() / HOLDER_NAME


def enabled() -> bool:
    """`PM_LIVE_LOCK` 是否打开。认 1/true/yes/on（大小写不敏感），其余都算关。"""
    return (os.getenv("PM_LIVE_LOCK", "") or "").strip().lower() in {"1", "true", "yes", "on"}


def wait_seconds() -> float:
    """排队等多久。读不出来或没设就是 0（立刻失败）。"""
    raw = (os.getenv("PM_LIVE_LOCK_WAIT", "") or "").strip()
    try:
        value = float(raw)
    except ValueError:
        return 0.0
    return value if value >= 0 else 0.0


def _lock_impl() -> object:
    """返回平台原语模块：Windows 用 msvcrt 字节锁，其余用 fcntl.flock。

    返回模块而不是就地调用：mypy 看不见 `fcntl.flock` / `msvcrt.locking` 的属性
    （桩不全），而这两个模块运行期一定在（标准库自带）。
    """
    if os.name == "nt":
        import msvcrt

        return msvcrt
    import fcntl

    return fcntl


def _try_hold(fd: int) -> bool:
    """试着锁一次；被占着返回 False，退避节奏由调用方控。"""
    mod = _lock_impl()
    try:
        if os.name == "nt":
            mod.locking(fd, mod.LK_NBLCK, 1)  # type: ignore[attr-defined]
        else:
            mod.flock(fd, mod.LOCK_EX | mod.LOCK_NB)  # type: ignore[attr-defined]
        return True
    except OSError:
        return False


def _release(fd: int) -> None:
    mod = _lock_impl()
    with contextlib.suppress(OSError):
        if os.name == "nt":
            mod.locking(fd, mod.LK_UNLCK, 1)  # type: ignore[attr-defined]
        else:
            mod.flock(fd, mod.LOCK_UN)  # type: ignore[attr-defined]


def _note_holder(path: Path) -> None:
    """把本轮 PID 与起始时间写进旁边的 holder 文件。

    写失败只影响"报得出谁占着"，不该让已经拿到锁的运行失败——所以吞掉 OSError。
    """
    with contextlib.suppress(OSError):
        path.write_text(
            f"{os.getpid()} {time.strftime('%Y-%m-%d %H:%M:%S')}\n",
            encoding="utf-8",
        )


def _holder_pid(path: Path) -> str:
    """现任持有者的 PID；读不到就说"未知"，绝不因为报信失败盖掉主错误。"""
    try:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return "未知"
    return text.split()[0] if text else "未知"


@contextlib.contextmanager
def hold(
    *, path: Path | None = None, holders: Path | None = None, timeout: float | None = None
) -> Iterator[int]:
    """占住这把锁；被占着就抛 `LiveRunBusy`，消息里点名持有者。

    `path` / `holders` 只为测试与特殊目录留的口；缺省都取 `log_dir()` 下的约定文件名。
    """
    token = path if path is not None else lock_path()
    record = holders if holders is not None else holder_path()
    budget = wait_seconds() if timeout is None else timeout
    token.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(token), os.O_RDWR | os.O_CREAT, 0o600)
    deadline = time.monotonic() + budget
    try:
        while not _try_hold(fd):
            if time.monotonic() >= deadline:
                raise LiveRunBusy(
                    f"{token} 被 PID {_holder_pid(record)} 占着（已等 {budget:.1f}s）——"
                    "另一轮真端点运行还没收口。要么等它结束，"
                    "要么显式取消 PM_LIVE_LOCK 并承担并发污染噪声带的后果"
                )
            time.sleep(_POLL_SECONDS)
        # 抢到之后才登记：这样 holder 里的号永远是"现在占着的人"，
        # 而不是刚才来抢、失败退出的那个人
        _note_holder(record)
        yield os.getpid()
    finally:
        _release(fd)
        os.close(fd)
