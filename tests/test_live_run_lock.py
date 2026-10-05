"""`pm/cli/live_lock.py` 与 `run.py` 那条串接的回归（进程内 + 真跨进程，零出网）。

为什么补这块（2026-10-04 实测）：同一工作树里多个会话会各自起真端点 E2E 轮次，
共用同一个 3-Key 池。第六轮 run `ed320306a11d` 的窗口里落了 5 个别的会话的产物
（两轮真调用 11 + 25 次、三次缺 Key 尝试），50 次调用只换来一个**不可引用的噪声带**
（`docs/evaluation.md` §十七·七）。"跑之前确认没人同时在跑"靠人记不住——
同日那两轮并行（§十七·五 / §十七·六）就是这么撞上的。所以做成一把锁，
并且按本仓库的规矩：**先证明它真会拦，再宣称它在保护**。

三层判据的分工：
- 语义层：抢到才写 PID、释放后可再抢、被占时报的是**持有者**的 PID。
- 真并发层：子进程持锁、父进程来抢，双向都对得上（in-process 嵌套里两个 PID 相同，
  报错了也看不出"先写再抢"这个设计错误）。
- 入口层：`PM_LIVE_LOCK` 开着才串；缺省关着时**一次都不碰锁**（产品并发不许被改掉）。
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pm.cli.main  # noqa: F401 —— `pm.cli` 的 __init__ 用同名函数遮蔽了子模块
import pytest
from pm.cli import live_lock

cli_main = sys.modules["pm.cli.main"]

ROOT = Path(__file__).resolve().parents[1]

# 子进程持锁脚本：占住 → 写 holder → 自报 PID → 等 RELEASE → 释放并打印 RELEASED
HOLDER_SCRIPT = """
import os, pathlib, sys, time
sys.path.insert(0, sys.argv[1])
from pm.cli import live_lock
token = pathlib.Path(sys.argv[2])
record = pathlib.Path(sys.argv[3])
with live_lock.hold(path=token, holders=record):
    pathlib.Path(sys.argv[4]).write_text(str(os.getpid()), encoding="utf-8")
    while not pathlib.Path(sys.argv[5]).exists():
        time.sleep(0.02)
