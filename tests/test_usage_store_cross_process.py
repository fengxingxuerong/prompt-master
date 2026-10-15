"""`usage_store` 跨进程写那两条防线的守卫（同族事故的第三形，2026-10-15）。

背景（真事故，不是推演）：`data/usage_daily.json` 在 2026-10-15 22:25~23:02 连着
5 次被判损坏，报错一律是 `Expecting property name enclosed in double quotes: line 1
column 3` —— 那是"两个 JSON 对象首尾相接"的形状，只能来自**两个进程写同一个临时文件**。
当时 `record_call` 两个毛病叠在一起：

1. 临时文件名写死 `usage_daily.json.tmp`（`vkeys` 2026-09-25 已经为同一件事出过一次，
   当时就改成带 pid，这里没跟上）；
2. 只锁本进程（`_LOCK` 是 threading.Lock），`--workers > 1` 时两个 worker 各读各的整份、
   各写各的整份 —— 就算临时文件不互踩，笔数照样丢。

所以本文件盯两件事：**临时文件必须唯一**（进程内确定性断言）与**跨进程必须不丢笔、
不许损坏**（真起两个解释器）。不制造假竞态来"假装复现"。

注意：修法不是在这里重写一遍锁，而是与 `vkeys` 共用 `pm/modelhub/filelock.py` 那份——
同族事故抄第三遍就会漂，这是本仓已经付过学费的教训。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from pm.modelhub import usage_store as US

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    d = tmp_path / "us-data"
    d.mkdir()
    monkeypatch.setenv("PMH_DATA_DIR", str(d))
    return d


def _record(n: int = 1) -> None:
    for _ in range(n):
        US.record_call(
            model="m1",
            agent="bot",
            role="assistant",
            success=True,
            latency_ms=1,
            failovers=0,
            content_chars=1,
        )


# --------------------------------------------------------------------------
# 防线一：临时文件不许是固定名
# --------------------------------------------------------------------------


def test_record_call_replaces_a_pid_unique_temp_file(
    monkeypatch: pytest.MonkeyPatch, isolated: Path
) -> None:
    """写盘所用的临时文件必须带 pid，**不能**是 `usage_daily.json.tmp` 那个固定名。

    固定名的后果不是"可能冲突"，是必然交错：两个进程同时 `write_text` 同一个路径，
    盘上留下的是半截 JSON，下一读就整份判损坏（当天那 5 条 ALERT 全这个形状）。
    判据取 `os.replace` 的**源文件名**，绕过的是"实现细节不可见"这种借口。
    """
    real = os.replace
    seen: list[str] = []

    def spy(src: object, dst: object, *a: object, **kw: object) -> None:
        if str(dst).endswith("usage_daily.json"):
            seen.append(Path(str(src)).name)
        real(src, dst, *a, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "replace", spy)
    _record()

    assert seen, "record_call 必须真的走一次原子替换（否则这条守卫是空的）"
    name = seen[0]
    assert name != "usage_daily.json.tmp", (
        f"临时文件又成了固定名 {name} —— 两个进程会写同一个文件，"
        "2026-10-15 那 5 条 corrupt 就是这么来的"
    )
    assert str(os.getpid()) in name, f"临时文件名要能区分写者：{name}"
    assert not list(isolated.glob("*.tmp")), "写完之后临时文件不许留在盘上"


# --------------------------------------------------------------------------
# 防线二：跨进程锁真的挂在写入路径上
# --------------------------------------------------------------------------


def test_record_call_holds_the_cross_process_lock(
    monkeypatch: pytest.MonkeyPatch, isolated: Path
) -> None:
    """读整份 → 改 → 写整份必须整段在跨进程锁里，且锁挂在日聚合文件本身。

    进程内的 `_LOCK` 单独用是不行的：`--workers > 1` 时每个进程各有一份，
    两个 worker 同时写会互相整份覆盖（vkeys 侧实测两进程各发 150 把只剩 126~151 把）。
    这里不验"锁能锁住"（那是 `test_modelhub_contention_paths.py` 的事），
    只验**这条写路径真的接在锁上** —— 接没接是最容易在重构里被摘掉的东西。
    """
    calls: list[Path] = []
    real = US._process_lock

    def spy(path: Path, **kw: object) -> object:
        calls.append(Path(path))
        return real(path, **kw)  # type: ignore[arg-type,call-arg]

    monkeypatch.setattr(US, "_process_lock", spy)
    _record(2)

    assert len(calls) == 2, f"每一笔都要独立进出临界区，实际只锁了 {len(calls)} 次"
    assert all(p.name == "usage_daily.json" for p in calls), f"锁挂错了文件：{calls}"


# --------------------------------------------------------------------------
# 防线三：真起两个解释器，量"丢不丢笔、坏不坏"
# --------------------------------------------------------------------------


def test_two_processes_do_not_corrupt_or_lose_usage(isolated: Path) -> None:
    """两个进程各记 60 笔：盘上必须 120 笔、不许出现 .corrupt-*、不许有 tmp 残留。

    为什么必须真起两个解释器：进程内的锁跨不了进程，Windows 上不同句柄的字节锁在
    同进程内也互相不冲突 —— 用线程或串行 subprocess 跑，锁被摘掉也照样绿。
    这一条是这条修复**唯一的端到端证据**，别用"看起来对"的桩替掉它。
    """
    child = isolated / "usage_child.py"
    child.write_text(
        "import sys\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "from pm.modelhub import usage_store as US\n"
        "for _ in range(int(sys.argv[2])):\n"
        "    US.record_call(model='m1', agent='bot', role='assistant', success=True,"
        " latency_ms=1, failovers=0, content_chars=1)\n"
        "print('ok')\n",
        encoding="utf-8",
    )
    env = {**os.environ, "PMH_DATA_DIR": str(isolated)}
    procs = [
        subprocess.Popen(
            [sys.executable, str(child), str(ROOT), "60"],
            cwd=str(ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
        for _ in range(2)
    ]
    # 先全起完再收：串行 communicate 等于一个跑完才起下一个，那样什么都测不到
    for p in procs:
        out, err = p.communicate(timeout=120)
        assert p.returncode == 0, f"子进程记账崩了：rc={p.returncode} {out[-300:]} {err[-300:]}"

    day = US.load_daily()
    total = sum(v.get("calls", 0) for v in day.values())
    assert total == 120, (
        f"两个进程各记 60 笔，盘上只有 {total} 笔 —— 跨进程整份覆盖又回来了"
        f"（损坏文件残留数 {len(list(isolated.glob('usage_daily.json.corrupt-*')))}）"
    )
    assert not list(isolated.glob("usage_daily.json.corrupt-*")), "并发写出损坏文件：防线没生效"
    assert not list(isolated.glob("*.tmp")), "并发写留下了临时文件"
