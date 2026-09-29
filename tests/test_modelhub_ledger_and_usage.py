"""ModelHub 的台账（JSONL）与日聚合（usage_daily.json）回归 —— 进程内、零出网。

补这块的理由和 `tests/test_modelhub_pool_and_keys.py` 同源：网关的验收用例都在
`tests_modelhub/` 那堆脚本里（pytest 收集 0 条），所以"记账对不对"这件事在流水线里
没人看过。而台账是 `/v1/ledger`、看板、故障复盘的唯一事实源，`usage_daily.json`
更是出过事的文件（当时仓库里还躺着一份 `data/usage_daily.json.corrupt-20260922-165846`，
2026-09-28 已随 data/ 运行态台账移出 git 跟踪一起清理）。

这里盯三条性质：① 写台账绝不影响主流程（但必须把失败说出来）；② 检索的过滤/排序/limit
语义；③ 聚合文件坏了要**留存现场**而不是静默清零，且并发记账不能丢笔。
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

import pytest
from pm.modelhub import ledger as LG
from pm.modelhub import usage_store as US

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """台账与日聚合各归一个文件。

    conftest 已经兜底封了这两个写路径，这里再显式设一遍是为了拿到句柄做断言
    —— 而不是因为"忘了设就没事"：恰恰是"以为设了"那次把线上台账截断了。
    """
    d = tmp_path / "mh-data"
    d.mkdir()
    monkeypatch.setenv("PMH_DATA_DIR", str(d))
    monkeypatch.setenv("PMH_LEDGER_PATH", str(d / "modelhub_ledger.jsonl"))
    return d


def test_writing_events_never_touches_the_repo_ledger(isolated: Path) -> None:
    """密封守卫：往台账写一条，仓库里那份运营台账必须一字不变。

    这条存在的唯一理由是 2026-09-25 真实发生过一次：用例把隔离变量记成 PMH_DATA_DIR，
    而 `ledger_path()` 读 PMH_LEDGER_PATH（默认 `<repo>/logs/modelhub_ledger.jsonl`），
    一个 write_text 截断了 411 条线上事件。conftest 那条夹具被谁删掉，这条就红。
    """
    real = ROOT / "logs" / "modelhub_ledger.jsonl"
    before = real.read_bytes() if real.exists() else None
    LG.append_call_event(
        request_id="seal-guard",
        model="m",
        endpoint="e",
        success=True,
        latency_ms=1,
        attempts=1,
        failovers=0,
        http_status=200,
        error_type=None,
        error_msg=None,
        agent="",
        role="",
        content_chars=None,
    )
    after = real.read_bytes() if real.exists() else None
    assert after == before, "测试把事件写进了运营台账 logs/modelhub_ledger.jsonl"
    assert LG.query_ledger(request_id="seal-guard"), "沙箱里应能读回自己写的那条"


# --------------------------------------------------------------------------
# 台账写入
# ---------------------------------------------------------------------------
def test_append_event_adds_timestamp_and_is_jsonl(isolated: Path) -> None:
    LG.append_event({"type": "call", "model": "m1"})
    LG.append_event({"type": "switch", "from_model": "m1", "ts": "2026-01-01T00:00:00Z"})
    lines = LG.ledger_path().read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["ts"], "缺 ts 的记录排不进时间序"
    assert json.loads(lines[1])["ts"] == "2026-01-01T00:00:00Z", "已有的 ts 不许被覆盖"


def test_call_event_carries_the_fields_the_dashboard_reads(isolated: Path) -> None:
    LG.append_call_event(
        request_id="r1",
        model="glm-5.2",
        endpoint="http://x/v1",
        success=False,
        latency_ms=120,
        attempts=2,
        failovers=1,
        http_status=None,
        error_type="GatewayError",
        error_msg="上游 502",
        agent="bot",
        role="assistant",
        content_chars=None,
    )
    row = LG.query_ledger(request_id="r1")[0]
    assert row["type"] == "call"
    for key in (
        "model",
        "endpoint",
        "success",
        "latency_ms",
        "attempts",
        "failovers",
        "error_type",
        "agent",
        "role",
    ):
        assert key in row, f"台账少字段 {key}，看板与复盘会读空"
    assert row["success"] is False and row["failovers"] == 1


def test_ledger_write_failure_does_not_break_the_caller(
    isolated: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """台账写不下去时只出声、不抛：它不能变成请求失败的原因。

    但也必须说出来 —— 静默丢账比丢账本身更贵（复盘时以为"这次调用没发生"）。
    """
    # 让台账路径的父目录变成一个普通文件 → mkdir/open 必失败
    blocked = isolated / "not-a-dir"
    blocked.write_text("我不是目录", encoding="utf-8")
    monkeypatch.setenv("PMH_LEDGER_PATH", str(blocked / "ledger.jsonl"))
    LG.append_call_event(
        request_id="r2",
        model="m",
        endpoint="e",
        success=True,
        latency_ms=1,
        attempts=1,
        failovers=0,
        http_status=200,
        error_type=None,
        error_msg=None,
        agent="",
        role="",
        content_chars=None,
    )  # 不该抛
    assert "ledger write failed" in capsys.readouterr().out


# --------------------------------------------------------------------------
# 检索与汇总
# ---------------------------------------------------------------------------
def _seed_ledger(lines: list[Any]) -> None:
    """接受 dict（序列化）与裸字符串（故意写坏的行）两种入参。"""
    path = LG.ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = [
        line if isinstance(line, str) else json.dumps(line, ensure_ascii=False) for line in lines
    ]
    path.write_text("\n".join(rendered) + "\n", encoding="utf-8")


def test_query_filters_sorts_and_limits(isolated: Path) -> None:
    _seed_ledger(
        [
            {"type": "call", "ts": "2026-09-20T00:00:00Z", "model": "a", "success": True},
            {"type": "call", "ts": "2026-09-22T00:00:00Z", "model": "b", "success": False},
            {"type": "switch", "ts": "2026-09-21T00:00:00Z", "from_model": "a", "to_model": "b"},
            "{ 这行坏了",
            "[1,2] 顶层不是对象的一行",
        ]
    )
    assert len(LG.query_ledger()) == 3, "坏行/非对象行要跳过而不是炸"
    assert [r["model"] for r in LG.query_ledger(model="a")] == ["a"]
    assert len(LG.query_ledger(type="switch")) == 1
    assert len(LG.query_ledger(success=True)) == 1
    between = LG.query_ledger(since="2026-09-21", until="2026-09-21T23:59:59Z")
    assert [r["type"] for r in between] == ["switch"], "ISO 串按字典序比较即等于时间序"
    newest = LG.query_ledger(limit=1)
    assert newest[0]["ts"] == "2026-09-22T00:00:00Z", "必须时间倒序取最近"
    assert LG.query_ledger(model="没有这个模型") == []


def test_query_on_missing_ledger_is_empty_list(isolated: Path) -> None:
    assert LG.query_ledger() == []


def test_ledger_stats_counts_and_by_model(isolated: Path) -> None:
    empty = LG.ledger_stats()
    assert empty["exists"] is False and empty["calls"] == 0
    _seed_ledger(
        [
            {"type": "call", "ts": "1", "model": "a", "success": True},
            {"type": "call", "ts": "2", "model": "a", "success": False},
            {"type": "call", "ts": "3", "model": "b", "success": True},
            {"type": "switch", "ts": "4"},
            {"type": "switch", "ts": "5"},
        ]
    )
    st = LG.ledger_stats()
    assert st["exists"] is True
    assert (st["calls"], st["success"], st["failed"], st["switches"]) == (3, 2, 1, 2)
    assert st["by_model"]["a"]["calls"] == 2 and st["by_model"]["a"]["success"] == 1


# --------------------------------------------------------------------------
# 日聚合 usage_daily.json
# ---------------------------------------------------------------------------
def _today() -> str:
    import time

    return time.strftime("%Y-%m-%d")


def test_record_call_accumulates_into_today(isolated: Path) -> None:
    US.record_call(
        model="m1",
        agent="bot",
        role="assistant",
        success=True,
        latency_ms=100,
        failovers=0,
        content_chars=42,
    )
    US.record_call(
        model="m1",
        agent="bot",
        role="assistant",
        success=False,
        latency_ms=50,
        failovers=1,
        content_chars=0,
    )
    day = US.load_daily()[_today()]
    assert day["calls"] == 2
    assert day["success"] == 1, "失败的那笔不许计入 success"
    assert day["latency_ms_sum"] == 150
    assert day["content_chars_sum"] == 42
    assert day["failovers_sum"] == 1
    assert day["by_model"]["m1"] == {"calls": 2, "success": 1}


def test_corrupt_usage_file_is_kept_and_rebuilt(isolated: Path) -> None:
    """坏了要留下现场（.corrupt-<ts>）再重建，且必须说一声。

    静默清零是这里最贵的失效形态：看板会显示"今天一次调用都没有"，
    而真实原因（文件坏了）谁也看不到。仓库里就躺着一份历史损坏文件。
    """
    p = US._path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{ 这不是合法 JSON", encoding="utf-8")
    assert US._load() == {}
    leftovers = [f.name for f in p.parent.glob("usage_daily.json.corrupt-*")]
    assert leftovers, "损坏文件必须改名留存，不能直接被覆盖"


def test_merge_treats_none_as_zero(isolated: Path) -> None:
    day: dict[str, Any] = {}
    US._merge(
        day,
        success=False,
        latency_ms=0,
        failovers=0,
        content_chars=0,
        model="?",  # type: ignore[arg-type]
    )
    US._merge(day, success=True, latency_ms=7, failovers=2, content_chars=9, model="")
    assert day["calls"] == 2 and day["success"] == 1
    assert day["latency_ms_sum"] == 7 and day["failovers_sum"] == 2
    assert "" in day["by_model"]


def test_concurrent_records_lose_nothing(isolated: Path) -> None:
    """8 线程各记 5 笔 = 40 笔，一笔都不能少。

    这是"读-改-写 + 原子替换"唯一的验法：锁失守时症状是数字悄悄变小，
    而不是报错 —— 看板上根本看不出来。
    """
    rounds = 5

    def worker() -> None:
        for _ in range(rounds):
            US.record_call(
                model="m",
                agent="bot",
                role="",
                success=True,
                latency_ms=1,
                failovers=0,
                content_chars=1,
            )

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    day = US.load_daily()[_today()]
    assert day["calls"] == 8 * rounds, f"并发记账丢笔：{day['calls']} != {8 * rounds}"
    assert day["by_model"]["m"]["calls"] == 8 * rounds


def test_record_call_retries_windows_replace_lock(
    isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AV 短锁 os.replace（WinError 5）时有界重试兜回，一笔不丢。

    2026-09-29 实测偶发：8×5 并发记账丢 4 笔，线程栈是
    os.replace → PermissionError（杀毒/索引短暂打开刚写完的文件）。
    确定性复现：前两次 replace 抛 PermissionError，第三次必须成功落盘。
    """
    real_replace = os.replace
    calls = {"n": 0}

    def flaky_replace(src: Any, dst: Any) -> None:
        calls["n"] += 1
        if calls["n"] <= 2:
            raise PermissionError(5, "拒绝访问。")
        real_replace(src, dst)

    monkeypatch.setattr(US.os, "replace", flaky_replace)
    US.record_call(
        model="m",
        agent="bot",
        role="",
        success=True,
        latency_ms=1,
        failovers=0,
        content_chars=1,
    )
    # 不 monkeypatch.undo()：它会连 isolated fixture 的 PMH_DATA_DIR 一起撤掉。
    # flaky_replace 在第 3 次后直接透传 real_replace，无残留副作用。
    assert calls["n"] == 3, f"应重试至第 3 次成功，实际调了 {calls['n']} 次"
    assert US.load_daily()[_today()]["calls"] == 1
