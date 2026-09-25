"""依赖与打包声明的一致性。

为什么需要：`[project] dependencies` 一度是空的，真实版本全写在 requirements.txt 里。
表现是 `pip install .` 装出一个没有依赖的空壳（import 时就 ModuleNotFoundError），
而 CI 只跑 `pip install -r requirements.txt`，所以没人发现。这两份清单必须比对。
"""

from __future__ import annotations

import re
import subprocess
import sys
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


def test_console_scripts_resolve_and_share_the_bootstrap():
    """`[project.scripts]` 必须解析得到可调用对象，且与 run.py 共用同一套启动引导。

    两件事各自都真出过问题：① 这一段以前根本不存在，装完包"有包没入口"，
    Agent 只能写 `python /绝对路径/run.py`；② 一旦入口各自再写一遍 dotenv/sys.path/utf8，
    就会出现"`--help` 在一个入口能跑、另一个乱码"这种永远查不到根的分叉。
    """
    import ast
    import importlib

    scripts = _pyproject()["project"].get("scripts") or {}
    assert scripts, "装了包却没有命令 = 有包没入口"
    for name, spec in scripts.items():
        assert re.fullmatch(r"[\w.]+:[\w.]+", spec), f"{name} 的 {spec!r} 不是 module:attr 形式"
        module_name, attr = spec.split(":")
        target = getattr(importlib.import_module(module_name), attr)
        assert callable(target), f"{spec} 解析到的不是可调用对象"

    def called(src: str) -> set[str]:
        """真实调用到的函数名（按 AST，不按文本）。

        用文本 grep 会被注释骗：第一版断言 `"load_dotenv(" not in src` 正是因为 run.py 的
        注释里写了这几个字而假红 —— 判据要能跑，不能靠扫关键字。
        """
        out: set[str] = set()
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Call):
                fn = node.func
                if isinstance(fn, ast.Name):
                    out.add(fn.id)
                elif isinstance(fn, ast.Attribute):
                    out.add(fn.attr)
        return out

    def imported(src: str) -> set[str]:
        mods: set[str] = set()
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.ImportFrom):
                mods.add(node.module or "")
            elif isinstance(node, ast.Import):
                mods.update(a.name for a in node.names)
        return mods

    for label, path in (("run.py", "run.py"), ("pm/cli/console.py", "pm/cli/console.py")):
        src = (ROOT / path).read_text(encoding="utf-8")
        assert "prepare_console" in called(src), f"{label} 没走统一的启动引导"
        assert "dotenv" not in imported(src), f"{label} 自己 import 了 dotenv（引导该只有一份）"


def test_calibration_engine_is_inside_the_package():
    """校准引擎必须在包里：它原来是仓库根的 `calibrate_judge.py`，`pip install .` 装不走，
    于是装包后的 `calibrate` 子命令只能靠"往 sys.path 塞仓库根"兜底 —— 那只在这份检出里成立。
    """
    assert (ROOT / "pm" / "calibration.py").exists()
    assert not (ROOT / "calibrate_judge.py").exists(), "根目录旧脚本没删净：会出现两份引擎"
    cli_src = (ROOT / "pm" / "cli" / "calibrate.py").read_text(encoding="utf-8")
    assert "import calibrate_judge" not in cli_src, "CLI 还在按顶层脚本名导入"
    assert "sys.path.insert" not in cli_src, "装包态的 sys.path 兜底还留着"


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


