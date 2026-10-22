"""只读项目扫描的回归：排除密钥、限制体积、拦住越界路径。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from extras import project_context as pc

SECRET = "sk-abcdefghijklmnopqrstuvwxyz"


def make_project(tmp_path: Path) -> Path:
    root = tmp_path / "demo"
    (root / "src").mkdir(parents=True)
    (root / "docs").mkdir()
    (root / "node_modules").mkdir()
    (root / ".env").write_text(f"PM_API_KEY={SECRET}\n", encoding="utf-8")
    (root / ".env.local").write_text("TOKEN=another-secret-value\n", encoding="utf-8")
    (root / "node_modules" / "dep.js").write_text("依赖目录不该被读", encoding="utf-8")
    (root / "README.md").write_text("# Demo\n\n已完成登录，下一步做支付。\n", encoding="utf-8")
    (root / "package.json").write_text('{"name": "demo"}', encoding="utf-8")
    (root / "docs" / "plan.md").write_text("# Plan\n- TODO: 写测试\n", encoding="utf-8")
    (root / "src" / "app.py").write_text(
        f"# TODO: add auth\nAPI_KEY = '{SECRET}'\ndef run():\n    return 1\n",
        encoding="utf-8",
    )
    return root


def test_scan_reads_key_files_but_never_secret_files(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    brief = pc.scan_project(root)

    assert brief.file_count == 4
    assert brief.skipped_secrets == 2
    paths = [item.path for item in brief.key_files]
    assert "README.md" in paths and "src/app.py" in paths
    assert ".env" not in paths and ".env.local" not in paths
    context = brief.context_text()
    assert SECRET not in context
    assert "another-secret-value" not in context
    assert "node_modules" not in context
    assert "依赖目录不该被读" not in context


def test_redact_secrets_covers_common_forms_and_plural_key_names() -> None:
    raw = "\n".join(
        [
            f"PM_API_KEY={SECRET}",
            "PM_API_KEYS=alpha-key,beta-key",
            'password = "hunter2"',
            "Authorization: Bearer abcdefghijklmnop",
            "postgres://user:p%40ss@db.example.com/app",
            "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----",
        ]
    )
    out = pc.redact_secrets(raw)
    for secret in (
        SECRET,
        "alpha-key",
        "beta-key",
        "hunter2",
        "abcdefghijklmnop",
        "p%40ss",
        "MIIEow",
    ):
        assert secret not in out
    assert "PM_API_KEY" in out and "PM_API_KEYS" in out
    assert "[已隐去私钥]" in out


def test_read_file_rejects_traversal_and_secret_paths(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")

    with pytest.raises(ValueError):
        pc.read_file(root, "../outside.txt")
    with pytest.raises(ValueError):
        pc.read_file(root, ".env")
    text = pc.read_file(root, "src/app.py")
    assert "TODO: add auth" in text
    assert SECRET not in text


def test_symlink_outside_root_is_not_followed(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text(f"TOKEN={SECRET}", encoding="utf-8")
    link = root / "src" / "outside-link.txt"
    try:
        os.symlink(outside, link)
    except (OSError, NotImplementedError):
        pytest.skip("这台机器不允许建符号链接")
    brief = pc.scan_project(root)
    assert "src/outside-link.txt" not in [item.path for item in brief.key_files]
    with pytest.raises(ValueError):
        pc.read_file(root, "src/outside-link.txt")


def test_list_and_search_tools_are_read_only(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    assert ".env" not in pc.list_files(root)
    hits = pc.search_text(root, "TODO")
    assert {hit["path"] for hit in hits} == {"docs/plan.md", "src/app.py"}
    assert all(isinstance(hit["line"], str) for hit in hits)
    hits_with_secret = pc.search_text(root, SECRET)
    assert hits_with_secret == []


def test_git_snapshot_filters_secret_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    root = make_project(tmp_path)

    def fake_git(_root: Path, args: list[str]) -> str:
        if args[:2] == ["rev-parse", "--is-inside-work-tree"]:
            return "true"
        if args[0] == "branch":
            return "feature/demo"
        if args[0] == "log":
            return f"abc1234 2026-10-20 完成登录\n{SECRET}"
        if args[0] == "status":
            return " M src/app.py\n?? .env\n?? docs/new.md"
        if args[0] == "diff":
            return " src/app.py | 2 +-"
        return ""

    monkeypatch.setattr(pc.shutil, "which", lambda _name: "git")
    monkeypatch.setattr(pc, "_git_run", fake_git)
    snap = pc.git_snapshot(root)
    assert snap is not None
    assert snap["branch"] == "feature/demo"
    assert any("src/app.py" in line for line in snap["status"])
    assert not any(".env" in line for line in snap["status"])
    assert SECRET not in "\n".join(snap["recent_commits"] + snap["status"])


def test_git_snapshot_returns_none_outside_a_repo(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = make_project(tmp_path)
    monkeypatch.setattr(pc.shutil, "which", lambda _name: "git")
    monkeypatch.setattr(pc, "_git_run", lambda _root, _args: "")
    assert pc.git_snapshot(root) is None


def test_scan_bounds_the_context(tmp_path: Path) -> None:
    root = tmp_path / "big"
    root.mkdir()
    for index in range(20):
        (root / f"mod_{index}.py").write_text("x = 1\n" + "y" * 800, encoding="utf-8")
    brief = pc.scan_project(root, max_context_chars=400)
    context = brief.context_text(max_chars=400)
    assert len(context) <= 430
    assert "截断" in context
    assert brief.truncated is True
    assert sum(item.chars for item in brief.key_files) <= 400


def test_context_keeps_its_closing_tags_when_content_fits(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    brief = pc.scan_project(root, max_context_chars=5_000)
    context = brief.context_text(max_chars=5_000)
    assert context.rstrip().endswith("</PROJECT_EVIDENCE>")


def test_scan_rejects_missing_directory(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        pc.scan_project(tmp_path / "nope")
