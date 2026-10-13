"""入口脚本在 cp936（GBK）控制台上的编码守卫（2026-10-13 立）。

真实事故：本机 `python scripts/logs_usage.py --root logs` 返回 rc=1，
`UnicodeEncodeError: 'gbk' codec can't encode character '\\u26a0' in position 2`
—— 那份"告诉你 logs/ 里哪些能删"的报表在**最后一行**崩掉。而 CI 的两个 job 都绿：
GitHub 的 windows runner 缺省码页就是 UTF-8，没有任何一台门禁机替中文 Windows
用户崩过一次。也就是说这条缺陷**只有真实用户会撞到**，必须有一判据替他们撞。

四条判据各管一层，缺一层就漏一类：
1. 静态：入口脚本的字面量里有 GBK 放不下的字符 ⇒ 必须走共享 helper（漏了就是今天这个）；
2. 静态：不许再手写 `sys.stdout.reconfigure(...)`（两份引导迟早漂成两种行为）；
3. 行为：在 `PYTHONIOENCODING=gbk` 的子里进程里真打印一次 —— "调了 helper"是意图，
   "崩不了"才是结果。判据是流的编码而不是 OS，所以 linux CI 同样能复现；
4. 反向对照：摘掉引导必须真的红，否则第 3 条的绿只证明宿主本来就是 UTF-8。
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# 交付面（用户/Agent 会直接 `python <file>` 的东西）：仓库根的入口薄壳 + 这三个目录。
# ⚠️ 故意不含 `releases/`：那里是**历史发布快照**（v1.0.0 / v1.1.0 / deploy-drill / suite 三服务），
#    快照各带自己的 bootstrap；给它接本产品的 helper 等于篡改"当时发出去的那一份"。
#    测试目录同理不在列：那里的中文由 pytest 的写手输出，再套一层引导只会互相遮蔽。
_DELIVERY_DIRS = ("scripts", "examples", "pm")
_NON_DELIVERY_PARTS = {"tests", "tests_modelhub", "__pycache__"}


def _is_delivery(rel: str) -> bool:
    parts = rel.split("/")
    if len(parts) == 1:
        return True
    return parts[0] in _DELIVERY_DIRS and not any(p in _NON_DELIVERY_PARTS for p in parts)


_GUARD_MARKERS = ("ensure_utf8_stdio", "prepare_console")
_MAIN_MARKERS = ('__name__ == "__main__"', "__name__ == '__main__'")
_GLYPH = "⚠"  # U+26A0：GBK 放不下，正是 2026-10-13 崩掉的那个字符


def _gbk_impossible_chars(source: str) -> set[str]:
    """字面量里 GBK 编不出的字符。注释与 docstring 排除：它们永远不流向 stdout。"""
    tree = ast.parse(source)
    spans: list[tuple[int, int]] = []
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list) or not body:  # Lambda 的 body 是单个表达式，不是语句块
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            spans.append((first.value.lineno, first.value.end_lineno or first.value.lineno))
    bad: set[str] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        if any(s <= node.lineno <= e for s, e in spans):
            continue
        for ch in node.value:
            if ord(ch) < 128:
                continue
            try:
                ch.encode("gbk")
            except UnicodeEncodeError:
                bad.add(ch)
    return bad


def _entry_scripts() -> list[Path]:
    """只认 **git 已跟踪** 的入口脚本：本机未跟踪的一次性探针不该把门禁判红
    （口径同 tests/test_hooks_actually_block.py 那条被 orders.db 骗过的教训）。"""
    out = subprocess.run(
        ["git", "ls-files", "*.py"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=ROOT,
        check=False,
    )
    if out.returncode != 0:
        pytest.skip(f"git ls-files 不可用（rc={out.returncode}），无法界定跟踪集")
    rels = [ln.strip().replace("\\", "/") for ln in out.stdout.splitlines() if ln.strip()]
    scripts: list[Path] = []
    for rel in rels:
        if not _is_delivery(rel):
            continue
        path = ROOT / rel
        source = path.read_text(encoding="utf-8", errors="replace")
        if any(m in source for m in _MAIN_MARKERS):
            scripts.append(path)
    return scripts


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _run_python(code: str) -> subprocess.CompletedProcess[bytes]:
    """子里进程强制 gbk 码页起（宿主忘了设、或 CI 的 UTF-8 码页都复现不了这次事故）。"""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "gbk"
    env.pop("PYTHONUTF8", None)
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        env=env,
        cwd=str(ROOT),
        timeout=120,
        check=False,
    )


def _guard_wired(source: str) -> bool:
    """护栏真的接上了 = 导入了 helper **且** 有一处调用它。

    只查字符串出现过是不行的：注释里写一句 `ensure_utf8_stdio` 就能骗过判据，
    而那正是"看着接了、其实没调"的形态（本仓库吃过入口引导漏接线的那类红）。
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    imported = any(
        isinstance(node, ast.ImportFrom)
        and node.module
        and node.module.endswith("bootstrap")
        and any(a.name in _GUARD_MARKERS for a in node.names)
        for node in ast.walk(tree)
    )
    called = any(
        isinstance(node, ast.Call) and getattr(node.func, "id", "") in _GUARD_MARKERS
        for node in ast.walk(tree)
    )
    return imported and called


