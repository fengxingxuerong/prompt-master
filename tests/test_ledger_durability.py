"""台账（`logs/modelhub_ledger.jsonl`）是事实源，它自己不许静默丢账（2026-10-16）。

为什么这条比"日聚合坏了能重建"更靠前：`usage_store.rebuild_from_ledger()` 正是拿这份
JSONL 整份重算日聚合的——日聚合坏了还有这里可以重建，**这里丢了就真没了**。

修之前它有两个洞，都指向同一种失效形态——**账少了，但盘上看起来一切正常**：

1. **追加写只有进程内锁**（`--workers > 1` / CLI 与 server 同时跑）。
   实测（`scripts` 外的临时探针，两进程各 60 条短行、跑 6 轮）：
   无锁 **6/6 轮丢账**（盘上 113~117 条），而**坏行数是 0** —— 两笔写进了同一个偏移、
   后一笔完整盖掉前一笔（行长相同，连撕裂都看不出来），只剩"总数少几条"。
   带 `_process_lock` 后 **6/6 轮 120 条、0 坏行**。
2. **写失败只有一句 `print()`**：终端一关就查不到丢过账。现在丢一笔记一笔
   （`dropped_events()` 可问）+ 落 `notifications.jsonl`。

⚠️ 一条差点犯的错要留档：第一轮探针用 12KB 长行 × 40 条，结果是 80/80 全好，
据此我差点把锁撤掉、还写好了"未获证据支持"的注释——换成**短行**才复现。
"我试过一次没复现"从来不是"不会发生"的证据；参数一变结论就翻。

外加读路径一条：Windows 上别人的写入会瞬时拒绝读，不许把瞬时占用当损坏
（与 `vkeys._load_json_or` 同族，那边实测过"一次 EACCES 就判不可读"会把合法并发读者打死）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from pm.modelhub import ledger as LG
from pm.modelhub import notify

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    d = tmp_path / "ledger-data"
    d.mkdir()
    monkeypatch.setenv("PMH_LEDGER_PATH", str(d / "modelhub_ledger.jsonl"))
    monkeypatch.setenv("PMH_DATA_DIR", str(d))
    return d


def _append(n: int) -> None:
    for i in range(n):
        LG.append_call_event(
            request_id=f"r{i}",
            model="m1",
            endpoint="/v1/chat/completions",
            success=True,
            latency_ms=5,
            attempts=1,
            failovers=0,
            http_status=200,
            error_type=None,
            error_msg=None,
            agent="bot",
            role="assistant",
            content_chars=3,
        )


def _rows() -> list[dict]:
    return [
        json.loads(x)
        for x in LG.ledger_path().read_text(encoding="utf-8").splitlines()
        if x.strip()
    ]


# --------------------------------------------------------------------------
# 写失败必须留痕（不许只有一句 print）
# --------------------------------------------------------------------------


def test_write_failure_is_recorded_and_counted(
    monkeypatch: pytest.MonkeyPatch, isolated: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """写失败要落 notifications.jsonl 且能问出丢了几笔——只 print 等于静默丢账。"""
    # 告警落到仓库 logs/ 会污染运行目录，这里把它接到 tmp 下
    monkeypatch.setattr(notify, "notifications_path", lambda: isolated / "notifications.jsonl")

    target = LG.ledger_path()
    real_open = Path.open

    def flaky_open(self: Path, *a: object, **kw: object) -> object:
        # 只拦台账文件本身：全局拦 open 会把读告警文件的动作也打死，测的就不是台账了
        if self == target:
            raise PermissionError(13, "另一个程序正在使用此文件")
        return real_open(self, *a, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(LG, "_utcnow_iso", lambda: "2026-10-16T00:00:00.000Z")
    monkeypatch.setattr(Path, "open", flaky_open)

    before = LG.dropped_events()
    LG.append_event({"type": "call", "model": "m1"})  # 不许抛：台账是旁路

    assert LG.dropped_events() == before + 1, "丢一笔就要记一笔，否则没人数得清"
    out = capsys.readouterr().out
    assert "ledger write failed" in out
    note = isolated / "notifications.jsonl"
    assert note.exists(), "写失败必须落盘留痕——stdout 关掉就没了"
    assert "source" in note.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# 跨进程追加不许撕裂
# --------------------------------------------------------------------------


def test_two_processes_do_not_tear_the_ledger(isolated: Path) -> None:
    """两个进程各追加 60 条：盘上必须 120 条**完整行**，一条都不能少。

    这条有真证据（撤掉 `_process_lock` 后 6/6 轮只落 113~117 条，且坏行数 0 ——
    不是撕裂，是整笔被覆盖）：进程内的 `_LEDGER_LOCK` 跨不了进程。
    起真两个解释器这一点不能省：用线程或串行 subprocess 跑，锁被摘掉也照样绿。
    ⚠️ 行长别改成超长行：12KB/行的那轮探针复现不出来，会用假绿骗过这条守卫。
    """
    child = isolated / "ledger_child.py"
    child.write_text(
        "import sys\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "from pm.modelhub import ledger as LG\n"
        "for i in range(60):\n"
        "    LG.append_call_event(request_id=f'c{i}', model='m1', endpoint='/v1/chat/completions',"
        " success=True, latency_ms=1, attempts=1, failovers=0, http_status=200,"
        " error_type=None, error_msg=None, agent='bot', role='assistant', content_chars=1)\n"
        "print('ok')\n",
        encoding="utf-8",
    )
    env = {**os.environ, "PMH_LEDGER_PATH": str(LG.ledger_path()), "PMH_DATA_DIR": str(isolated)}
    procs = [
        subprocess.Popen(
            [sys.executable, str(child), str(ROOT)],
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
    for p in procs:  # 先全起完再收：串行收等于一个跑完才起下一个
        out, err = p.communicate(timeout=180)
        assert p.returncode == 0, f"子进程写台账崩了：rc={p.returncode} {out[-300:]} {err[-300:]}"

    text = LG.ledger_path().read_text(encoding="utf-8")
    lines = [x for x in text.splitlines() if x.strip()]
    bad = [x for x in lines if not x.startswith("{") or not x.endswith("}")]
    assert len(lines) == 120, f"两个进程各写 60 条，盘上只有 {len(lines)} 条（交错写毁了行）"
    assert not bad, f"有 {len(bad)} 行是撕裂的（会被读者按坏行静默丢掉）：{bad[:1]}"
    assert LG.ledger_stats()["calls"] == 120


# --------------------------------------------------------------------------
# 读路径：瞬时占用不许当成损坏
# --------------------------------------------------------------------------


def test_read_backs_off_on_transient_denial(
    monkeypatch: pytest.MonkeyPatch, isolated: Path
) -> None:
    """别人的写入正在进行时读会被瞬时拒绝：必须退避重试，而不是把合法读者打死。"""
    p = LG.ledger_path()
    p.write_text('{"type":"call","model":"m1"}\n', encoding="utf-8")
    real = Path.read_text
    state = {"n": 0}

    def flaky(self: Path, *a: object, **kw: object) -> str:
        if self == p:
            state["n"] += 1
            if state["n"] == 1:
                raise PermissionError(13, "正在写入")
        return real(self, *a, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", flaky)
    assert LG._read_text(p).strip().startswith("{"), "退避后必须读到内容"
    assert state["n"] == 2, f"第一次被拒要重试到成功（read 调用 {state['n']} 次）"


def test_stats_survives_transient_denial(monkeypatch: pytest.MonkeyPatch, isolated: Path) -> None:
    """`ledger_stats` / `query_ledger` 必须走同一条退避读，不然并发时 500。"""
    p = LG.ledger_path()
    p.write_text(
        '{"type":"call","model":"m1","success":true}\n{"type":"switch"}\n', encoding="utf-8"
    )
    real = Path.read_text
    state = {"n": 0}

    def flaky(self: Path, *a: object, **kw: object) -> str:
        if self == p:
            state["n"] += 1
            if state["n"] <= 2:
                raise PermissionError(13, "正在写入")
        return real(self, *a, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", flaky)
    assert LG.ledger_stats()["calls"] == 1
    assert len(LG.query_ledger()) == 2
    assert state["n"] >= 3, "两条读路径都得真的重试过"