print("RELEASED", flush=True)
"""


def _wait_for(path: Path, deadline: float = 25.0) -> str:
    """等文件出现并返回其内容；超时是"并发用例自己没跑起来"，不是通过。"""
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
        time.sleep(0.02)
    raise AssertionError(f"等 {path} 出现超时（{deadline}s）——并发用例自己没跑起来")


def _busy_holder(**kw: Any) -> Any:
    """假 `hold`：模拟"别人占着"。"""
    raise live_lock.LiveRunBusy("被 PID 4242 占着（已等 0.0s）")


# ---------------------------------------------------------------------------
# 缺省值与 env 解析
# ---------------------------------------------------------------------------


def test_lock_is_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """缺省必须关：产品侧 target 模型赛马、网关多 worker 的并发是有意的。"""
    monkeypatch.delenv("PM_LIVE_LOCK", raising=False)
    assert live_lock.enabled() is False
    for value in ("0", "off", "", "  ", "no", "anything"):
        monkeypatch.setenv("PM_LIVE_LOCK", value)
        assert live_lock.enabled() is False, value
    for value in ("1", "true", "TRUE", "yes", "on"):
        monkeypatch.setenv("PM_LIVE_LOCK", value)
        assert live_lock.enabled() is True, value


@pytest.mark.parametrize(
    "raw,expect",
    [("0.2", 0.2), ("", 0.0), ("abc", 0.0), ("-1", 0.0), ("7", 7.0)],
)
def test_wait_seconds_only_accepts_nonnegative_numbers(
    monkeypatch: pytest.MonkeyPatch, raw: str, expect: float
) -> None:
    """排队秒数读不出来就当 0（立刻失败），不许因为一个坏值变成无限等。"""
    monkeypatch.setenv("PM_LIVE_LOCK_WAIT", raw)
    assert live_lock.wait_seconds() == expect


def test_lock_path_follows_pm_log_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """两个文件都跟着产物目录走。写死"仓库/logs"的话，测试就会去抢真实工作树的锁。"""
    monkeypatch.setenv("PM_LOG_DIR", str(tmp_path / "elsewhere"))
    assert live_lock.lock_path() == tmp_path / "elsewhere" / live_lock.LOCK_NAME
    assert live_lock.holder_path() == tmp_path / "elsewhere" / live_lock.HOLDER_NAME


def test_wait_budget_polls_before_giving_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """给了排队秒数就必须真的退避轮询，而不是看一眼就放弃。

    这条钉的是 `while not _try_hold(fd)` 里那一次 `time.sleep`：
    没有它，这个循环会变成空转刷 CPU（也顺便让对方更快释放不了）。
    """
    record: list[float] = []
    real_sleep = time.sleep

    def spy(seconds: float) -> None:
        record.append(seconds)
        real_sleep(0.01)

    monkeypatch.setattr(live_lock.time, "sleep", spy)
    token = tmp_path / live_lock.LOCK_NAME
    holders = tmp_path / live_lock.HOLDER_NAME
    with (
        live_lock.hold(path=token, holders=holders),
        pytest.raises(live_lock.LiveRunBusy),
        live_lock.hold(path=token, holders=holders, timeout=0.06),
    ):
        pass  # pragma: no cover
    assert record, "排队期间一次都没退避"
    assert set(record) == {live_lock._POLL_SECONDS}


# ---------------------------------------------------------------------------
# 锁本身
# ---------------------------------------------------------------------------


def test_hold_records_the_pid_and_releases_on_exit(tmp_path: Path) -> None:
    token = tmp_path / live_lock.LOCK_NAME
    record = tmp_path / live_lock.HOLDER_NAME

    with live_lock.hold(path=token, holders=record) as holder:
        assert holder == os.getpid()
        assert record.read_text(encoding="utf-8").split()[0] == str(os.getpid())

    # 退出后必须能再抢：抢不到就说明释放没做干净
    with live_lock.hold(path=token, holders=record):
        pass


def test_busy_message_names_holder_and_waited_time(tmp_path: Path) -> None:
    """同进程套两把也要给出可用信息：消息是运维唯一读得到的线索。"""
    token = tmp_path / live_lock.LOCK_NAME
    record = tmp_path / live_lock.HOLDER_NAME
    with (
        live_lock.hold(path=token, holders=record),
        pytest.raises(live_lock.LiveRunBusy) as ei,
        live_lock.hold(path=token, holders=record, timeout=0.0),
    ):
        pass  # pragma: no cover
    msg = str(ei.value)
    assert str(os.getpid()) in msg, "持有者 PID 必须出现在消息里"
    assert "已等 0.0s" in msg


def test_holder_pid_falls_back_to_unknown(tmp_path: Path) -> None:
    """空文件或读不到都只报"未知"——报信失败不许盖掉"被占用"这个主错误。"""
    empty = tmp_path / "empty.holder"
    empty.write_text("", encoding="utf-8")
    assert live_lock._holder_pid(empty) == "未知"
    assert live_lock._holder_pid(tmp_path / "missing.holder") == "未知"


# ---------------------------------------------------------------------------
# 真跨进程并发（不是自我声明）
# ---------------------------------------------------------------------------


def test_a_second_process_cannot_take_the_lock(tmp_path: Path) -> None:
    """两个真进程抢同一把锁：第二个必须被拒，且报的是**第一个**的 PID。

    in-process 嵌套证明不了这条——两个 PID 相同，"先写 PID 再抢"这种写反的设计
    在它里面也看不出来。这里让子进程真的持锁，父进程来抢，
    才能验出消息里的 PID 到底是不是持有者。
    """
    token = tmp_path / live_lock.LOCK_NAME
    record = tmp_path / live_lock.HOLDER_NAME
    held = tmp_path / "HELD"
    release = tmp_path / "RELEASE"

    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            HOLDER_SCRIPT,
            str(ROOT),
            str(token),
            str(record),
            str(held),
            str(release),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(ROOT),
    )
    try:
        holder_pid = _wait_for(held)
        assert holder_pid.isdigit(), f"子进程自报的 PID 不是数字：{holder_pid!r}"
        with (
            pytest.raises(live_lock.LiveRunBusy) as ei,
            live_lock.hold(path=token, holders=record, timeout=0.0),
        ):
            pass  # pragma: no cover
        msg = str(ei.value)
        # 用子进程**自报**的 PID 比对，不用 `Popen.pid`：venv 的 python.exe 会再派生真解释器，
        # Popen 报的可能是中间进程（2026-10-05 实测：holder 自报 21256 而 Popen.pid=26720）。
        # 这条断言真正钉的是"holder 由持锁者写"：竞争者若在抢锁前就写 holder，
        # 这里会读到自己的号，"谁占着"就成了假信息。
        assert holder_pid in msg, f"消息该点名持有者 {holder_pid}，实测：{msg}"
        assert str(os.getpid()) not in msg, "报了自己的 PID：说明是抢锁前先写 PID，设计被写反了"

        release.write_text("go", encoding="utf-8")
        out, err = child.communicate(timeout=25)
        assert "RELEASED" in out, f"子进程没能正常释放：{(err or '')[-300:]}"

        # 对方退出后必须抢得到（OS 会释放进程锁），否则这道门会永久卡死
        with live_lock.hold(path=token, holders=record, timeout=0.0):
            pass
    finally:
        if child.poll() is None:
            release.write_text("go", encoding="utf-8")
            child.communicate(timeout=25)


# ---------------------------------------------------------------------------
# 入口那条串接（`run.py` 同步路径）
# ---------------------------------------------------------------------------


def _fake_args(**over: Any) -> SimpleNamespace:
    base: dict[str, Any] = {"json": False}
    base.update(over)
    return SimpleNamespace(**base)


def test_serialized_runs_inside_the_lock_and_releases_after(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """开着时：执行期间锁必须真被本轮持有；跑完要放开。

    判据不能只看退码——`hold` 整个失效也照样返回 0。所以在"执行"那一刻
    再去抢同一把锁：抢得到就说明本轮根本没持着它（= 假串行）。
    """
    token = tmp_path / live_lock.LOCK_NAME
    record = tmp_path / live_lock.HOLDER_NAME
    reached = {"n": 0}

    def fake_execute(args: Any, init: dict[str, Any]) -> int:
        with (
            pytest.raises(live_lock.LiveRunBusy),
            live_lock.hold(path=token, holders=record, timeout=0.0),
        ):
            pass  # pragma: no cover
        reached["n"] += 1
        return 0

    monkeypatch.setattr(cli_main, "_live_lock_enabled", lambda: True)
    monkeypatch.setattr(
        cli_main, "_live_lock_hold", lambda **kw: live_lock.hold(path=token, holders=record)
    )
    monkeypatch.setattr(cli_main, "_execute_run", fake_execute)

    assert cli_main._execute_run_serialized(_fake_args(), {}) == 0
    assert reached["n"] == 1, "执行函数压根没被调用"
    with live_lock.hold(path=token, holders=record, timeout=0.0):
        pass  # 释放干净才走到得了这里


def test_busy_refuses_with_exit_2_and_names_the_holder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """被占着：退码必须是 2（配置/参数错那一档），且要说清是谁占着。"""
    monkeypatch.setattr(cli_main, "_live_lock_enabled", lambda: True)
    monkeypatch.setattr(cli_main, "_live_lock_hold", _busy_holder)
    monkeypatch.setattr(cli_main, "_live_lock_path", lambda: tmp_path / "x.lock")

    assert cli_main._execute_run_serialized(_fake_args(), {}) == 2
    assert "4242" in capsys.readouterr().err


def test_json_mode_busy_still_emits_exactly_one_json_object(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--json` 的约定是 stdout 恰好一个 JSON 对象，**占用路径也不例外**。

    否则 Agent 的 `json.loads(stdout)` 会拿到一句中文提示，
    报出与"锁"完全无关的错误（README 退出码协议那条）。
    """
    monkeypatch.setattr(cli_main, "_live_lock_enabled", lambda: True)
    monkeypatch.setattr(cli_main, "_live_lock_hold", _busy_holder)

    assert cli_main._execute_run_serialized(_fake_args(json=True), {}) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "busy"
    assert "4242" in payload["error"]


