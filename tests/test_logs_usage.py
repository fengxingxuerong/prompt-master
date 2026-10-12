"""`scripts/logs_usage.py` 的守卫：分类口径、可回收判定、零写入、JSON 单对象。

为什么值得一条守卫（2026-10-13）：这个工具的处置建议直接对着**证据类**文件
（`run_*.json` 被 `docs/evaluation.md` 按 id 引用）与**死重**（归档里内嵌的 venv）。
一旦分类口径漂了（比如把 `venv` 挪出 RECLAIMABLE_DIRS，或把 `run_` 前缀认错），
报表会开始给错建议——而它自己不删东西，没人会当场发现口径错了。所以口径本身要有闸。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

TOOL = Path(__file__).resolve().parents[1] / "scripts" / "logs_usage.py"


def _make_tree(root: Path) -> None:
    (root / "run_abc123.json").write_text("{}", encoding="utf-8")
    (root / "report_abc123.md").write_text("# r", encoding="utf-8")
    (root / "_scratch.log").write_text("x", encoding="utf-8")
    (root / "eval_cache.json").write_text("{}", encoding="utf-8")
    (root / "probe.py").write_text("print(1)", encoding="utf-8")
    (root / "archive" / "delivery" / "venv" / "Lib" / "site-packages").mkdir(parents=True)
    (root / "archive" / "delivery" / "venv" / "Lib" / "site-packages" / "big.pyd").write_bytes(
        b"0" * 4096
    )
    (root / "archive" / "delivery" / "venv" / "Scripts").mkdir(parents=True)
    (root / "archive" / "delivery" / "venv" / "Scripts" / "python.exe").write_bytes(b"0" * 1024)


def _run_tool(root: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(TOOL), "--root", str(root), *extra],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def _snapshot(root: Path) -> dict[str, int]:
    return {str(p.relative_to(root)): p.stat().st_size for p in root.rglob("*") if p.is_file()}


def test_classification_keeps_evidence_out_of_reclaimable(tmp_path: Path) -> None:
    """run_/report_ 是证据、venv/cache 是可回收——两类必须分得开。"""
    _make_tree(tmp_path)
    proc = _run_tool(tmp_path, "--json")
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    cats = data["categories"]
    assert cats["evidence_runs"]["files"] == 1
    assert cats["evidence_reports"]["files"] == 1
    assert cats["reclaimable_dirs"]["files"] == 2, "venv 里的两个文件都应计入"
    assert cats["reclaimable_cache"]["files"] == 1
    # 可回收建议按**目录**整报，且不包含证据类
    dirs = [d["path"] for d in data["reclaimable_dirs"]]
    assert len(dirs) == 1 and dirs[0].replace("\\", "/").endswith("archive/delivery/venv"), dirs
    assert all("run_" not in p and "report_" not in p for p in dirs)
    assert data["reclaimable_bytes"] == 4096 + 1024 + len(b"{}")


def test_report_is_read_only(tmp_path: Path) -> None:
    """工具对日志树零写入：跑前跑后逐文件快照必须完全一致。"""
    _make_tree(tmp_path)
    before = _snapshot(tmp_path)
    proc = _run_tool(tmp_path)
    assert proc.returncode == 0
    assert _snapshot(tmp_path) == before, "审计工具动了文件——它承诺过从不删除/改写"


def test_json_channel_is_a_single_object(tmp_path: Path) -> None:
    """--json 时 stdout 恰好一个 JSON 对象（与全仓 CLI 契约一致。"""
    _make_tree(tmp_path)
    proc = _run_tool(tmp_path, "--json")
    json.loads(proc.stdout)  # 多一个字符都会在这里炸
    assert proc.stdout.strip().startswith("{") and proc.stdout.strip().endswith("}")


def test_missing_root_exits_two(tmp_path: Path) -> None:
    """目录不存在 = 参数错误，退出码 2（与 run.py 的四态契约同口径）。"""
    proc = _run_tool(tmp_path / "nope")
    assert proc.returncode == 2
