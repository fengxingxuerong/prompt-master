"""日聚合坏了要能**从台账重建**，而不是归零（2026-10-16）。

背景（真事故）：`data/usage_daily.json` 在 2026-10-15 之前已累计 **554 次**损坏告警
（`logs/notifications.jsonl` 计数）。当时的处置是"改名留存 + 从空重建"——于是当天及以前的
日聚合**全部归零**：`/v1/usage` 与看板会显示"今天一次调用都没有"，而真实原因（文件坏了）
只有那行 ALERT 知道。修完跨进程写坏的根因之后，剩下这一半才是"数据到底丢没丢"。

JSONL 台账（`logs/modelhub_ledger.jsonl`）里每一笔调用都有 `type=call` 的原文，
所以损坏不该等于丢数据。本文件钉的就是这条恢复链，重点不是"能算出个数"，而是
**重建出来的数必须和增量记账的数一模一样** —— 口径只要差一次时区换算，同一批调用
就会被拆到两个日期键上，两边再也加不到一起。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pm.modelhub import usage_store as US


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    d = tmp_path / "us-data"
    d.mkdir()
    monkeypatch.setenv("PMH_DATA_DIR", str(d))
    monkeypatch.setenv("PMH_LEDGER_PATH", str(d / "modelhub_ledger.jsonl"))
    return d


def _call_event(*, ts: str, model: str = "m1", success: bool = True, **kw: Any) -> dict[str, Any]:
    ev = {
        "type": "call",
        "ts": ts,
        "model": model,
        "success": success,
        "latency_ms": 10,
        "failovers": 0,
        "content_chars": 5,
    }
    ev.update(kw)
    return ev


def _write_ledger(events: list[dict[str, Any]], raw_extra: str = "") -> None:
    from pm.modelhub import ledger as LG

    p = LG.ledger_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events) + raw_extra,
        encoding="utf-8",
    )


def _record(model: str = "m1", success: bool = True) -> None:
    US.record_call(
        model=model,
        agent="bot",
        role="assistant",
        success=success,
        latency_ms=10,
        failovers=0,
        content_chars=5,
    )


# --------------------------------------------------------------------------
# 口径：重建必须与增量记账给出同一个数
# --------------------------------------------------------------------------


def test_rebuild_matches_incremental_accounting(isolated: Path) -> None:
    """同一批调用，增量记账与台账重建必须逐字段相等——这是重建能不能用的唯一判据。

    时刻刻意选在**本地当天凌晨**（UTC 上还是前一天）：只要 `_day_key_from_ts` 少做一次
    时区换算，重建结果就落到昨天，本条当场红。用"今天中午"这种时刻测不出这个洞。
    """
    local_now = datetime.now().astimezone()
    midnight = local_now.replace(hour=0, minute=30, second=0, microsecond=0)
    ts = midnight.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    _record(success=True)
    _record(success=False)
    incremental = US.load_daily()

    # 清掉日聚合，只留台账：模拟"文件没了，事实还在"
    (isolated / "usage_daily.json").unlink()
    _write_ledger([_call_event(ts=ts), _call_event(ts=ts, success=False)])

    rebuilt = US.rebuild_from_ledger()
    assert rebuilt == incremental, (
        f"重建 {rebuilt} 与增量记账 {incremental} 不一致——日期口径或字段口径错了，"
        "两边的数就再也加不到一起"
    )


def test_rebuild_counts_by_model_and_failures() -> None:
    ts = datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S.000Z")
    _write_ledger(
        [
            _call_event(ts=ts, model="a", success=True),
            _call_event(ts=ts, model="a", success=False, failovers=2, latency_ms=30),
            _call_event(ts=ts, model="b", success=True),
            {"type": "switch", "ts": ts, "from": "a", "to": "b"},  # 切换事件不是调用
        ]
    )
    out = US.rebuild_from_ledger()
    day = out[next(iter(out))]
    assert day["calls"] == 3, "switch 事件不计入调用数"
    assert day["success"] == 2
    assert day["failovers_sum"] == 2
    assert day["latency_ms_sum"] == 50
    assert day["by_model"]["a"] == {"calls": 2, "success": 1}
    assert day["by_model"]["b"] == {"calls": 1, "success": 1}


# --------------------------------------------------------------------------
# 恢复：损坏不再等于归零
# --------------------------------------------------------------------------


def test_corrupt_file_is_rebuilt_from_ledger_not_zeroed(isolated: Path) -> None:
    """核心：文件坏了以后读到的必须是台账重算出来的数，不是 0 条。"""
    ts = datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S.000Z")
    _write_ledger([_call_event(ts=ts), _call_event(ts=ts), _call_event(ts=ts, success=False)])
    p = isolated / "usage_daily.json"
    p.write_text("{ 这不是合法 JSON", encoding="utf-8")

    data = US._load()
    total = sum(v.get("calls", 0) for v in data.values())
    assert total == 3, f"损坏后读到 {total} 笔（应为 3）——重建没接上，数据还是丢了"

    # 重建结果当场落盘：下次读就是好文件，不用每读一次都重扫台账
    again = json.loads(p.read_text(encoding="utf-8"))
    assert sum(v.get("calls", 0) for v in again.values()) == 3
    assert list(isolated.glob("usage_daily.json.corrupt-*")), "损坏现场必须留存"


def test_no_ledger_means_empty_and_says_so(
    isolated: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """台账里重建不出任何一天时退回空，并且**要说清是归零**而不是含糊带过。"""
    p = isolated / "usage_daily.json"
    p.write_text("{ broken", encoding="utf-8")
    assert US._load() == {}
    out = capsys.readouterr().out
    assert "重建不出" in out, f"退回空时必须明说数据归零，实际输出：{out[-200:]}"


def test_rebuild_survives_bad_ledger_lines(isolated: Path) -> None:
    """一条坏行不许毁掉整份重建（与 query_ledger 同口径：跳过，不炸）。"""
    ts = datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S.000Z")
    _write_ledger(
        [_call_event(ts=ts)],
        raw_extra="{ 这不是 JSON\n" + "\n" + json.dumps(_call_event(ts=ts)) + "\n",
    )
    assert sum(v.get("calls", 0) for v in US.rebuild_from_ledger().values()) == 2


def test_missing_ledger_file_is_empty_not_an_error(isolated: Path) -> None:
    """台账文件压根不存在 ⇒ 认不出，返回空；不许抛。"""
    assert US.rebuild_from_ledger() == {}
