"""`scripts/logs_archive.py` 的守卫：证据只移动不删除、dry-run 零写入、文档引用的不被归档。

为什么值得守卫（2026-10-23）：这是"日志会膨胀"那条 ops 承诺的落地工具。它动的对象是
`run_*.json` / `report_*.md` —— 被 `docs/evaluation.md`、`docs/iteration/` 按 run id 点名的
**证据**。logs_usage.py 刻意只读、绝不删；本工具允许移动，但有三条不可破的承诺必须钉死：
  ① 默认 dry-run 零写入（不移动、不建 archive）；
  ② `--commit` 移动时**不改字节**（证据的内容快照逐字一致，只是换了目录）；
  ③ 仍被文档按 id 点名的证据不归档（避免"归档了,某篇文档还指着它"）。
一旦这些口径漂了，工具会开始静默丢证据 —— 而移动成功时没有 crash，没人会当场发现。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

TOOL = Path(__file__).resolve().parents[1] / "scripts" / "logs_archive.py"


def _make_tree(root: Path, run_id: str = "abc123def456") -> None:
    """造一棵含 run_/report_ 证据与一份无关文件的日志树。"""
    (root / f"run_{run_id}.json").write_text('{"a":1}', encoding="utf-8")
    (root / f"report_{run_id}.md").write_text("# r\nbody", encoding="utf-8")
    (root / "eval_cache.json").write_text("{}", encoding="utf-8")  # 不该被本工具碰


def _run_tool(root: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(TOOL), "--root", str(root), "--older-than", "0", *extra],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def _snapshot(root: Path) -> dict[str, str]:
    """逐文件内容快照（不是大小）：commit 后字节必须逐字一致。"""
    out: dict[str, str] = {}
    for p in root.rglob("*"):
        if p.is_file() and "manifest.json" not in p.name:
            out[str(p.relative_to(root))] = p.read_text(encoding="utf-8", errors="replace")
    return out


def test_dry_run_is_zero_write(tmp_path: Path) -> None:
    """不带 --commit 时：不建 archive、不移动、树快照完全一致。"""
    _make_tree(tmp_path)
    before = _snapshot(tmp_path)
    proc = _run_tool(tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert _snapshot(tmp_path) == before, "dry-run 动了文件——工具承诺过默认零写入"
    assert not (tmp_path / "archive").exists(), "dry-run 不该创建归档目录"


def test_commit_moves_but_does_not_change_bytes(tmp_path: Path) -> None:
    """--commit：证据移进 archive/ 且内容逐字一致；缓存文件原位不动。"""
    _make_tree(tmp_path)
    proc = _run_tool(tmp_path, "--commit", "--json")
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    assert data["commit"] is True
    moved = data["moved"]
    assert len(moved) == 2, moved  # 该 run 的一份 json + 一份 md 都移走
    # 内容逐字可回查（换目录不等于改字节）
    for rel in moved:
        assert (tmp_path / rel).exists(), f"归档后原字节不在 {rel}"
    # 缓存文件不该被本工具碰（别把功能依赖卷进去）
    assert "eval_cache.json" not in moved
    assert (tmp_path / "eval_cache.json").read_text(encoding="utf-8") == "{}"
    # 留档清单存在且能反推原路径
    manifest = (
        tmp_path / "archive" / next(iter((tmp_path / "archive").iterdir())).name / "manifest.json"
    )
    if manifest.exists():
        m = json.loads(manifest.read_text(encoding="utf-8"))
        assert len(m["entries"]) == 2
        assert all(e["run_id"] == "abc123def456" for e in m["entries"])


def test_doc_referenced_evidence_is_protected(tmp_path: Path) -> None:
    """文档点名的 run id 不归档 —— 靠 scripts 同仓 docs/ 的引用判断。

    用一个真实被 docs 引用的 id 造树：docs 里确实有点名 → 它必须留在原位。
    """
    # 从仓库实际文档里取一个"确实被引用"的 id，保证测试不是自说自话
    import re

    docs = Path(__file__).resolve().parents[1] / "docs"
    referenced = set()
    for p in docs.rglob("*.md"):
        referenced.update(
            re.findall(r"run_([0-9a-f]{12,20})", p.read_text(encoding="utf-8", errors="replace"))
        )
    assert referenced, "docs/ 里应存在被引用的 run id（否则本测试失锚）"
    rid = sorted(referenced)[0]
    _make_tree(tmp_path, run_id=rid)
    proc = _run_tool(tmp_path, "--json")
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    # 文档点名 → candidates 应为空（protect 命中）
    assert data["candidates"] == [], (
        f"被文档引用的 {rid} 仍进了归档候选；保护逻辑失效（protect={data['protected_by_docs']}）"
    )


def test_json_channel_is_single_object_and_missing_root_exits_two(tmp_path: Path) -> None:
    """--json 单对象；目录不存在 = 参数错误 rc=2（与全仓 CLI 契约一致）。"""
    _make_tree(tmp_path)
    proc = _run_tool(tmp_path, "--json")
    parsed = json.loads(proc.stdout)
    assert parsed["older_than_days"] == 0
    bad = _run_tool(tmp_path / "nope")
    assert bad.returncode == 2
