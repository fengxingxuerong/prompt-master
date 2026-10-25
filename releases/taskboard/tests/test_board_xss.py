"""看板页的存储型 XSS 回归（2026-10-31 只读审计实测）。

`title` / `assignee` 来自**用户提交的任务**。旧版把它们裸拼进 innerHTML，
一条 `<img src=x onerror=alert(1)>` 的标题就能让运营者打开看板即执行。
"""

from __future__ import annotations

import re
from pathlib import Path

PAGE = Path(__file__).resolve().parents[1] / "static" / "board.html"


def _js() -> str:
    src = PAGE.read_text(encoding="utf-8")
    return "\n".join(re.findall(r"<script>(.*?)</script>", src, re.S))


def test_page_defines_an_escaper() -> None:
    assert "function esc" in _js(), "看板页没有转义函数"


def test_task_fields_are_escaped_in_the_due_table() -> None:
    js = _js()
    for dangerous in (
        "<td>${r.title}",
        "<td>${r.assignee}",
        "${r.task_id}</td>",
        "onclick=\"transit('${r.task_id}",
        '<span class="badge b-${r.status}">${b.status_labels[r.status]}',
    ):
        assert dangerous not in js, f"发现未转义的任务字段 `{dangerous}`：任务标题可由用户写入"
    for must in ("esc(r.title)", "esc(r.assignee)", "esc(r.task_id)"):
        assert must in js, f"转义没有真的接到 {must} 上"


def test_due_table_has_no_bare_task_interpolation() -> None:
    js = _js()
    start = js.find('$("#dueBody").innerHTML')
    assert start >= 0, "没找到截止日表格渲染段（页面结构变了，本用例需同步）"
    segment = js[start : start + 1600]
    bare = re.findall(r"\$\{r\.(?!\s*\))[a-z_]+\}", segment)
    assert not bare, f"截止日表格里还有裸插值：{bare[:5]}"
