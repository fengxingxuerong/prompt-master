"""根 conftest 的临时根选择：坏目录不能再把整套门禁砸成"全部 setup ERROR"。

真实事故（2026-10-13 本机）：`.pytest_tmp/` 的 ACL 被宿主弄坏（`ls`/`icacls` 都拒绝访问），
一次全量跑 **1693 条全部在 setup 就 ERROR**、`--cov` 出 23%，而 ruff / mypy / selftest
全绿。写死一个路径并相信它 = 验证能力挂在别人手里。这些用例把"先探再选"钉住，
并保证退路落在检出之外（否则同一个坏目录还能第二次砸下来）。
"""

from __future__ import annotations

import os
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def conftest_mod():
    """只加载根 conftest 的**函数定义**，不跑它 import 期的副作用。

    为什么不能整份 exec：那份文件的 nt 分支会写 `os.environ["PYTEST_DEBUG_TEMPROOT"]`，
    而 pytest 是**每次建 tmp_path 时重读**这个变量的。第一版在这里先占好变量再 exec，
    等于把会话已经选好的可用根改回坏路径 —— 于是本文件 8 条用例全部卡在 setup，
    红的正是它要防的那个故障（现场就是这样，不是编的）。
    """
    source = (ROOT / "conftest.py").read_text(encoding="utf-8")
    head = source.split('if os.name == "nt":')[0]
    assert "def choose_temp_root" in head, "根 conftest 的结构变了：切不到副作用块，先修这条夹具"
    mod = types.ModuleType("_root_conftest_defs")
    mod.__file__ = str(ROOT / "conftest.py")
    exec(compile(head, mod.__file__, "exec"), mod.__dict__)
    return mod


def test_default_root_is_used_when_writable(conftest_mod, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(conftest_mod, "_REPO", tmp_path)
    root, warn = conftest_mod.choose_temp_root({})
    assert root == tmp_path / ".pytest_tmp", "默认可用时必须留在检出目录内（原行为零漂移）"
    assert warn is None, f"默认可用却报了退让：{warn}"


def test_unusable_default_falls_back_and_says_so(conftest_mod, tmp_path: Path, monkeypatch) -> None:
    """撞坏法：`.pytest_tmp` 是个**普通文件** —— 任何 OS 上 mkdir 它都会失败。"""
    (tmp_path / ".pytest_tmp").write_text("not a directory", encoding="utf-8")
    monkeypatch.setattr(conftest_mod, "_REPO", tmp_path)
    root, warn = conftest_mod.choose_temp_root({})
    assert root != tmp_path / ".pytest_tmp", "坏掉的默认位被当成可用 —— 那 1693 条 ERROR 会原样重演"
    assert warn, "退让必须出声：静默换根会让人把环境故障读成代码雪崩"
    assert str(tmp_path / ".pytest_tmp") in warn and str(root) in warn, (
        f"警告里要同时点名从哪退走、退到哪：{warn}"
    )


def test_fallback_lives_outside_the_checkout(conftest_mod, tmp_path: Path, monkeypatch) -> None:
    """退路若在检出里（换个名字也一样），下一次同类 ACL 事故会第二次砸下来。"""
    (tmp_path / ".pytest_tmp").write_text("x", encoding="utf-8")
    monkeypatch.setattr(conftest_mod, "_REPO", tmp_path)
    root, _ = conftest_mod.choose_temp_root({})
    assert tmp_path not in root.parents and root.parent != tmp_path, f"退路仍在检出内：{root}"


def test_fallback_root_is_absolute(conftest_mod, tmp_path: Path, monkeypatch) -> None:
    """写这条的理由很具体：`Path("D:") / "tmp"` 是**盘相对**路径（"D: 当前目录下"），
    上一版就这么拼过一次，实测把退路目录建进了检出目录（`<repo>/tmp/…`）。"""
    (tmp_path / ".pytest_tmp").write_text("x", encoding="utf-8")
    monkeypatch.setattr(conftest_mod, "_REPO", tmp_path)
    root, _ = conftest_mod.choose_temp_root({})
    assert root.is_absolute(), f"退路不是绝对路径，会随工作目录漂：{root}"


def test_caller_set_env_is_taken_verbatim(conftest_mod, tmp_path: Path) -> None:
    """显式设值时不替调用方探测 —— `tests/test_env_isolation_meta.py` 的夹具靠这条
    把临时根关进自己的 tmp_path；改成"也要验一遍"会让那类夹具莫名其妙改道。"""
    wanted = tmp_path / "caller-chosen"
    root, warn = conftest_mod.choose_temp_root({"PYTEST_DEBUG_TEMPROOT": str(wanted)})
    assert root == wanted
    assert warn is None


def test_every_candidate_broken_still_names_the_manual_escape(
    conftest_mod, tmp_path: Path, monkeypatch
) -> None:
    broken = tmp_path / "a" / ".pytest_tmp"
    broken.parent.mkdir()
    broken.write_text("x", encoding="utf-8")
    other = tmp_path / "b"
    other.write_text("x", encoding="utf-8")
    monkeypatch.setattr(conftest_mod, "_REPO", broken.parent)
    monkeypatch.setattr(conftest_mod, "_candidates", lambda: [broken, other])
    root, warn = conftest_mod.choose_temp_root({})
    assert root == broken, "全部不可用时保持原行为（不悄悄改根）"
    assert warn and "PYTEST_DEBUG_TEMPROOT" in warn, f"要给出人能手执的出口：{warn}"


def test_probe_leaves_no_litter(conftest_mod, tmp_path: Path) -> None:
    """探测目录必须当场清掉：它在检出根里，留着就成了"每次跑测试长一个目录"。"""
    assert conftest_mod._usable(tmp_path) is True
    # 只查自己那类残留：tests/conftest.py 的写入密封夹具会往 tmp_path 放 modelhub-store，
    # 断"目录全空"会把别人的夹具当成泄漏（第一版就是这么假红了一次）。
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.startswith("_probe-")]
    assert leftovers == [], f"探测留下了东西：{leftovers}"


def test_probe_returns_false_for_a_non_directory(conftest_mod, tmp_path: Path) -> None:
    """判据要认得出"路径存在但不是目录"这一形 —— 本机那次坏法比这还隐蔽（ACL 拒绝）。"""
    hostile = tmp_path / "blocked"
    hostile.write_text("x", encoding="utf-8")
    assert conftest_mod._usable(hostile) is False


@pytest.mark.skipif(os.name != "nt", reason="这条兜底只针对 Windows 上的临时根事故")
def test_the_running_session_actually_has_a_writable_temp_root() -> None:
    """端到端：能跑到这里就说明本轮选出来的根真的可用 —— 但"可用"不该靠推断，
    所以再当场开一个嵌套目录验一遍（撞坏 `.pytest_tmp` 时这条会连带变红/变退让）。"""
    root = Path(os.environ["PYTEST_DEBUG_TEMPROOT"])
    probe = root / f"_selfcheck-{os.getpid()}"
    probe.mkdir(parents=True, exist_ok=True)
    (probe / "inner").mkdir(parents=False, exist_ok=True)
    assert probe.exists()
    import shutil as _sh

    _sh.rmtree(probe, ignore_errors=True)
