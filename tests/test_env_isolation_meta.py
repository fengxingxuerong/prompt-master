"""测试隔离的**元测试**：宿主 `.env` 不许悄悄改变单测结果。

## 为什么要有这条（元测试守元测试）

本仓已经被"宿主机 `.env` 污染测试进程"咬过 **三次**，每次都是同一种形状：

| 日期 | 漏清的键 | 后果 |
|---|---|---|
| 2026-09-22 | `PM_JUDGE_JITTER` | 单跑绿、全跑红（顺序依赖） |
| 2026-10-05 | `PM_EVALUATOR_JITTER` 等三个座位键 | 仲裁线被抬到 6.12，4 条仲裁用例红 |
| 2026-10-05 | `PM_FORCE_JSON_CHANNEL` | 静默切到文本通道 |

前两次的代价是**某条用例偶然变红才被发现**。问题是：发现机制是偶然的。
本模块把"发现偶然"换成"发现必然"——

## 判据

扫 `pm/**` 里所有**会改控制流**的 `PM_*` 读取点，
要求每一个都在 conftest 的隔离清单里（连接类键归 `isolate_host_connection_env`）。

新增一个 `PM_FOO=os.getenv(...)` 的行为开关时，**这条会立刻红**，
逼着作者同时决定"它属于连接类还是行为类"以及"conftest 里要不要钉"。
这正是本仓 2026-10-05 那三次修复该有的那道闸。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

PM_ROOT = Path(__file__).resolve().parents[1] / "pm"
CONFTEST = Path(__file__).resolve().parent / "conftest.py"

# 扫出所有 `os.getenv("PM_X"` / `os.environ["PM_X"]` 形式的字面量键
_LITERAL = re.compile(
    r"""(?:os\.getenv|os\.environ\.get)\(\s*["'](PM_[A-Z0-9_]+)["']"""
    r"""|os\.environ\[\s*["'](PM_[A-Z0-9_]+)["']\s*\]"""
)
# f-string 拼出来的（PM_{seat}_JITTER 之类）扫不到，单独白名单
_DYNAMIC = re.compile(r"""f["']PM_\{[^}]+\}""")

# 由 isolate_host_connection_env 负责的"连接类"键（后缀判定，与该夹具一致）
_CONNECTION_SUFFIXES = ("_API_KEY", "_BASE_URL", "_MODEL")


def _rel(p: Path) -> str:
    """POSIX 化相对路径——Windows 上 `rglob` 给的是反斜杠，不统一会让断言
    在 Windows 上红、在 Linux 上绿（本仓 CI 两边都跑）。"""
    return p.relative_to(PM_ROOT.parent).as_posix()


def _literal_keys() -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for path in sorted(PM_ROOT.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for m in _LITERAL.finditer(text):
            # 两个分支各有一个捕获组，取先匹配到的那个（不是 None 的）
            found.setdefault(m.group(1) or m.group(2), []).append(_rel(path))
    return found


def _conftest_body() -> str:
    return CONFTEST.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 判据一：扫出来的键必须在 conftest 里有**生效的**隔离
# ---------------------------------------------------------------------------
def _mentioned_only_in_comment(key: str) -> bool:
    """该键只出现在注释/文档字符串里，**没有任何生效的 setenv/delenv**。

    这是变异测试逼出来的判据：初版只 grep 原文，于是
    `("PM_FAKE_BACKEND", "")` 那行删掉后，注释里的 "PM_FAKE_BACKEND=progress"
    仍然让测试保持绿色——一个不会失败的闸比没有闸更糟。
    """
    tree = ast.parse(_conftest_body())
    literals: set[str] = set()
    for node in ast.walk(tree):
        # 只看真正会被执行的调用：setenv / delenv
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = getattr(func, "attr", getattr(func, "id", ""))
        if name not in ("setenv", "delenv") or not node.args:
            continue
        # 第一参数可能是常量，也可能是**元组/列表循环**的绑定变量：
        #   for key, default in ((...), (...)): monkeypatch.setenv(key, default)
        #   for key in ("A", "B"): monkeypatch.delenv(key, raising=False)
        # 后一种形参名本身不是键，所以要把 for 循环里解包出来的元组一并收进来。
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            literals.add(first.value)
    literals |= _keys_in_for_loops(tree)
    return key not in literals


def _keys_in_for_loops(tree: ast.Module) -> set[str]:
    """收集 `for ... in ((k, v), (k, v))` / `for x in ("K",)` 里的全部字符串常量。

    conftest 的隔离大量写成这种批量形式；只认 `setenv("KEY", ...)` 会把
    那些键全部误判成"只在注释里出现"。
    """
    out: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.For) or not isinstance(node.iter, (ast.Tuple, ast.List)):
            continue
        for elt in node.iter.elts:
            for sub in ast.walk(elt):
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                    out.add(sub.value)
    return out


def _covered_by_connection_fixture(key: str) -> bool:
    """`isolate_host_connection_env` 是**按后缀**清的，不是逐键列的。

    所以 `PM_TARGET_API_KEY` 在 conftest 文本里找不到字面量，但它被后缀规则覆盖了。
    这条函数把那条规则显式写出来——夹具改了规则，这里也要一起改。
    """
    return key.endswith(("_API_KEY", "_BASE_URL", "_MODEL")) or key in (
        "PM_API_KEYS",
        "PM_PROVIDER",
    )


def test_every_literal_pm_key_appears_in_conftest() -> None:
    """漏一条就红。新增行为开关时必须同时改 conftest（或按后缀被连接类夹具覆盖）。

    ⚠️ 这条曾经是个**假闸**：初版只要求键在 conftest 文本里出现过，
    而 `PM_FAKE_BACKEND` 在夹具里只出现在**注释里**（"一旦 .env 里留着…"），
    于是把那行隔离删掉，这条照样绿。
    变异测试（删掉该行 → 本条仍绿）把这个洞翻了出来。
    ⇒ 判据从"提到过"改成"**出现在可执行的 setenv/delenv 里**"。
    """
    keys = _literal_keys()
    missing = sorted(
        k for k in keys if not _covered_by_connection_fixture(k) and _mentioned_only_in_comment(k)
    )
    assert not missing, (
        "这些 PM_* 键在 pm/ 里被读取，但 tests/conftest.py 里没有**生效的**隔离：\n"
        + "\n".join(f"  {k}  <- {', '.join(keys[k])}" for k in missing)
        + "\n请判断它属于连接类（isolate_host_connection_env）还是行为类"
        "（isolate_behaviour_switches / isolate_jitter_config），并补进去。"
    )


def test_the_connection_fixture_really_covers_what_it_claims() -> None:
    """上面的后缀豁免是有代价的：规则必须真的生效，否则等于放行。"""

    body = _conftest_body().split("def isolate_host_connection_env(", 1)[1]
    for key in ("PM_TARGET_API_KEY", "PM_EVALUATOR_BASE_URL", "PM_MODEL"):
        up = key.upper()
        assert up.endswith(("_API_KEY", "_BASE_URL", "_MODEL")) or up in (
            "PM_API_KEYS",
            "PM_PROVIDER",
        ), f"{key} 不该被这条豁免覆盖"
        assert up in body or "_API_KEY" in body, "规则文本被改了？"


def test_the_scan_actually_finds_keys() -> None:
    """先证明扫描器没坏：扫到 0 个键的话上面那条是假的绿。"""
    keys = _literal_keys()
    assert len(keys) > 20, f"只扫到 {len(keys)} 个键，扫描器大概失效了"
    assert "PM_JUDGES" in keys, "已知的行为开关必须能被扫到"
    assert "PM_JUDGE_DISAGREEMENT" in keys, "仲裁触发线必须能被扫到"


def test_dynamic_pm_keys_are_known() -> None:
    """f-string 拼出来的键扫不到（`PM_{seat}_JITTER` 之类），用白名单钉住。

    这不是"就这几个"的乐观假设：下面这条断言在真出现**第 4 个**动态键时立刻红，
    逼着作者把它改成字面量（可被上面那条扫到）或显式加进隔离。
    """
    found = {
        _rel(p) for p in PM_ROOT.rglob("*.py") if _DYNAMIC.search(p.read_text(encoding="utf-8"))
    }
    assert found == {"pm/schemas.py", "pm/cache.py", "pm/llm.py"}, (
        f"出现了新的动态 PM_ 键构造：{sorted(found)}\n"
        "请确认它是否需要隔离——动态键没法被 test_every_literal_pm_key 扫到。"
    )
    body = _conftest_body()
    assert "_JITTER" in body, "座位抖动键的隔离不许被删掉（§二十三）"
    assert "PM_" in body.split("def isolate_host_connection_env(", 1)[-1][:2000], (
        "isolate_host_connection_env 必须在（它负责 _API_KEY / _BASE_URL / _MODEL 后缀）"
    )


# ---------------------------------------------------------------------------
# 判据二：隔离夹具本身不许腐化
# ---------------------------------------------------------------------------
def test_all_isolation_fixtures_are_still_autouse() -> None:
    """这些夹具一旦有人去掉 autouse，整轮测试静默回到"读宿主机 .env"。"""
    body = _conftest_body()
    for fixture in (
        "legacy_measurement_defaults",
        "isolate_behaviour_switches",
        "isolate_jitter_config",
        "isolate_host_connection_env",
        "seal_calibration_ledger",
        "no_shared_disk_cache",
        "pin_force_json_channel_off",
        "isolate_modelhub_write_paths",
    ):
        assert f"def {fixture}(" in body, f"{fixture} 被删了"
        head = body.split(f"def {fixture}(", 1)[0]
        assert "autouse=True" in head.rsplit("@pytest.fixture", 1)[-1], (
            f"{fixture} 还在，但已经不是 autouse 了 —— 隔离会静默失效"
        )


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("PM_JUDGES", "2"),
        ("PM_TARGET_MAX_CONCURRENCY", "4"),
        ("PM_JUDGE_DISAGREEMENT", "2.0"),
        ("PM_TIMEOUT", "120"),
    ],
)
def test_behaviour_switches_are_pinned_to_documented_defaults(key: str, expected: str) -> None:
    """钉回的值必须等于**文档**写的默认值，不能是随手写的。

    这条不是"检查一下"——本轮逐个对过 `.env.example`：
    `PM_TIMEOUT=120`（.env.example:17）、`PM_JUDGES=2`（:99 默认）、`PM_JUDGE_DISAGREEMENT=2.0`
    （:115）、`PM_TARGET_MAX_CONCURRENCY=4`（:127）。改这里就要同步改那份文档。
    """
    body = _conftest_body()
    assert f'("{key}", "{expected}")' in body, f"{key} 的隔离值应与 .env.example 一致（{expected}）"
    example = (PM_ROOT.parent / ".env.example").read_text(encoding="utf-8")
    assert f"{key}={expected}" in example, (
        f".env.example 里找不到 `{key}={expected}` —— 隔离值失去文档依据了"
    )


def test_connection_keys_stay_with_the_connection_fixture() -> None:
    """分工不许反串：连接类归 `isolate_host_connection_env`，行为类归本轮那条。"""
    body = _conftest_body()
    conn = body.split("def isolate_host_connection_env(", 1)[1]
    conn = conn.split("\n@pytest.fixture", 1)[0]
    for suffix in _CONNECTION_SUFFIXES:
        assert suffix in conn, f"连接类后缀 {suffix} 应由 isolate_host_connection_env 管"
    behav = body.split("def isolate_behaviour_switches(", 1)[1]
    behav = behav.split("\n@pytest.fixture", 1)[0]
    for suffix in _CONNECTION_SUFFIXES:
        assert suffix not in behav, f"{suffix} 属于连接类，放进行为类夹具会让职责反串"