def test_coverage_floors_agree_across_all_copies() -> None:
    """覆盖率地板的抄件必须同数：pyproject（全局线）、ci.yml（modelhub 分项线）、
    `docs/operations.md` 的门禁块与表格、根 README §五。数字一旦漂开，"照文档跑一遍门禁"
    就会得到一条比 CI 松得多的绿 —— 那比没有门禁更坏。

    真实漂移（2026-09-25 复测时发现）：CI 已经是 `--fail-under=56`，
    而文档的门禁块还写着 `--fail-under=32`（差 24 个点，且没人会红）。

    同日第二轮发现的另一半：**上面这三处当时都在断言里，唯独根 README 不在**。
    README 是访客第一眼读的那份，它写着"地板 87 / 分项线 56"，真闸已经是 92 / 82 ——
    断言绿着、最容易被照抄的文件漂着。所以这次把 README 纳进来。
    """
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    docs = (ROOT / "docs" / "operations.md").read_text(encoding="utf-8")
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

    mhub = re.search(r'--include="\*/modelhub/\*" --fail-under=(\d+)', ci)
    assert mhub, "ci.yml 里找不到 modelhub 分项线（改了写法要同步改这条断言）"
    floor_mhub = int(mhub.group(1))

    docs_floors = {
        m.group(1)
        for line in docs.splitlines()
        if "coverage report" in line  # 只比**可执行抄件**：叙述历史值的散文不是配置
        for m in re.finditer(r"--fail-under=(\d+)", line)
    }
    assert docs_floors == {str(floor_mhub)}, (
        f"docs/operations.md 的 --fail-under 抄件与 CI 不一致："
        f"文档 {sorted(docs_floors)} vs CI {floor_mhub}"
    )

    floor_all = int(re.search(r"^fail_under\s*=\s*(\d+)", pyproject, re.M).group(1))
    row_all = re.search(r"^\|\s*`pm/`\s*全量\s*\|.*\|\s*(\d+)\s*\|\s*$", docs, re.M)
    row_mhub = re.search(r"^\|\s*`pm/modelhub/\*`\s*\|.*\|\s*(\d+)\s*\|\s*$", docs, re.M)
    assert row_all and int(row_all.group(1)) == floor_all, (
        f"文档表格的全量地板列（{row_all and row_all.group(1)}）与 pyproject fail_under={floor_all} 不一致"
    )
    assert row_mhub and int(row_mhub.group(1)) == floor_mhub, (
        f"文档表格的 modelhub 地板列（{row_mhub and row_mhub.group(1)}）与 CI 的 {floor_mhub} 不一致"
    )

    # 根 README 是访客第一眼照抄的那份，所以它也算配置抄件。README 里"实测 x%"是**快照**
    # （允许比现状旧，只要在下面这行不漂的前提下重测时顺手改），"（地板 N）"才是抄件。
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    readme_floors = re.findall(r"（地板 (\d+)）", readme)
    assert sorted(readme_floors) == sorted([str(floor_all), str(floor_mhub)]), (
        f"README 的地板抄件与真闸不一致：README {sorted(readme_floors)} "
        f"vs pyproject fail_under={floor_all} / CI 分项线 {floor_mhub}"
        "（两条都要有：少一条会静默放过一整个文件的漂移）"
    )


def test_test_count_copies_match_the_live_collection() -> None:
    """ "多少条用例"这句话在三个文件里被抄了三遍，而它是那种**只会变、没人回头改**的数。

    2026-09-25 深夜实测：README 写 728+、docs/operations.md 写 844、`.pre-commit-config.yaml`
    写 844+，而活体收集是 886 条。所以这里不抄数，去问 pytest 本身。

    用 subprocess 打 `--collect-only`：它不执行用例（不会递归跑测试），实测约 3 秒，
    换来的是"文档说一套、门禁做另一套"这一整类漂移被钉住。

    ⚠️ **必须先认退出码，数字本身会骗人**：植入一条 import 就崩的用例后实测，pytest 仍然打印
    `886 tests collected, 1 error` 而 rc=2 —— 也就是说"读不到计数"这个假设是错的，
    只读那行会把"有个模块根本收集不动"读成"文档漂了 6 条"。（本机拿错解释器时是 39 条收集错误。）
    """
    out = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/",
            "--collect-only",
            "-q",
            "-o",
            "addopts=",  # 别把 pyproject 的 -q 叠上来，数法要固定
            "-p",
            "no:cacheprovider",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert out.returncode == 0, (
        f"收集本身就坏了（rc={out.returncode}），先去修套件，别急着改文档里的数：\n"
        f"{(out.stdout or '')[-600:]}\n{(out.stderr or '')[-300:]}"
    )
    m = re.search(r"(\d+) tests? collected", out.stdout or "")
    assert m, f"rc=0 却没有计数行，是数法变了（不是文档漂移）：\n{(out.stdout or '')[-600:]}"
    live = int(m.group(1))

    copies = {
        "README.md": re.search(
            r"CI 门禁；(\d+)\+? 条", (ROOT / "README.md").read_text(encoding="utf-8")
        ),
        "docs/operations.md": re.search(
            r"`pytest tests/ -q`（(\d+) 项",
            (ROOT / "docs" / "operations.md").read_text(encoding="utf-8"),
        ),
        ".pre-commit-config.yaml": re.search(
            r"(\d+)\+? 条用例", (ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")
        ),
    }
    stale = {
        k: (v.group(1) if v else "读不到")
        for k, v in copies.items()
        if v is None or int(v.group(1)) != live
    }
    assert not stale, (
        f"活体收集是 {live} 条，但这些文件里的抄件不是：{stale}。"
        "加了用例就顺手把这三处一起改（它们是同一句话的三份复印件）。"
    )
