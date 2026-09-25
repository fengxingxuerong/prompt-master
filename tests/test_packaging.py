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
    # 排除"开发/门禁工具"：口径是 dev extras 里列了什么，而不是另抄一份名单。
    # 原来写死 {"pytest","ruff","mypy"}，于是往 requirements.txt 加一个 pytest-cov
    # 就会让这条测试红在"requirements 里有、pyproject 没声明"上 —— 一个必须两处同改的
    # 清单迟早会漏改，让它自己从 dev extras 推。
    dev = {name for name, _, _ in map(_split, proj["optional-dependencies"]["dev"])}
    runtime = {k: v for k, v in reqs.items() if k not in dev}

    missing = sorted(set(runtime) - set(declared))
    assert not missing, f"requirements.txt 里有、pyproject 里没声明：{missing}"
    drift = {k: (runtime[k], declared[k]) for k in runtime if runtime[k] != declared[k]}
    assert not drift, f"两份清单版本/extra 不一致：{drift}"


def test_dev_extras_cover_ci_tools():
    dev = {n for n, _, _ in map(_split, _pyproject()["project"]["optional-dependencies"]["dev"])}
    assert {"pytest", "ruff", "mypy"} <= dev


def _installed_packages_on_disk() -> list[str]:
    """从文件系统推导"应该被安装"的包：pm 本身 + 每个含 __init__.py 的子目录。

    为什么推导而不写死清单：这条测试原来是 `== ["pm","pm.nodes","pm.cli"]`，
    把 pm/web.py 拆成 pm/web/ 的那天它就红了（2026-09-18）——测试记住了旧世界，
    于是"加一个子包"必须记得同时改测试，忘了就是一条红 CI。
    推导之后它检查的是真正的不变量：**磁盘上的每个子包都被声明，且没有多余声明**。
    """
    found = {"pm"} if (ROOT / "pm" / "__init__.py").exists() else set()
    for init in ROOT.glob("pm/**/__init__.py"):
        if "__pycache__" in init.parts:
            continue
        rel = init.parent.relative_to(ROOT)
        found.add(".".join(rel.parts))
    return sorted(found)


def test_installable_package_layout_declared():
    """平铺布局必须显式声明 packages，否则 setuptools 自动发现会把 run.py 当成顶层模块。"""
    cfg = _pyproject().get("tool", {}).get("setuptools", {})
    declared = list(cfg.get("packages") or [])
    expected = _installed_packages_on_disk()
    assert expected, "pm 包必须存在（推导结果为空说明目录结构坏了）"
    assert set(declared) == set(expected), (
        f"声明与磁盘不一致：声明缺 {sorted(set(expected) - set(declared))}，"
        f"多声明 {sorted(set(declared) - set(expected))}"
        "（新增/删除子包时同步 pyproject 的 tool.setuptools.packages）"
    )
    # 仓库入口薄壳不能被当成顶层模块装进去
    for bogus in ("run", "run_server", "scripts", "tests"):
        assert bogus not in declared
    assert (ROOT / "pm" / "__init__.py").exists()
    assert (ROOT / "pm" / "nodes" / "__init__.py").exists()
    assert _pyproject().get("build-system", {}).get("requires"), (
        "缺 build-system 时 pip install . 用的是猜测的默认值"
    )


def test_precommit_hooks_cover_the_same_scope_as_ci():
    """`.pre-commit-config.yaml` 的检查范围必须与 CI 逐条相同。

    真实缺陷（2026-09-25 盘点）：钩子写的是 `pm/ tests/ run.py examples/` 与 `pm/ run.py`，
    两边都比 CI 少一个 `run_server.py` —— 于是"本地 pre-commit 绿"和"CI 绿"不是同一件事，
    而这正是 CI 注释里自己警告过的"第三种口径"。另外钩子默认会把 staged 文件追加到 args
    后面，等于**门禁范围随你这次改了哪些文件而变**。
    所以这里比两件事：三条命令的作用路径集合相等 + 每条钩子都 `pass_filenames: false`。
    """
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    cfg_path = ROOT / ".pre-commit-config.yaml"
    cfg = cfg_path.read_text(encoding="utf-8")

    def ci_scope(sub: str) -> set[str]:
        m = re.search(rf"run:\s*{re.escape(sub)}\s+(.+)", ci)
        assert m, f"CI 里找不到 `{sub}` 命令，改了这一条就要同步改测试"
        return {t for t in m.group(1).split() if not t.startswith("-")}

    def hook_scope(entry_head: str) -> set[str]:
        m = re.search(rf"entry:.*{re.escape(entry_head)}\s+(.+)", cfg)
        assert m, f"pre-commit 配置里找不到 `{entry_head}`"
        return {t for t in m.group(1).split() if not t.startswith("-")}

    pairs = (
        ("ruff check", "m ruff check"),
        ("ruff format --check", "m ruff format --check"),
        ("mypy", "m mypy"),
    )
    for sub, head in pairs:
        assert ci_scope(sub) == hook_scope(head), (
            f"`{sub}` 的检查范围与 CI 不一致：CI 多 {sorted(ci_scope(sub) - hook_scope(head))}，"
            f"钩子多 {sorted(hook_scope(head) - ci_scope(sub))}"
        )

    hooks = re.findall(r"- id: (\S+)(.*?)(?=\n      - id: |\n  - repo: |\Z)", cfg, re.S)
    assert hooks, "解析不到任何钩子"
    for hook_id, body in hooks:
        assert "pass_filenames: false" in body, (
            f"钩子 {hook_id} 没设 pass_filenames: false —— "
            "staged 文件会被追加进 args，门禁范围就随提交内容变化"
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
