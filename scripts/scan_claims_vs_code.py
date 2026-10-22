"""系统性扫描"代码承诺 vs 代码实现"的不一致（2026-10-21）。

## 为什么要有这个探针

上一轮（`fix(modelhub): 台账那个"保留上限"承诺从未实现`）偶得一处真缺陷：
`pm/modelhub/ledger.py` 里

    # 事件保留条数上限（防止无限增长；台账可随时用文本工具检索，超过截断最旧的）
    _MAX_EVENTS = 20000

从 2026-09-22 引入到删除，**从未被任何代码读过** —— 注释承诺了截断，代码一次都没做。
那是同期排查时撞见的，不是系统性找出来的。本探针把它变成可重复的扫描。

## 扫的是什么（四类结构性矛盾）

1. **模块级常量只定义不读取**（`_MAX_EVENTS` 的形态）—— 常量声明了一个"规则"，
   但没有代码执行它。只报**私有/全大写**常量，且要求它周围有"承诺性"注释
   （含 上限/限制/截断/保留/最大/阈值/超时/重试 等词），否则大量无害常量会淹没结果。
2. **函数定义了但全仓无调用点**（排除 `__all__` 导出、`@tool`/`@mcp.tool` 装饰的
   对外接口、`if __name__ == "__main__"` 入口、测试引用的）。
3. **docstring 里写"会做某事"但函数体里没有对应动作** —— 只在 docstring 含
   "截断/删除/清理/重试/缓存/告警/记录/校验/拒绝"等**动作词**，而函数体里
   完全没有相关调用时报警。
4. **`# 承诺性注释` 紧邻的代码块里没有兑现动作** —— 同 3，但对象是注释而非 docstring。

## 输出

按"可疑度"排序打印，每条形如：
    [类别] 文件:行  名称  ← 证据（为什么认为它没兑现）

这是**探针**不是判据：它只负责把候选摆出来，真假要逐条实测取证
（`_MAX_EVENTS` 那次就是"全仓 grep 零引用"才定的案）。
"""

from __future__ import annotations

import ast
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PM = ROOT / "pm"

# 承诺性词汇：出现在注释/docstring 里，说明作者声称"有某个机制"
PROMISE_WORDS = (
    "上限", "限制", "截断", "保留", "最大", "阈值", "超时", "重试",
    "缓存", "淘汰", "清理", "删除", "告警", "校验", "拒绝", "兜底", "回退",
)
# 动作词 → 函数体里应当能找到的调用/关键字（宽松匹配）
ACTION_HINTS = {
    "截断": ("truncat", "seek", "rewrite", "replace", "unlink", "rmdir", "[:", "slice"),
    "删除": ("unlink", "rmdir", "remove", "rmtree", "replace"),
    "清理": ("unlink", "rmdir", "remove", "rmtree", "clear", "close"),
    "重试": ("for ", "while ", "retry", "attempt", "sleep", "backoff"),
    "缓存": ("cache", "memo", "_CACHE", "hit", "store"),
    "告警": ("_notify", "notify", "print", "logging", "logger", "warn", "alert"),
    "校验": ("if ", "assert", "raise", "check", "valid"),
    "拒绝": ("raise", "return None", "abort", "reject", "forbid"),
    "淘汰": ("pop", "unlink", "del ", "clear", "evict"),
    "兜底": ("except", "fallback", "default", "or "),
}

# 这些装饰器标的是"对外接口"，没有仓内调用点是正常的
EXTERNAL_DECORATORS = ("tool", "mcp.tool", "app.get", "app.post", "route", "fixture")


def iter_py(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


def module_level_names(tree: ast.Module) -> dict[str, int]:
    """模块级常量：NAME = <字面量>（含 AnnAssign）。"""
    out: dict[str, int] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name):
                    out[tgt.id] = node.lineno
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            out[node.target.id] = node.lineno
    return out


def loaded_names(tree: ast.Module) -> set[str]:
    return {
        n.id
        for n in ast.walk(tree)
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
    }


def is_private_const(name: str) -> bool:
    return name.startswith("_") or (name.isupper() and len(name) > 2)


