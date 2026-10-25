"""子进程读输出时必须显式声明编码，否则 Windows 上会静默退化成 GBK。

为什么钉这条（2026-10-31）：本机（中文 Windows，默认码页 cp936）跑全量 pytest 时，
`tests/test_rejudge_form.py` 的两条 `node` 用例在读取线程里抛了
`UnicodeDecodeError: 'gbk' codec can't decode byte 0xa8` —— 而它们是**绿着**的：

- `subprocess.run(..., text=True)` 不给 `encoding` 时，解码用 `locale.getpreferredencoding()`；
  在中文 Windows 上那是 cp936，而 node/python 子进程吐的是 UTF-8；
- 解码器在**读取线程**里抛，`rc.stdout` / `rc.stderr` 于是是空串，
  断言 `"冒烟全部通过" in rc.stdout` 变成一个永远为假的判据，
  而 `assert rc.returncode == 0, rc.stderr` 的失败信息也变成空白 —— 报错时什么都没得看。

两条路都是"假绿/假红"，而 CI 的 linux job 上 locale 是 UTF-8，永远不会暴露它。
所以这条闸放在用例层：谁再写一个不带 encoding 的 `text=True`，这里就红。

判据只看 AST，不跑子进程：`text=True` / `universal_newlines=True` 与 `encoding=` 同时出现才算合规。
`capture_output=True` 但不带 `text=True` 的调用返回 bytes，不在此列（没有解码就没有这个坑）。
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 会解码 stdout/stderr 的 subprocess 入口（check_output 等价于 run(capture_output=True)）
_SPAWN_ATTRS = {"run", "Popen", "check_output", "check_call", "call"}

# 参与判定的目录：CI 的 lint/pytest 范围，与 pyproject 的 [tool.ruff] src 保持一致
_SCAN_DIRS = ("pm", "tests", "tests_extras", "extras", "scripts")


def _kwarg(call: ast.Call, name: str) -> ast.keyword | None:
    for kw in call.keywords:
        if kw.arg == name:
            return kw
    return None


def _is_true_literal(node: ast.expr | None) -> bool:
    return isinstance(node, ast.Constant) and node.value is True


def _python_files() -> list[Path]:
    out: list[Path] = []
    for d in _SCAN_DIRS:
        base = ROOT / d
        if base.is_dir():
            out.extend(sorted(base.rglob("*.py")))
    return out


def _violations() -> list[str]:
    found: list[str] = []
    for path in _python_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute) or func.attr not in _SPAWN_ATTRS:
                continue
            if not (isinstance(func.value, ast.Name) and func.value.id == "subprocess"):
                continue
            decodes = _is_true_literal(
                getattr(_kwarg(node, "text"), "value", None)
            ) or _is_true_literal(getattr(_kwarg(node, "universal_newlines"), "value", None))
            if not decodes:
                continue
            if _kwarg(node, "encoding") is not None:
                continue
            rel = path.relative_to(ROOT).as_posix()
            found.append(
                f"{rel}:{node.lineno} subprocess.{func.attr}(..., text=True) 没有 encoding="
            )
    return found


def test_every_text_mode_subprocess_pins_utf8() -> None:
    found = _violations()
    assert not found, (
        "子进程解码没锁 UTF-8（中文 Windows 上会抛 UnicodeDecodeError 并把输出变成空串）：\n  "
        + "\n  ".join(found)
    )


def test_the_guard_can_actually_fail() -> None:
    """闸本身的阴性对照：判据能认出一个真实的违规写法。

    没有这条，`_violations()` 里的过滤条件写错（比如 attr 名拼错）会让整条闸永远绿——
    本仓库对"配置存在但从未真跑"的状态有过专门记录，这里不重蹈。
    """
    src = "import subprocess\nsubprocess.run(['x'], capture_output=True, text=True)\n"
    tree = ast.parse(src)
    bad = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr in _SPAWN_ATTRS and _kwarg(node, "encoding") is None:
            bad += 1
    assert bad == 1, "阴性对照没生效：判据认不出不带 encoding 的调用"
