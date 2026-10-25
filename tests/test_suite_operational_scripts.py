"""`releases/suite/` 三个运营脚本的口径：轮转要轮全部且失败不静默、探针要看存储。

都是 2026-10-31 只读审计实测出来的"运维盲区"——不影响功能，但会让
"夜间无人值守"这个前提失效：

1. `rotate_ledger.py`：路径写死成 `D:\\projects\\prompt-master\\...`（换机器就指向空，
   只印一句 "no ledger file" 然后**当成功**）；只轮转一份台账（三套服务的
   `releases/*/data/*.jsonl` 不在范围）；Windows 上文件被占用时 `replace` 抛
   PermissionError 被 watchdog 的 `print` 吞掉 ⇒ 台账**从未轮转过**且日志看不出异常。
2. `watchdog_suite.py` / `monitor_suite.py`：只看 HTTP 200。把 `orders.db` 写成垃圾字节后
   服务仍回 200 + `status:"ok"`（只把 `storage` 记成 error），探针照样报绿。
   而带 token 的部署里探针自己不带令牌 ⇒ 数据接口恒 401，超时任务告警静默不触发。
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SUITE = ROOT / "releases" / "suite"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"suite_{name}", SUITE / f"{name}.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"suite_{name}"] = mod
    spec.loader.exec_module(mod)
    return mod


# --------------------------------------------------------------- rotate_ledger
def test_rotate_targets_every_ledger_not_a_hardcoded_one(tmp_path: Path) -> None:
    """范围 = `logs/*.jsonl` + `releases/*/data/*.jsonl`，且在调用时按 root 现算。"""
    rl = _load("rotate_ledger")
    (tmp_path / "logs").mkdir()
    (tmp_path / "releases" / "lobster" / "data").mkdir(parents=True)
    (tmp_path / "logs" / "big.jsonl").write_text("x" * 1_500_000, encoding="utf-8")
    (tmp_path / "logs" / "small.jsonl").write_text("x" * 10, encoding="utf-8")
    (tmp_path / "releases" / "lobster" / "data" / "ledger.jsonl").write_text(
        "x" * 1_500_000, encoding="utf-8"
    )

    report = rl.rotate(root=tmp_path)
    assert report["scanned"] == 3, f"三份台账应当都在范围内：{report}"
    assert report["rotated"] == 2, "超 1MB 的两份应当被轮转"
    assert report["skipped"] == 1
    assert (tmp_path / "logs" / "big.jsonl.1").exists()
    assert (tmp_path / "releases" / "lobster" / "data" / "ledger.jsonl.1").exists(), (
        "三套服务的运营台账不在范围里 —— 而它们才是天天在长的那些"
    )
    assert not (tmp_path / "logs" / "small.jsonl.1").exists()


def test_rotate_reports_a_locked_file_instead_of_swallowing_it(tmp_path: Path) -> None:
    """文件被占用时：`errors` 里必须有那条，`rotated` 不许谎报。

    Windows 上服务正开着台账 ⇒ `os.replace` 抛 PermissionError。旧版只有一句 print，
    而 watchdog 每 30 秒调一次、每次都这样过去 —— 台账实际上从未轮转过。
    """
    rl = _load("rotate_ledger")
    (tmp_path / "logs").mkdir()
    f = tmp_path / "logs" / "busy.jsonl"
    f.write_text("x" * 1_500_000, encoding="utf-8")

    handle = open(f, "rb")  # noqa: SIM115 - 故意的：保持句柄才能复现占用
    try:
        report = rl.rotate(root=tmp_path)
    finally:
        handle.close()

    if report["rotated"]:  # 非 Windows / 平台允许改名：这条判据不适用
        pytest.skip("该平台允许改名被打开的文件，占用场景无法复现")
    assert report["errors"], "占用失败被静默吞掉了"
    assert "PermissionError" in report["errors"][0] or "另一个程序" in report["errors"][0]


def test_rotate_with_no_ledgers_says_so(tmp_path: Path) -> None:
    rl = _load("rotate_ledger")
    report = rl.rotate(root=tmp_path)
    assert report["scanned"] == 0 and report["rotated"] == 0 and report["errors"] == []


# ------------------------------------------------------------------- 健康判据
def _cases() -> list[tuple[int | None, dict, bool]]:
    """(HTTP 码, 请求体, 应当判为"有问题") 的判定表。"""
    return [
        (200, {"status": "ok", "storage": "sqlite(wal)"}, False),
        (200, {"status": "ok"}, False),
        (200, {"status": "ok", "storage": "error: DatabaseError"}, True),
        (200, {"status": "degraded"}, True),
        (401, {}, True),
        (None, {"err": "connection refused"}, True),
    ]


def test_monitor_treats_a_broken_store_as_bad_health() -> None:
    ms = _load("monitor_suite")
    for status, body, should_flag in _cases():
        issue = ms._health_issue(status, body)
        assert (issue is not None) is should_flag, (status, body, issue)


def test_watchdog_and_monitor_agree_on_health() -> None:
    """两条路径必须给同一个答案（本仓的老教训：同一个概念两套实现必然漂）。"""
    ms = _load("monitor_suite")
    wd = _load("watchdog_suite")

    class _Resp:
        def __init__(self, status: int, payload: bytes):
            self.status = status
            self._payload = payload

        def read(self) -> bytes:
            return self._payload

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    import json as _json

    for status, body, should_flag in _cases():
        if status is None:
            continue  # 网络异常那条由各自的 except 分支覆盖，形状不同

        def fake_urlopen(_req, timeout=0, _s=status, _b=body):
            return _Resp(_s, _json.dumps(_b).encode())

        original = wd.urllib.request.urlopen
        wd.urllib.request.urlopen = fake_urlopen  # type: ignore[assignment]
        try:
            issue = wd.health_issue("http://x/api/health", "lobster")
        finally:
            wd.urllib.request.urlopen = original  # type: ignore[assignment]
        assert (issue is not None) is should_flag, (status, body, issue)
        assert (issue is not None) is (ms._health_issue(status, body) is not None)


def test_probes_carry_the_token_when_one_is_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """带 token 的部署里探针必须自己带令牌，否则数据接口恒 401、告警静默不触发。"""
    ms = _load("monitor_suite")
    wd = _load("watchdog_suite")
    monkeypatch.setenv("TASKBOARD_TOKEN", "tok-123")
    try:
        assert ms.headers_for("taskboard") == {"X-API-Key": "tok-123"}
        assert wd._headers_for("taskboard") == {"X-API-Key": "tok-123"}
    finally:
        monkeypatch.delenv("TASKBOARD_TOKEN", raising=False)
    monkeypatch.delenv("TASKBOARD_TOKEN", raising=False)
    assert ms.headers_for("taskboard") == {}
    assert wd._headers_for("taskboard") == {}


def test_monitor_root_is_derived_not_hardcoded() -> None:
    """脚本位置变了也得能跑：根路径不许写死成某台机器的绝对路径。"""
    text = (SUITE / "monitor_suite.py").read_text(encoding="utf-8")
    assert "Path(__file__)" in text, "monitor_suite 的 ROOT 还是硬编码的"
    rl = (SUITE / "rotate_ledger.py").read_text(encoding="utf-8")
    assert "Path(__file__)" in rl and "D:\\projects" not in rl, "rotate_ledger 还写死本机路径"
    assert os.sep not in rl.split("LEDGER_GLOBS")[0].splitlines()[-1]
