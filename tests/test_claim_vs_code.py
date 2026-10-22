""" "承诺了但没兑现"的常量，不许再靠偶然发现（2026-10-21）。

## 为什么要有这条

这类缺陷在本仓已经出现过**两次**，两次都是"偶得"而不是"被守卫抓到"：

1. `pm/modelhub/ledger.py` 的 `_MAX_EVENTS = 20000`（2026-09-22 引入）：注释写
   "超过截断最旧的"，但**从未被任何代码读过**。2026-10-17 同期排查时撞见，
   处置是**删掉承诺**（台账是事实源，真截断会主动丢历史）。
2. `pm/schemas.py` 的 `REGRESSION_MARGIN = 0.2`：注释写"回退判定：当前轮 avg 低于
   历史最佳超过该值"，`early_stop_reason` 的 docstring 也写"余量 = max(REGRESSION_MARGIN,
   2×noise)"，但实现用的 `noise_margin()` 里只有 `PLATEAU_MARGIN` —— 它被架空了。
   2026-10-21 处置是**兑现承诺**（回退与平台期各用各的余量），因为两者语义确实不同。

两次的共同形态：**常量声明了一条规则，但没有代码执行它**。而且它**不会自己暴露** ——
值恰好相同时行为毫无差异，只有有人去调它才会发现"改了没反应"。

## 判据

扫 `pm/**` 的模块级常量：凡是**名字像"规则/上限/余量"**（含 MARGIN / LIMIT / MAX /
MIN / RATIO / ALERT / THRESHOLD / BUDGET）**且带承诺性注释**的，必须在**全仓**
（`pm/`、`tests/`、`extras/`、`scripts/`）至少有一处**读取点**（`Load` 上下文）。

⚠️ 判据取**全仓**而不是本文件：写第一版探针时只查本文件，7 条候选里有 6 条其实
被别的模块 import 过去了 —— 那种误报会让人误删好常量。本文件就是被那次误报教出来的。

## 已知豁免

`pm/modelhub/ledger.py` 的 `_MAX_EVENTS` 已删除，不需要豁免。若将来又要加"保留策略"，
请连同**实现**与它丢历史的代价一起提交（见该模块 docstring）。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PM = ROOT / "pm"
# 判"有人读过"的范围：只用 pm/ 会漏掉"只被测试读"的常量（那也是活的）
SEARCH_ROOTS = ("pm", "tests", "extras", "tests_extras", "scripts")

# 名字里带这些词的常量，最容易被写成"声明了规则却没人执行"
RULE_NAME = re.compile(r"(MARGIN|LIMIT|MAX|MIN|RATIO|ALERT|THRESHOLD|BUDGET|CAP)")
# 注释里的承诺性词汇
PROMISE_WORDS = (
    "上限",
    "限制",
    "截断",
    "保留",
    "阈值",
    "超过",
    "判定",
    "余量",
    "不得",
    "必须",
    "至少要",
    "最多",
)


def iter_py(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


def module_consts(tree: ast.Module) -> dict[str, int]:
    out: dict[str, int] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name):
                    out[tgt.id] = node.lineno
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            out[node.target.id] = node.lineno
    return out


def _executable_reads(name: str) -> int:
    """统计 `name` 在**可执行代码**里被读取的次数（不算注释、不算 docstring、不算定义处）。

    第一版判据只数"文本里出现几次"，于是一个只在 docstring 和注释里被提到的常量
    也会被算成"有人读"——变异验证当场暴露了这个假绿（把 `reg_margin = regression_margin(noise)`
    改成 `PLATEAU_MARGIN` 后，守卫仍然绿）。
    这里用 AST：只数 `Name(ctx=Load)` 与属性读取，docstring 是 `Constant` 不算。
    """
    count = 0
    for root_name in SEARCH_ROOTS:
        root = ROOT / root_name
        if not root.exists():
            continue
        for path in iter_py(root):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (OSError, SyntaxError):
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                    if node.id == name:
                        count += 1
                elif isinstance(node, ast.Attribute) and node.attr == name:
                    count += 1
    return count


def _promise_near(lines: list[str], lineno: int) -> list[str]:
    lo = max(0, lineno - 4)
    ctx = "\n".join(lines[lo : lineno + 1])
    return [w for w in PROMISE_WORDS if w in ctx]


def test_rule_constants_with_promises_are_actually_read() -> None:
    """带承诺注释的"规则类"常量，必须在全仓**可执行代码**里至少有一处读取点。"""
    offenders: list[str] = []
    for path in iter_py(PM):
        try:
            src = path.read_text(encoding="utf-8")
            tree = ast.parse(src)
        except (OSError, SyntaxError):
            continue
        consts = module_consts(tree)
        if not consts:
            continue
        lines = src.splitlines()
        for name, lineno in consts.items():
            if not RULE_NAME.search(name):
                continue
            promise = _promise_near(lines, lineno)
            if not promise:
                continue
            # 定义处本身就是一次 Store，不计入；要求别处有 Load
            if _executable_reads(name) > 0:
                continue
            offenders.append(
                f"{path.relative_to(ROOT)}:{lineno}  {name}"
                f"  ← 注释含「{'/'.join(promise)}」，但全仓可执行代码里找不到读取点"
            )
    assert not offenders, (
        "这些常量声明了一条规则，却没有代码执行它（改了也不会生效）：\n  "
        + "\n  ".join(offenders)
        + "\n\n两种正确处置（别默认选第一种）：\n"
        "  · 承诺**值得兑现** ⇒ 像 REGRESSION_MARGIN 那样，让判定真的读它，并补行为判据；\n"
        "  · 承诺**不该兑现** ⇒ 像 _MAX_EVENTS 那样删掉承诺（并写清为什么不实现）。"
    )


def test_the_two_known_cases_are_settled() -> None:
    """点名钉住两次已知案例的**处置方向**，防止有人"修反"。

    · `_MAX_EVENTS`：删承诺（台账是事实源，截断会丢历史）——不许回来；
    · `REGRESSION_MARGIN`：兑现承诺（回退与平台期语义不同）——必须仍被判定读取。
    """
    ledger = (PM / "modelhub" / "ledger.py").read_text(encoding="utf-8")
    assert not re.search(r"^\s*_MAX_EVENTS\s*=", ledger, re.M), (
        "_MAX_EVENTS 又被定义了 —— 台账是事实源（rebuild_from_ledger 整份扫它），"
        "真截断会主动丢历史；要加保留策略请连同实现与代价一起提交"
    )

    schemas = (PM / "schemas.py").read_text(encoding="utf-8")
    assert "def regression_margin(" in schemas, (
        "regression_margin() 不见了 —— 回退余量会退回被 PLATEAU_MARGIN 架空的状态"
    )
    tree = ast.parse(schemas)
    early = next(
        n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "early_stop_reason"
    )
    body = ast.get_source_segment(schemas, early) or ""
    assert "regression_margin(" in body, (
        "early_stop_reason 不再调用 regression_margin() —— REGRESSION_MARGIN 又被架空了"
    )