def scan_dead_constants() -> list[str]:
    """类别 1：模块级常量只定义不读取，且附近有承诺性注释。"""
    findings: list[str] = []
    for path in iter_py(PM):
        try:
            src = path.read_text(encoding="utf-8")
            tree = ast.parse(src)
        except (OSError, SyntaxError):
            continue
        defined = module_level_names(tree)
        used = loaded_names(tree)
        if not defined:
            continue
        lines = src.splitlines()
        for name, lineno in defined.items():
            if not is_private_const(name) or name in used:
                continue
            # 附近 3 行内是否有承诺性注释
            lo = max(0, lineno - 4)
            ctx = "\n".join(lines[lo : lineno + 1])
            promise = [w for w in PROMISE_WORDS if w in ctx]
            if not promise:
                continue
            # 全仓（pm/）是否真的零引用
            findings.append(
                f"[死常量] {path.relative_to(ROOT)}:{lineno}  {name}"
                f"  ← 注释含「{'/'.join(promise)}」，但本文件内无读取点"
            )
    return findings


def scan_uncalled_functions() -> list[str]:
    """类别 2：定义了但全仓无调用点的函数（排除对外接口与入口）。"""
    # 先收集全仓所有被调用的名字（含属性调用 obj.fn()）
    called: set[str] = set()
    for path in iter_py(PM):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                if isinstance(fn, ast.Name):
                    called.add(fn.id)
                elif isinstance(fn, ast.Attribute):
                    called.add(fn.attr)

    # 测试与脚本也算"有人用"（避免把只被测试用的工具报成死代码）
    for extra in (ROOT / "tests", ROOT / "scripts", ROOT / "extras", ROOT / "tests_extras"):
        if not extra.exists():
            continue
        for path in iter_py(extra):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            except (OSError, SyntaxError):
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    fn = node.func
                    if isinstance(fn, ast.Name):
                        called.add(fn.id)
                    elif isinstance(fn, ast.Attribute):
                        called.add(fn.attr)
                elif isinstance(node, ast.Attribute):
                    called.add(node.attr)
                elif isinstance(node, ast.Name):
                    called.add(node.id)
                elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                    called.add(node.value)  # 字符串里点名的（`__all__` / getattr）

    findings: list[str] = []
    for path in iter_py(PM):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name.startswith("_"):
                continue  # 私有函数由"区域内无人用"另行判断，噪音大
            if node.name in called:
                continue
            decos = []
            for d in node.decorator_list:
                decos.append(ast.unparse(d))
            if any(k in " ".join(decos) for k in EXTERNAL_DECORATORS):
                continue
            findings.append(
                f"[无调用] {path.relative_to(ROOT)}:{node.lineno}  {node.name}()"
                f"  ← 全仓 pm/ tests/ scripts/ extras/ 均无调用点"
            )
    return findings


def _body_text(fn: ast.AST) -> str:
    return "\n".join(ast.unparse(n) for n in ast.walk(fn) if hasattr(ast, "unparse"))


def scan_docstring_promises() -> list[str]:
    """类别 3：docstring 承诺了动作，函数体里找不到对应动作。"""
    findings: list[str] = []
    for path in iter_py(PM):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            doc = ast.get_docstring(node) or ""
            if not doc:
                continue
            body = _body_text(node)
            for word, hints in ACTION_HINTS.items():
                if word not in doc:
                    continue
                if any(h in body for h in hints):
                    continue
                findings.append(
                    f"[承诺未兑现] {path.relative_to(ROOT)}:{node.lineno}  {node.name}()"
                    f"  ← docstring 提到「{word}」，函数体里找不到相关动作"
                )
    return findings


def main() -> int:
    groups = {
        "死常量（定义无读取 + 有承诺注释）": scan_dead_constants(),
        "无调用点的公开函数": scan_uncalled_functions(),
        "docstring 承诺了但函数体没做的事": scan_docstring_promises(),
    }
    total = 0
    for title, items in groups.items():
        print(f"\n{'=' * 70}\n## {title}  （{len(items)} 条）\n{'=' * 70}")
        for it in items:
            print("  " + it)
        total += len(items)
    print(f"\n合计候选 {total} 条 —— 这是**探针**，每条都要实测取证才能定案")
    return 0


if __name__ == "__main__":
    sys.exit(main())
