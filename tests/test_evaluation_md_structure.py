"""`docs/evaluation.md` 的结构守卫：这本账本被自己的自编辑探针砸过一次。

事故（2026-10-11 修复）：§三十·十一 落盘时，自编辑探针把要追加的脚本原文
（`import re` 到 `d = 三个连续双引号` 共 5 行）连同正文一起贴进了文档，
且那个三引号从未闭合——自 §三十·十二 起约 1100 行一直泡在这个未终止字符串里，
markdown 结构至此断裂。修复记录见该文 §三十·十一 末尾的「账本修复注」。

这里钉三条，均为当日事故的真实签名：
1. 围栏外不得出现裸 `import` / `from pathlib import` 行（探针脚本原文的形态；
   正文里的行内代码以反引号开头，不会命中）；
2. 全文不得出现三个连续双引号——那是当时吞掉 1100 行的机制本身。
   若将来确需在围栏内引用 Python 文档字符串，请连同本守卫一起改，
   并在提交信息里说明为什么机制不再危险；
3. 文件以换行结尾（修复前缺失；pre-commit 没有管这一条的钩子）。
"""

from __future__ import annotations

from pathlib import Path

DOC = Path(__file__).resolve().parent.parent / "docs" / "evaluation.md"

TRIPLE_QUOTE = chr(34) * 3


def check_structure(text: str) -> list[str]:
    """返回结构问题列表（空 = 干净）。独立成纯函数是为了变异验证。"""
    problems: list[str] = []
    if TRIPLE_QUOTE in text:
        problems.append("正文里出现三个连续双引号——§三十·十一 事故的吞行机制")
    in_fence = False
    for i, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if line.startswith(("import ", "from pathlib import")) or (
            len(line) > 2 and line[0].islower() and " = Path(" in line
        ):
            problems.append(f"第 {i} 行围栏外出现脚本形态：{line[:60]}")
    if not text.endswith(chr(10)):
        problems.append("文件不以换行结尾")
    return problems


def test_evaluation_md_structure_survives_self_edit_probes() -> None:
    text = DOC.read_text(encoding="utf-8")
    assert check_structure(text) == []
