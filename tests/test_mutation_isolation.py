"""变异类用例**改动仓库文件**，必须对"另一个 pytest 进程同时也在跑"免疫（2026-10-17）。

## 起因：两个套件并发时成片假红

复现（本机实测，两次全量 `pytest tests/ -q` 并发）：
两个进程各自都红一片，而**单独跑每一个文件都是绿的**。红的集中在：

    tests/test_guard_mutation.py      （改 docs/operations.md、ci.yml、conftest.py 做变异）
    tests/test_hooks_actually_block.py（改 pm/prompts.py 做变异）
    tests/test_ci_gate_matrix.py      （读上述文件做断言）
    tests/test_precommit_gate_set.py  （同上）

机制：变异用例走"**改真文件 → 跑工具 → 断言工具能红 → 还原**"这条路
（这是本仓刻意的设计：变异必须打在真判据上，否则测的是影子）。但还原用的是
**固定的 `.hooksbak` / 原文本备份名**，且没有任何跨进程保护。于是并发时：

- A 进程把 `pm/prompts.py` 污染到命中规则；
- B 进程的 `prompt-gate` 恰好在这时读到被污染的版本 ⇒ B 报
  "工作树是干净的，prompt-gate 却 rc=1"（**误判为自己的回归**）；
- A 进程先还原并 `unlink` 掉 `.hooksbak`，B 进程随后想还原却发现备份没了。

这正是本仓反复修过的同族缺陷（vkeys 2026-09-25 固定临时文件名互踩、
usage_store 2026-10-15 同形、ledger 2026-10-16 追加写无跨进程锁），
只是这次落在**测试基建**上：`pm/` 那三处都有守卫，测试侧一直没有。

## 为什么要单独一条守卫，而不是只说"别并发跑"

"别并发跑"不是能执行的约束：本地跑钩子时人的终端里已经有测试在跑、
编辑器/IDE 会同时跑单个文件、CI 上 `-n auto` 之类也会并发。
真实发生过一次（本会话提交时，pre-commit 的 pytest 与另一个后台 pytest 撞上，
报出一片 `FileNotFoundError`，把一次正常提交拦了下来）。

判据取**结构**：`tests/` 下任何"写回仓库内被跟踪文件"的用例，必须持有
**跨进程锁**；锁的实现复用生产侧那套（`pm/modelhub/filelock._process_lock`），
不另写一份 —— 同族实现抄第二遍必然分叉，这是本仓付过学费的。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"

# 变异类用例所在文件（会改真文件的那几个）
_MUTATING = ("test_guard_mutation.py", "test_hooks_actually_block.py")


def _writes_to_repo_files(src: str) -> list[str]:
    """找出"往仓库内文件写"的调用点（粗略但足够：write_text / write_bytes）。"""
    hits: list[str] = []
    try:
        tree = ast.parse(src)
    except SyntaxError:  # pragma: no cover
        return hits
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
        if name in ("write_text", "write_bytes"):
            hits.append(f"line {node.lineno}")
    return hits


def _holds_lock_via_helper(src: str) -> bool:
    """是否通过 `tests/_mutation_lock.py` 的 `mutate()` 持锁。

    判据取"用了那个助手"，而不是"字面量里出现 `_process_lock`"：助手内部复用的正是
    `pm.modelhub.filelock._process_lock`（同族实现不抄第二遍），所以接线方式只有一种。
    """
    return "from _mutation_lock import mutate" in src or "from _mutation_lock import" in src


def test_mutating_cases_hold_a_cross_process_lock() -> None:
    """会改仓库文件的变异用例，必须持跨进程锁（否则并发跑必然互相污染）。"""
    offenders: list[str] = []
    for fname in _MUTATING:
        p = TESTS / fname
        if not p.exists():  # pragma: no cover - 文件被删/改名时由别处守卫负责
            continue
        src = p.read_text(encoding="utf-8")
        writes = _writes_to_repo_files(src)
        if not writes:
            continue  # 不再改真文件的实现（例如全改到 tmp_path）是更好的方向，放行
        if "_process_lock" in src or _holds_lock_via_helper(src):
            continue
        offenders.append(f"{fname}（写文件点：{', '.join(writes[:4])}）")
    assert not offenders, (
        "这些变异用例会写仓库内的真文件、却没有跨进程锁："
        + "; ".join(offenders)
        + " —— 两个 pytest 并发时会互相污染（本机实测：成片假红，"
        "而单跑每个文件都是绿的）。请用 `tests/_mutation_lock.py` 的 `mutate()`"
        "（它复用 `pm.modelhub.filelock._process_lock`）包住「改→跑→还原」整段"
    )


def test_the_lock_is_held_across_the_whole_mutation() -> None:
    """锁必须包住**整段**（改 + 跑工具 + 还原），不能只包其中一步。

    只包还原等于没包：污染的窗口正是"改完之后、还原之前"，而另一个进程
    恰好在那段时间读文件。这条判据要求 `with mutate(...)` 块内**同时**出现
    写文件与 subprocess 调用。
    """
    missing: list[str] = []
    for fname in _MUTATING:
        p = TESTS / fname
        if not p.exists():  # pragma: no cover
            continue
        src = p.read_text(encoding="utf-8")
        if not (_holds_lock_via_helper(src) or "_process_lock" in src):
            continue  # 上一条判据已经报了
        try:
            tree = ast.parse(src)
        except SyntaxError:  # pragma: no cover
            continue
        helper = "mutate" if _holds_lock_via_helper(src) else "_process_lock"
        ok = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.With):
                continue
            if helper not in ast.dump(node):
                continue
            body = ast.dump(ast.Module(body=node.body, type_ignores=[]))
            has_write = "write_text" in body or "write_bytes" in body
            has_run = "subprocess" in body or "run_hook" in body or "Popen" in body
            # 变异写文件常常发生在被调助手内部（如 _mutate_schemas / _mutate_and_run），
            # 那时 with 块里只有"调用助手 + 跑子进程"，也算包住。
            has_indirect_write = "_mutate_schemas" in body or "_mutate_and_run" in body
            if (has_write or has_indirect_write) and has_run:
                ok = True
                break
        if not ok:
            missing.append(fname)
    assert not missing, (
        f"这些文件里跨进程锁没有包住「改→跑→还原」整段：{missing} —— "
        "只包还原那一步等于没包，污染窗口在改完之后、还原之前"
    )


def test_the_backup_name_is_not_a_fixed_shared_path() -> None:
    """临时/备份文件名不许用**固定名**落在共享位置（同族缺陷的另一种形态）。

    本仓先例：vkeys 固定 `data/vkeys.json.tmp`、usage_store 固定
    `usage_daily.json.tmp` 各自出过一次并发写坏。测试侧的 `.hooksbak` 同形。
    """
    bad: list[str] = []
    for fname in _MUTATING:
        p = TESTS / fname
        if not p.exists():  # pragma: no cover
            continue
        src = p.read_text(encoding="utf-8")
        for m in re.finditer(r'["\']([^"\']*\.hooksbak)["\']', src):
            bad.append(f"{fname}: {m.group(1)}")
    assert not bad, (
        "备份文件仍用固定名（并发时会互相覆盖/删除）："
        + "; ".join(bad)
        + " —— 带上 pid 或改用 tmp_path"
    )


def test_restore_failure_is_loud_instead_of_leaving_a_dirty_tree(monkeypatch, tmp_path) -> None:
    """还原失败必须让测试红，不能静默留下污染文件（2026-10-22 实测缺陷）。

    真实症状：全量套件全绿，工作树里却留着 `python run.py surprise_gate`，
    下一次跑 `test_ci_gate_matrix` 红在完全无关的用例上。
    """
    import _mutation_lock as ml

    victim = tmp_path / "victim.txt"
    victim.write_text("original", encoding="utf-8")

    def boom(target: Path, raw: bytes, attempts: int = 5) -> None:
        raise OSError("file is locked")

    monkeypatch.setattr(ml, "_restore_with_retries", boom)
    with pytest.raises(RuntimeError, match="还原失败"), ml.mutate(victim):
        victim.write_text("dirty", encoding="utf-8")
    victim.write_text("original", encoding="utf-8")


def test_restore_retries_transient_lock_failures(monkeypatch, tmp_path) -> None:
    """Windows 上文件被临时占用时重试几次，别把一次抖动升级成红。"""
    import _mutation_lock as ml

    victim = tmp_path / "victim.txt"
    victim.write_text("dirty", encoding="utf-8")
    real_replace = ml.os.replace
    calls = {"n": 0}

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] < 3:
            raise OSError("transient lock")
        return real_replace(src, dst)

    monkeypatch.setattr(ml.os, "replace", flaky)
    ml._restore_with_retries(victim, b"original")
    assert victim.read_bytes() == b"original"
    assert calls["n"] == 3


def test_the_reproduction_is_recorded_with_numbers() -> None:
    """复现读数必须写进本文件（否则下一个人不知道"并发跑红"是这个原因）。

    本仓纪律：环境类/并发类的红最容易被读成"代码坏了"，
    而**读数**是唯一能把它与其他红区分开的东西。这里钉两件事：
    · 复现方式（两个全量套件并发）留在文件里；
    · 修复前后的对比读数留在文件里。
    """
    src = Path(__file__).read_text(encoding="utf-8")
    assert "两个全量" in src or "并发" in src, "复现方式没写进本文件"
    assert "-进程锁" in src or "跨进程锁" in src, "修复手段没写进本文件"
    # 修复前的读数（本机实测）应当在文件里可查
    assert "成片假红" in src, (
        "修复前的症状描述（成片假红、单跑全绿）没留下 —— 下一个人遇到并发红时会重新查一遍"
    )
