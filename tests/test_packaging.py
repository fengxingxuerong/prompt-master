"""依赖与打包声明的一致性。

为什么需要：`[project] dependencies` 一度是空的，真实版本全写在 requirements.txt 里。
表现是 `pip install .` 装出一个没有依赖的空壳（import 时就 ModuleNotFoundError），
而 CI 只跑 `pip install -r requirements.txt`，所以没人发现。这两份清单必须比对。
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _split(spec: str) -> tuple[str, str, str]:
    """`uvicorn[standard]==0.34.2` → ("uvicorn", "standard", "0.34.2")。"""
    name, _, ver = spec.partition("==")
    name = name.strip()
    extras = ""
    m = re.match(r"^([A-Za-z0-9_.\-]+)\[([^\]]+)\]$", name)
    if m:
        name, extras = m.group(1), m.group(2)
    return (
        name.lower(),
        ",".join(sorted(e.strip() for e in extras.split(",") if e.strip())),
        ver.strip(),
    )


def _requirements() -> dict[str, tuple[str, str]]:
    out: dict[str, tuple[str, str]] = {}
    for raw in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-") or "==" not in line:
            continue
        name, extras, ver = _split(line)
        out[name] = (extras, ver)
    return out


def _pyproject() -> dict:
    with (ROOT / "pyproject.toml").open("rb") as fh:
        return tomllib.load(fh)


def test_pyproject_dependencies_match_requirements():
    proj = _pyproject()["project"]
    declared = {}
    for spec in proj["dependencies"]:
        name, extras, ver = _split(spec)
        declared[name] = (extras, ver)

    reqs = _requirements()
    runtime = {k: v for k, v in reqs.items() if k not in {"pytest", "ruff", "mypy"}}

    missing = sorted(set(runtime) - set(declared))
    assert not missing, f"requirements.txt 里有、pyproject 里没声明：{missing}"
    drift = {k: (runtime[k], declared[k]) for k in runtime if runtime[k] != declared[k]}
    assert not drift, f"两份清单版本/extra 不一致：{drift}"


def test_dev_extras_cover_ci_tools():
    dev = {n for n, _, _ in map(_split, _pyproject()["project"]["optional-dependencies"]["dev"])}
    assert {"pytest", "ruff", "mypy"} <= dev


def test_installable_package_layout_declared():
    """平铺布局必须显式声明 packages，否则 setuptools 自动发现会直接报错。"""
    cfg = _pyproject().get("tool", {}).get("setuptools", {})
    assert cfg.get("packages") == ["pm", "pm.nodes", "pm.cli"], (
        "应只安装 pm 包与 pm.nodes / pm.cli 子包（run.py / run_server.py 是仓库入口薄壳）"
    )
    assert (ROOT / "pm" / "__init__.py").exists()
    assert (ROOT / "pm" / "nodes" / "__init__.py").exists()
    assert _pyproject().get("build-system", {}).get("requires"), (
        "缺 build-system 时 pip install . 用的是猜测的默认值"
    )


def test_requires_python_covers_ci_matrix():
    """CI 矩阵里最老的 Python 版本必须 >= requires-python，否则装完就跑不起来。"""
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    matrix = re.search(r"python-version:\s*\[([^\]]+)\]", ci)
    assert matrix, "CI 里找不到 python-version 矩阵"
    versions = re.findall(r'"(\d+)\.(\d+)"', matrix.group(1))
    floor = _pyproject()["project"]["requires-python"]
    minor = int(re.search(r">=\s*(\d+)\.(\d+)", floor).group(2))
    assert min(int(v[1]) for v in versions) >= minor, (
        f"requires-python {floor} 比 CI 矩阵里的最低版本还高"
    )
