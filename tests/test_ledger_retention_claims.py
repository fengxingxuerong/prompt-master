"""台账/日聚合的**承诺与实现必须一致**（2026-10-17）。

## 为什么单独成文件

本轮（`fix(test)` MCP 探针那刀）同期排查时抓到一处"注释说着做了、代码没做"的静默风险：
`pm/modelhub/ledger.py` 里

    # 事件保留条数上限（防止无限增长；台账可随时用文本工具检索，超过截断最旧的）
    _MAX_EVENTS = 20000

—— 这条常量从 2026-09-22 引入起就**只被定义、从未被读**（全仓 `pm/` + `tests/` 零引用，
`releases/*` 与 `build/` 里的是过期副本）。也就是说：注释承诺了"超过就截断最旧的"，
实际永远不截断。

它为什么危险，而不是"无所谓的小瑕疵"：
**台账是本仓的事实源** —— `usage_store.rebuild_from_ledger()` 整份扫它来重算日聚合
（`ledger.py` 自己的模块 docstring 明写这一点）。所以这里有两种可能的错误方向，
而它们**互相相反**：
  · 若真去实现截断 ⇒ 主动丢掉事实源的历史（`rebuild_from_ledger` 再也算不回旧日聚合）；
  · 若留着不实现 ⇒ 下一个人照着注释以为有上限，于是**相信"台账不会无限长"**，
    排查时不再往"文件太大"的方向想。
正确处置是把误导性的承诺删掉，而不是把它实现出来 —— 这条判据钉的就是这件事：
**不许在事实源模块里留"保留上限"这种未实现的承诺**。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / "pm" / "modelhub" / "ledger.py"


def _module_text() -> str:
    return LEDGER.read_text(encoding="utf-8")


def test_ledger_has_no_unimplemented_retention_cap() -> None:
    """`ledger.py` 不许留"保留条数上限"这类未实现的承诺。

    判据是**成对**的：出现了形如 `_MAX_EVENTS`/`_MAX_LINES`/`_RETENTION…` 的常量，
    就必须在同一个文件里能找到它的**读取点**（赋值不算）。
    这正是那条死常量的失效形态：定义在、注释在、读它的代码不存在。
    """
    text = _module_text()
    tree = ast.parse(text)

    defined: dict[str, int] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and re.search(
                    r"(MAX_EVENTS|MAX_LINES|MAX_ROWS|RETENTION)", tgt.id
                ):
                    defined[tgt.id] = node.lineno

    read_lines: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            read_lines.add(node.id)

    dead = {name: ln for name, ln in defined.items() if name not in read_lines}
    assert not dead, (
        f"这些常量只有定义、没有任何读取点（注释承诺的保留/截断其实没实现）：{dead} —— "
        "台账是**事实源**（usage_store.rebuild_from_ledger 整份扫它），"
        "真实现截断会主动丢历史；请删掉承诺，而不是补上截断"
    )


def test_ledger_does_not_promise_a_truncation_it_never_does() -> None:
    """反向保证：删承诺不等于可以顺手把截断写进去 —— 事实源不许被截断。

    这条防的是"过度修正"：有人读到上一条判据，可能反过来去实现 `_MAX_EVENTS`。
    `rebuild_from_ledger` 的输入就是这份台账，截断等于让重建算不回旧账。
    """
    text = _module_text()
    assert "rebuild_from_ledger" in text or "事实源" in text, (
        "ledger.py 不再声明自己是事实源了 —— 那样上一条判据的理由（不许截断）就失效了，"
        "请连同本文档一起复核"
    )
    # 任何"截断/裁掉最旧"的实际动作都应当显式登记，而不是悄悄出现在 append 路径上
    for bad in ("truncate(", "rotatingfile", "RotatingFile", ".seek(0)"):
        assert bad not in text, (
            f"台账里出现了 `{bad}` —— 它是事实源，截断/回写会丢历史；"
            "真要清理得走独立工具并留档（先例：scripts/logs_usage.py 只读审计、从不删除）"
        )


@pytest.mark.parametrize("name", ["_MAX_EVENTS"])
def test_the_known_dead_constant_stays_gone(name: str) -> None:
    """点名钉子：这个具体常量不许**被赋值**回来。

    只查"赋值/注解"，不查正文提及 —— 它的历史必须能写在注释里
    （"这里曾经有 X，为什么删掉"），否则下一个读到的人会重新加一遍。
    """
    tree = ast.parse(_module_text())
    assigned = {
        tgt.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        for tgt in node.targets
        if isinstance(tgt, ast.Name)
    }
    assert name not in assigned, (
        f"`{name}` 又被赋值回来了 —— 它在 2026-09-22 ~ 2026-10-17 期间"
        "从定义到删除都没有任何读取点，注释承诺的截断从未实现；"
        "若你确实要加保留策略，请连同**实现**与它丢历史的代价一起提交"
    )
