"""`pm/modelhub/vkeys.py` 里"争用/退避"那几条防御分支的**确定性**回归（进程内，零出网）。

为什么补这块（2026-10-04）：同一份代码连跑两遍全量覆盖率，`vkeys.py` 的 miss 数在
27 与 30 之间摆，逐行比对后差值精确落在 `_atomic_write` 的 `except PermissionError`
那三条（188-190）上 —— 也就是说**这条分支过去只是碰巧被并发用例撞上才被覆盖**。
碰巧覆盖有两个坏处：
① 覆盖率成了一个不可复现的指标，任何"比上一轮涨/跌了几个点"的说法都失去依据；
② 谁把这段退避删掉，套件在多数机器上照样全绿 —— 而它守的是实测过的事故
（`os.replace` 撞 Windows 共享冲突时，一路 worker 当场死掉、另一把密钥静默消失）。

所以这里**不制造真竞态**，而是把"瞬时被拒"直接注入进去，让每条防御分支每次都走一遍：
被拒一次 → 必须让、必须写成功；一直拒 → 必须报错、必须不动老数据、必须不留临时文件。
判据都取"它有没有按设计退避"这种可断言的形式，不靠时长（真 sleep 会让这组用例拖到秒级）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from pm.modelhub import vkeys as V
from pm.modelhub.pool import ConfigError

BUSY = PermissionError(13, "另一个程序正在使用此文件")


@pytest.fixture
def isolated_data(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """台账落到 tmp_path：这些用例会真的写盘，绝不能碰仓库里的 data/*.json。"""
    d = tmp_path / "vh-data"
    d.mkdir()
    monkeypatch.setenv("PMH_DATA_DIR", str(d))
    return d


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """把退避用的 sleep 换成记账表：测"让了几次、每次多久"，不真等。"""
    calls: list[float] = []
    monkeypatch.setattr(V.time, "sleep", calls.append)
    return calls


def _deny_replace_once(monkeypatch: pytest.MonkeyPatch, real: Any) -> dict[str, int]:
    state = {"n": 0}

    def flaky(src: Any, dst: Any, *a: Any, **kw: Any) -> None:
        state["n"] += 1
        if state["n"] == 1:
            raise BUSY
        real(src, dst, *a, **kw)

    monkeypatch.setattr(V.os, "replace", flaky)
    return state


# --------------------------------------------------------------------------
# _atomic_write：replace 被瞬时拒绝
# --------------------------------------------------------------------------


def test_atomic_write_backs_off_then_succeeds_on_transient_denial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    """第一次 `os.replace` 被拒时必须让一下再重试，数据要落全。

    这三行（188-190）就是覆盖率两遍摆动的来源。删掉这段退避，本机多数轮次照样全绿，
    而并发签发时"密钥发出去了、盘上没有"会回来。
    """
    target = tmp_path / "vkeys.json"
    state = _deny_replace_once(monkeypatch, os.replace)

    V._atomic_write(target, {"version": 1, "keys": {"vk-1": {"agent": "a"}}})

    assert state["n"] == 2, "被拒一次之后必须真的重试过"
    assert json.loads(target.read_text(encoding="utf-8"))["keys"] == {"vk-1": {"agent": "a"}}
    assert sleeps == [0.02], f"退避节奏是设计的一部分，实测 {sleeps}"
    assert not list(tmp_path.glob("*.tmp")), "成功路径上不该留下自己的临时文件"


def _deny_replace_always(*a: Any, **kw: Any) -> None:
    raise BUSY


def test_atomic_write_surfaces_exhausted_denial_without_touching_the_old_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    """五次全被拒：抛出去（不静默），老文件原样不动，临时文件自己清掉。

    这里最容易写成"吞掉异常继续跑"——那正是本模块修过的那个静默丢密钥形状。
    """
    target = tmp_path / "vkeys.json"
    target.write_text("SENTINEL", encoding="utf-8")
    monkeypatch.setattr(V.os, "replace", _deny_replace_always)

    with pytest.raises(PermissionError):
        V._atomic_write(target, {"version": 1, "keys": {}})

    assert target.read_text(encoding="utf-8") == "SENTINEL", "写失败不能把已存在的台账改成半截"
    assert len(sleeps) == 5, f"退避要打满五轮才判失败，实测 {sleeps}"
    assert not list(tmp_path.glob("*.tmp")), "失败路径也必须清掉临时文件，别留垃圾进 git"


# --------------------------------------------------------------------------
# _load_json_or：读被瞬时拒绝 / 内容形状不对
# --------------------------------------------------------------------------


class _FlakyFile:
    """只对指定路径的前 N 次 read_text 抛 PermissionError，其余调用原样放行。

    为什么不用真文件锁：Windows 上"文件被别人锁住"没法在单进程里可靠复现，
    而那正是这段代码存在的原因（`os.replace` 进行中会瞬时拒绝读）。
    """

    def __init__(self, path: Path, fails: int) -> None:
        self.path = path
        self.fails = fails
        self.reads = 0

    def exists(self) -> bool:
        return self.path.exists()

    def read_text(self, encoding: str = "utf-8") -> str:
        self.reads += 1
        if self.reads <= self.fails:
            raise BUSY
        return self.path.read_text(encoding=encoding)


def test_load_json_backs_off_while_another_writer_is_replacing(
    tmp_path: Path, sleeps: list[float]
) -> None:
    """前两次读被拒（别人的 replace 正在飞）：让过之后要把数据读出来，不能判"损坏"。"""
    p = tmp_path / "agents.json"
    p.write_text(json.dumps({"agents": {"bot": {}}}), encoding="utf-8")
    flaky = _FlakyFile(p, fails=2)

    data = V._load_json_or(flaky, {"agents": {}}, "智能体注册表")  # type: ignore[arg-type]

    assert data == {"agents": {"bot": {}}}
    assert sleeps == [0.02, 0.04], f"退避是递增的，实测 {sleeps}"


def test_load_json_reports_other_io_errors_as_unreadable(tmp_path: Path) -> None:
    """非"被占用"的 OSError（权限、路径失效、盘没了）直接判不可读并带上原因。

    这条与"重试 5 次仍被占用"是两种话：前者一次都不必重试，后者说明有写者卡住了。
    合成一类就会让人去查错方向（实测排查时这两种文案救过场）。
    """
    p = tmp_path / "vkeys.json"
    p.write_text("{}", encoding="utf-8")

    class _Unreadable(_FlakyFile):
        def read_text(self, encoding: str = "utf-8") -> str:
            raise OSError(22, "路径失效")

    with pytest.raises(ConfigError, match="不可读："):
        V._load_json_or(_Unreadable(p, fails=0), {"keys": {}}, "虚拟密钥库")  # type: ignore[arg-type]


def test_load_json_raises_after_exhausting_retries(tmp_path: Path, sleeps: list[float]) -> None:
    p = tmp_path / "agents.json"
    p.write_text(json.dumps({"agents": {"bot": {}}}), encoding="utf-8")
    flaky = _FlakyFile(p, fails=99)

    with pytest.raises(ConfigError, match="重试 5 次仍被占用"):
        V._load_json_or(flaky, {"agents": {}}, "智能体注册表")  # type: ignore[arg-type]

    assert len(sleeps) == 5


def test_load_json_distinguishes_empty_missing_and_corrupt(tmp_path: Path) -> None:
    """三种"读不出内容"是三种事：空文件=缺省值、不存在=缺省值、非 JSON=报错并指行号。

    把这三种并到一个分支里是最常见的写法，而那会把"账本被截断成空文件"读成"没有密钥"，
    于是运维看到的是一个静默的空库而不是错误。
    """
    empty = tmp_path / "empty.json"
    empty.write_text("   ", encoding="utf-8")
    assert V._load_json_or(empty, {"keys": {}}, "虚拟密钥库") == {"keys": {}}

    missing = tmp_path / "missing.json"
    assert V._load_json_or(missing, {"keys": {}}, "虚拟密钥库") == {"keys": {}}

    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{ broken", encoding="utf-8")
    with pytest.raises(ConfigError, match="损坏（不是合法 JSON）"):
        V._load_json_or(corrupt, {"keys": {}}, "虚拟密钥库")

    array = tmp_path / "array.json"
    array.write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(ConfigError, match="顶层必须是 JSON 对象"):
        V._load_json_or(array, {"keys": {}}, "虚拟密钥库")


# --------------------------------------------------------------------------
# 跨进程写锁：拿不到 / 释放失败
# --------------------------------------------------------------------------


class _BusyLockModule:
    """假的原语模块：两个方向的调用都报"占用"。"""

    LK_NBLCK = 0x1
    LK_UNLCK = 0x2
    LOCK_EX = 0x2
    LOCK_NB = 0x4
    LOCK_UN = 0x8

    def locking(self, fd: int, mode: int, size: int) -> None:
        raise OSError(22, "被别的进程占着")

    def flock(self, fd: int, mode: int) -> None:
        raise OSError(11, "被别的进程占着")


def test_try_lock_reports_busy_as_false_instead_of_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    """被占用要返回 False 让调用方退避；抛出去会把"另一个写者正在写"变成 500。"""
    monkeypatch.setattr(V, "_lock_impl", lambda: _BusyLockModule())
    assert V._try_lock(3) is False


def test_unlock_failure_never_replaces_the_real_write_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """释放锁失败必须咽掉：它一旦抛出，盖掉的是真正那个写失败。"""
    monkeypatch.setattr(V, "_lock_impl", lambda: _BusyLockModule())
    V._unlock(3)


def test_process_lock_times_out_with_an_explicit_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    """超时报错要点名"跨进程写锁"并给出下一步，而不是无限等或静默当成没锁。"""
    monkeypatch.setattr(V, "_try_lock", lambda fd: False)
    path = tmp_path / "vkeys.json"

    with pytest.raises(ConfigError, match="拿不到跨进程写锁"), V._process_lock(path, timeout=0.05):
        pass  # pragma: no cover —— 拿到锁才会进来，这里断言它进不来

    assert sleeps, "等待期间必须按节奏退避，而不是空转刷 CPU"


def test_process_lock_uses_the_sibling_lock_file_and_releases_it(tmp_path: Path) -> None:
    """正对照：真原语真锁一次，锁文件挂在同名 .lock 上（不这么写的话上面的桩会自证）。"""
    path = tmp_path / "vkeys.json"
    with V._process_lock(path, timeout=1.0):
        assert (tmp_path / "vkeys.json.lock").exists()
    # 退出后同一把锁可以再拿一次：没释放干净会在这里挂住并超时
    with V._process_lock(path, timeout=1.0):
        pass


# --------------------------------------------------------------------------
# 台账形状：库读到的必须是对象
# --------------------------------------------------------------------------


def test_keystore_refuses_a_ledger_whose_keys_is_not_an_object(isolated_data: Path) -> None:
    """`keys` 不是对象时报错，而不是把半截文件当"没有密钥"继续发。"""
    (isolated_data / "vkeys.json").write_text(
        json.dumps({"version": 1, "keys": []}), encoding="utf-8"
    )
    with pytest.raises(ConfigError, match="keys 必须是对象"):
        V.VirtualKeyStore()


def test_agent_store_refuses_a_ledger_whose_agents_is_not_an_object(isolated_data: Path) -> None:
    (isolated_data / "agents.json").write_text(
        json.dumps({"version": 1, "agents": "bot"}), encoding="utf-8"
    )
    with pytest.raises(ConfigError, match="agents 必须是对象"):
        V.AgentStore()


def test_agent_store_rejects_a_blank_name(isolated_data: Path) -> None:
    """空名会写出一条谁都查不到的注册记录，所以直接拒。"""
    store = V.AgentStore()
    with pytest.raises(ConfigError, match="智能体名称不能为空"):
        store.register("   ")
