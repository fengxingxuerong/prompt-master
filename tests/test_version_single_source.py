"""版本号必须只有一个事实源：`pm/__init__.py:__version__`。

起因（2026-09-25 盘点）：同一个仓库里四个数并存 —— `pyproject.toml` 1.1.0、
`pm/__init__.py` 2.0.0、`pm/server.py` 自报 2.1.0、git tag 到 v1.4.6。
结果是**没人能说清"当前版本是多少"**，而回滚/报缺陷都要引用版本号。
修法不是挑一个数抄到另外三处，而是让其余三处**从这里取**，并由下面的断言守着：
写死一个字面量，就等于再造一个会漂移的事实源。

ModelHub 网关是**故意**不同源的（它跟 releases/ 那条网关验收线走），
所以它的版本也收成单个常量、三处引用同一个名字，而不是三份 `"1.1.0"` 字面量。
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pm
from pm.modelhub import server as mh

ROOT = Path(__file__).resolve().parents[1]


def _pyproject() -> dict[str, object]:
    with (ROOT / "pyproject.toml").open("rb") as f:
        return tomllib.load(f)  # type: ignore[no-any-return]


def test_pyproject_declares_version_as_dynamic_and_sourced_from_pm() -> None:
    project = _pyproject()["project"]
    assert isinstance(project, dict)
    assert "version" not in project, "pyproject 里写死版本 = 第二个事实源"
    assert "version" in (project.get("dynamic") or []), '必须声明 dynamic=["version"]'
    tool = _pyproject()["tool"]
    assert isinstance(tool, dict)
    dynamic = tool["setuptools"]["dynamic"]  # type: ignore[index]
    assert dynamic["version"] == {"attr": "pm.__version__"}  # type: ignore[index]


def test_version_is_statically_resolvable_by_setuptools() -> None:
    """setuptools 求 `dynamic.version` 时**不 import 包**，而是静态读 AST。

    所以 `pm/__init__.py` 里那个赋值必须是"字符串字面量直接赋给 __version__"这一种形式；
    哪天有人改成 `__version__ = metadata.version(...)` 或 f-string，`pip install .` 就会
    在构建阶段报"version not found"（本机 venv 没装 setuptools，构建期真跑一次由 CI/发布机做，
    这条断言先把"形式必须可静态求值"钉住）。
    """
    import ast

    tree = ast.parse((ROOT / "pm" / "__init__.py").read_text(encoding="utf-8"))
    found: list[str] = []
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == "__version__":
                assert isinstance(node.value, ast.Constant) and isinstance(node.value.value, str), (
                    "__version__ 必须是字符串字面量，否则 setuptools 静态求值失败"
                )
                found.append(node.value.value)
    assert found == [pm.__version__], f"事实源应只有一个赋值且与运行时一致：{found}"


def test_rest_api_self_reported_version_matches_the_package() -> None:
    from pm.server import app

    assert app.version == pm.__version__, (
        f"API 自报 {app.version} 与包版本 {pm.__version__} 不一致："
        "别人按自报版本号来报缺陷，我们对不上是常事"
    )


def test_modelhub_gateway_version_is_a_single_constant() -> None:
    """网关版本号只许出现一次字面量；三处出口读同一个常量。"""
    src = (ROOT / "pm" / "modelhub" / "server.py").read_text(encoding="utf-8")
    assert src.count('"1.1.0"') == 1, "1.1.0 只能定义在 GATEWAY_VERSION 那一行"
    assert mh.app.version == mh.GATEWAY_VERSION
    assert mh.health()["version"] == mh.GATEWAY_VERSION
    assert mh.root()["version"] == mh.GATEWAY_VERSION


def test_no_new_hardcoded_version_literals() -> None:
    """别再长出第二个事实源：`pm/` 与两个入口里不许出现写死的 x.y.z 版本字面量。

    白名单只有两处：`pm/__init__.py`（事实源本身）与 `GATEWAY_VERSION = "..."`
    （网关那条独立发布线的常量定义行）。
    """
    import re

    pattern = re.compile(r"(?:version\s*=\s*|\"version\"\s*:\s*)[\"']\d+\.\d+\.\d+[\"']")
    offenders: list[str] = []
    scanned = [
        *sorted((ROOT / "pm").rglob("*.py")),
        ROOT / "run.py",
        ROOT / "run_server.py",
    ]
    for path in scanned:
        if path == ROOT / "pm" / "__init__.py":
            continue
        for no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "GATEWAY_VERSION" in line or pattern.search(line) is None:
                continue
            offenders.append(f"{path.relative_to(ROOT)}:{no}: {line.strip()}")
    assert not offenders, f"写死的版本号（应改为引用常量）：{offenders}"
