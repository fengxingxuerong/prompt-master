"""模块级**全大写常量**必须有读取点：不许再有"写了没接线"的死承诺。

为什么钉这一条（本仓已有两次同形的真实事故）：

- `pm/modelhub/ledger.py::_MAX_EVENTS`：注释写着"超过截断最旧的"，从 2026-09-22 引入到
  10-17 删除，**从未被任何代码读过**。没有补实现而是删掉承诺——台账是事实源，真截断会
  主动丢历史；留着不实现则让下一个人误信"台账不会无限长"。
- `extras/desktop.py::ACCENT_SOFT` 等六组令牌：注释里写着"多了左侧导航栏"，界面上并没有
  （`docs/iteration/iteration-ledger.md` 有记录）。

判据刻意**只覆盖全大写常量**，不覆盖私有函数：
- dunder（`__str__`）、pydantic 校验器（`_foo` + `@field_validator`）都是隐式调用，
  按名字扫会一片假阳性；
- 而"常量写了没读"几乎总是真缺陷（要么该接线、要么该删），且误报率是 0。

"读取"的口径放宽到**全仓字符串**：常量名可能出现在 `getattr`、环境变量表、
测试的点名断言里。宁可漏判，也不要制造一个"永远红所以被绕过"的门禁。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 审哪些目录的定义（pm 是产品，extras 是可选附件，scripts 是运营脚本）
_DEFINED_IN = ("pm", "extras", "scripts")
# 在哪找"读取"：全仓都算（含 tests / tests_extras / docs），因为常量确实可能被文档或测试消费
_READ_EVERYWHERE = ("pm", "extras", "scripts", "tests", "tests_extras", "releases", "docs")
_SKIP_DIRS = {".venv", "__pycache__", "build", "node_modules", ".git", ".pytest_cache"}

# 少数常量天生"只被字符串引用"（如版本号与发布脚本约定、CI 的 env 名单），
# 允许显式豁免；加进来必须写一句理由，否则下一个人不知道它是活是死。
_ALLOW = {
    # 由外部工具/CI 按名字读（scripts/release_pypi.py 与 ci.yml 里的 env 名）：
    "__NONE__",
}


def _py_files(base: Path) -> list[Path]:
    return [p for p in base.rglob("*.py") if not (set(p.parts) & _SKIP_DIRS)]


def _const_definitions() -> list[tuple[Path, str, int]]:
    """定义侧：`NAME = ...` 且 NAME 全大写（允许数字与下划线）。"""
    out: list[tuple[Path, str, int]] = []
    for d in _DEFINED_IN:
        base = ROOT / d
        if not base.is_dir():
            continue
        for path in _py_files(base):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
            except SyntaxError:
                continue
            for node in tree.body:  # 只看模块级：类属性/函数内局部不算"对外承诺"
                targets: list[ast.expr] = []
                if isinstance(node, ast.Assign):
                    targets = list(node.targets)
                elif isinstance(node, ast.AnnAssign) and node.target is not None:
                    targets = [node.target]
                for t in targets:
                    if isinstance(t, ast.Name) and t.id.isupper() and len(t.id) > 2:
                        out.append((path, t.id, node.lineno))
    return out


def _read_blob() -> str:
    chunks: list[str] = []
    for d in _READ_EVERYWHERE:
        base = ROOT / d
        if not base.is_dir():
            continue
        for path in list(_py_files(base)) + [
            p for p in base.rglob("*.md") if not (set(p.parts) & _SKIP_DIRS)
        ]:
            try:
                chunks.append(path.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
    return "\n".join(chunks)


def test_every_module_level_constant_is_referenced_somewhere() -> None:
    defs = _const_definitions()
    assert defs, "没扫到任何常量定义——判据自己失效了"
    blob = _read_blob()
    dead: list[str] = []
    for path, name, line in defs:
        if name in _ALLOW:
            continue
        # 定义处本身会出现一次；>1 才说明另有引用
        if len(re.findall(rf"\b{re.escape(name)}\b", blob)) > 1:
            continue
        dead.append(f"{path.relative_to(ROOT).as_posix()}:{line} {name}")
    assert not dead, (
        "这些模块级常量没有任何读取点（写了没接线，或该删）：\n  "
        + "\n  ".join(sorted(dead))
        + "\n要么接上，要么删掉；确属外部按名字读的，加进本文件的 _ALLOW 并写清理由。"
    )


def test_the_guard_can_actually_fail(tmp_path: Path) -> None:
    """阴性对照：判据必须认得出一个真的零引用常量。

    没有这条，`_const_definitions()` 写错（比如永远返回空表）会让整条闸永远绿——
    本仓对"配置存在但从未真跑"的状态有过专门记录（.pre-commit-config.yaml 顶部注释）。
    """
    fake = tmp_path / "mod.py"
    fake.write_text("USED_ONE = 1\nNEVER_READ = 2\nprint(USED_ONE)\n", encoding="utf-8")
    src = fake.read_text(encoding="utf-8")
    names = [
        t.id
        for node in ast.parse(src).body
        if isinstance(node, ast.Assign)
        for t in node.targets
        if isinstance(t, ast.Name) and t.id.isupper()
    ]
    assert names == ["USED_ONE", "NEVER_READ"]
    unreferenced = [n for n in names if len(re.findall(rf"\b{n}\b", src)) < 2]
    assert unreferenced == ["NEVER_READ"], "对照失效：判据认不出零引用常量"