def test_entry_scripts_printing_unencodable_glyphs_wire_the_shared_guard() -> None:
    scripts = _entry_scripts()
    assert scripts, "一个入口脚本都没扫到：_DELIVERY_DIRS 与目录结构漂了，先修这条判据的素材"

    violators = []
    for path in scripts:
        source = _source(path)
        bad = _gbk_impossible_chars(source)
        if bad and not _guard_wired(source):
            violators.append(
                f"{path.relative_to(ROOT).as_posix()} 打印 {''.join(sorted(bad))} 却没接 pm/bootstrap 的引导"
            )
    assert not violators, (
        "这些入口在中文 Windows 控制台（cp936）上会当场崩在 print：\n  - "
        + "\n  - ".join(violators)
        + "\n接法：把仓库根插进 sys.path 后 `from pm.bootstrap import ensure_utf8_stdio`，"
        "在任何 print 之前调用一次。"
    )


def test_no_entry_script_hand_rolls_a_console_reconfigure() -> None:
    """引导只有一份。各写一份 `reconfigure(..., errors=…)` 迟早漂成两种行为
    （一个乱码、一个崩），而两条都只在中文控制台上显形。"""
    dupes = [
        path.relative_to(ROOT).as_posix()
        for path in _entry_scripts()
        if "sys.stdout.reconfigure" in _source(path) or "sys.stderr.reconfigure" in _source(path)
    ]
    assert not dupes, (
        "这些脚本自己写了 reconfigure，请改用 pm.bootstrap.ensure_utf8_stdio：\n  - "
        + "\n  - ".join(dupes)
    )


def test_the_guard_actually_rescues_a_gbk_console() -> None:
    code = (
        "import sys;"
        f"sys.path.insert(0, r'{ROOT}');"
        "from pm.bootstrap import ensure_utf8_stdio;"
        "ensure_utf8_stdio();"
        f"print('{_GLYPH}')"
    )
    proc = _run_python(code)
    assert proc.returncode == 0, (
        f"接了引导仍然崩在 gbk 码页：{proc.stderr.decode('utf-8', 'replace')[-400:]}"
    )
    assert _GLYPH in proc.stdout.decode("utf-8"), proc.stdout[:200]


def test_without_the_guard_a_gbk_console_crashes() -> None:
    """反向对照：这条红了说明 `PYTHONIOENCODING=gbk` 没生效（被宿主或 pytest 夹具吞了），
    上一条的绿就只是"宿主本来就是 UTF-8"，判据失去区分度。"""
    proc = _run_python(f"print('{_GLYPH}')")
    assert proc.returncode != 0, "不设防护也打得出来 —— gbk 码页没生效，编码判据已全部失效"
    assert b"UnicodeEncodeError" in proc.stderr


def test_logs_usage_survives_a_gbk_console_end_to_end(tmp_path: Path) -> None:
    """今天真崩的那条命令：--root 指着一棵含可回收目录的树，才会走到末尾那行 ⚠️。"""
    (tmp_path / "archive" / "venv" / "site-packages").mkdir(parents=True)
    (tmp_path / "archive" / "venv" / "site-packages" / "big.pyd").write_bytes(b"0" * 2048)
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "gbk"
    env.pop("PYTHONUTF8", None)
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "logs_usage.py"), "--root", str(tmp_path)],
        capture_output=True,
        env=env,
        cwd=str(ROOT),
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, (
        f"logs_usage 在 gbk 码页下 rc={proc.returncode}：\n"
        f"{proc.stderr.decode('utf-8', 'replace')[-400:]}"
    )
    assert "⚠" in proc.stdout.decode("utf-8", "replace"), "报表末尾那行没打出来"


def test_the_static_criterion_is_not_vacuous() -> None:
    """判据自己也要能被证伪：造一份"打印 ⚠ 又没接引导"的源码，静态分析必须点名。
    （少了这条，_gbk_impossible_chars 退化成恒不报，前两条就成了永久绿的摆设。）"""
    dirty = "print('结果 ⚠ 需要人工确认')\nif __name__ == '__main__':\n    pass\n"
    assert _GLYPH in _gbk_impossible_chars(dirty), (
        "静态分析抓不到字面量里的非 GBK 字符，判据 1 是空的"
    )
    clean = "'模块 docstring 里有 ⚠ 也不算：注释不流向 stdout'\nprint('普通中文与 ⇒ 的区别')\n"
    assert "⚠" not in _gbk_impossible_chars(clean), "docstring 被当成打印素材，判据会误报一堆脚本"


def test_a_comment_mentioning_the_guard_does_not_count_as_wired() -> None:
    """接线判据的假绿形态：名字出现在注释/字符串里也算"接了"，那这条门就等于没装。"""
    only_mentioned = (
        "# 记得接 ensure_utf8_stdio\nfrom pm.bootstrap import ensure_utf8_stdio\nprint('⚠')\n"
    )
    assert not _guard_wired(only_mentioned), "导入了却没调用，必须判没接"
    wired = "from pm.bootstrap import ensure_utf8_stdio\nensure_utf8_stdio()\nprint('⚠')\n"
    assert _guard_wired(wired), "真的接上了却判没接，判据会误报"