def test_lock_engagement_is_observable_in_the_log(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """挂上锁必须在日志里留证据。

    这条不是装饰：2026-10-05 实测过一次启动器没把 `PM_LIVE_LOCK` 传进子进程，
    那一轮从头到尾裸奔，而日志里没有任何一行能看出"没上锁"。
    静默的自我保护等于没有保护——所以"挂上了"必须可观测，缺这行就是没挂。
    """
    token = tmp_path / live_lock.LOCK_NAME
    holders = tmp_path / live_lock.HOLDER_NAME
    monkeypatch.setattr(cli_main, "_live_lock_enabled", lambda: True)
    monkeypatch.setattr(
        cli_main, "_live_lock_hold", lambda **kw: live_lock.hold(path=token, holders=holders)
    )
    monkeypatch.setattr(cli_main, "_live_lock_path", lambda: token)
    monkeypatch.setattr(cli_main, "_execute_run", lambda args, init: 0)

    with caplog.at_level(logging.INFO, logger="pm.cli.main"):
        assert cli_main._execute_run_serialized(_fake_args(), {}) == 0
    assert any("串行锁已持有" in r.getMessage() for r in caplog.records), "挂了锁却没留证据"


def test_default_path_never_touches_the_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    """缺省关着时一次都不许碰锁：这条是"不改产品行为"的正面证据。

    只断言退码不够——`hold` 被偷偷调用也会成功。这里把 `hold` 换成"一调就炸"。
    """

    def explode(**kw: Any) -> Any:
        raise AssertionError("PM_LIVE_LOCK 没开却去抢锁了")

    monkeypatch.setattr(cli_main, "_live_lock_enabled", lambda: False)
    monkeypatch.setattr(cli_main, "_live_lock_hold", explode)
    monkeypatch.setattr(cli_main, "_execute_run", lambda args, init: 0)
    assert cli_main._execute_run_serialized(_fake_args(), {}) == 0
