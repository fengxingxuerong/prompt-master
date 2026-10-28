"""注入装束分类学覆盖表的守卫（PE-6）：装束清单漂移 ⇒ 文档必须先更；引用不认识的格 ⇒ 红。

承重的是第 1 条。`docs/injection-taxonomy.md` 的表按装束 id 逐行映射到 promptfoo 的
公开插件格——往 `pm/injection_pack.py` 加第 11 种装束而不更表，覆盖表就安静地说谎。
"""

from __future__ import annotations

import re
from pathlib import Path

from pm.injection_pack import TECHNIQUES

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "injection-taxonomy.md"

# promptfoo 红队插件 ID 白名单（抓取自官方插件文档 2026-10-29，注入相关族 + 本表引用的格）。
# 表里出现白名单外的 ID = 引用了一个没核实过的格，宁可红也不让文档编造分类学。
PROMPTFOO_IDS = frozenset(
    {
        "indirect-prompt-injection",
        "system-prompt-override",
        "special-token-injection",
        "ascii-smuggling",
        "cca",
        "hijacking",
        "prompt-extraction",
        "data-exfil",
        "agentic:memory-poisoning",
        "rag-poisoning",
        "mcp",
    }
)


def test_doc_covers_exactly_the_live_techniques() -> None:
    doc = DOC.read_text(encoding="utf-8")
    mapped = set(re.findall(r"^\|\s*\d+\s*\|\s*`([a-z_]+)`", doc, flags=re.MULTILINE))
    live = {t.id for t in TECHNIQUES}
    assert mapped == live, (
        f"覆盖表与装束清单漂了：表多出 {sorted(mapped - live)}，缺 {sorted(live - mapped)}——"
        "改了 TECHNIQUES 就必须同步 docs/injection-taxonomy.md 的第二节"
    )


def test_every_referenced_taxonomy_id_is_verified() -> None:
    doc = DOC.read_text(encoding="utf-8")
    live = {t.id for t in TECHNIQUES}
    backticked = set(re.findall(r"`([a-z][a-z0-9:_-]+)`", doc))
    taxonomy_refs = backticked - live
    unknown = taxonomy_refs - PROMPTFOO_IDS
    assert not unknown, f"文档引用了白名单外的分类学格：{sorted(unknown)}——先核实再写"


def test_table_counts_match_the_rows() -> None:
    doc = DOC.read_text(encoding="utf-8")
    rows = re.findall(r"^\|\s*\d+\s*\|", doc, flags=re.MULTILINE)
    assert len(rows) == len(TECHNIQUES), f"表体 {len(rows)} 行 ≠ 装束 {len(TECHNIQUES)} 条"
    assert f"{len(TECHNIQUES)}/{len(TECHNIQUES)} 有对等格" in doc, "第二节的计数行没跟上表体"


def test_taxonomy_axis_honesty_is_pinned() -> None:
    """不编造：T01–T08 名称未核实这件事必须留在文档里；哪天真核实了，改这条守卫。"""
    doc = DOC.read_text(encoding="utf-8")
    assert "T09" in doc and "不安全编码行为" in doc, "T 系唯一的逐字锚点丢了"
    assert "名称未核实" in doc, "必须保留「未核实」的留白，不许把图片里的分类写成已核实"
