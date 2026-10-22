"""样例交付报告的守卫（2026-10-22）。

`docs/sample-report.md` 是给"还没花钱的人"看的**形状示例**：它由假后端产出，
分节结构与真实报告一致、读数全是桩。两条风险各配一条守卫：

1. 说明被重生成冲掉 ⇒ 桩读数看起来像真实读数（**误导**）；
2. 报告结构变了而样例没更新 ⇒ 读者按样例理解产品，读到的是过期形状。
"""

from __future__ import annotations

from pathlib import Path

SAMPLE = Path(__file__).resolve().parents[1] / "docs" / "sample-report.md"


def test_sample_report_keeps_its_caveat_header() -> None:
    """开头那段'"形状示例、不是真实读数"不许被覆盖掉。"""
    head = SAMPLE.read_text(encoding="utf-8")[:600]
    assert "假后端" in head, "样例开头必须写明它是假后端产出的"
    assert "不是真实读数" in head, "样例开头必须写明读数不代表效果"
    assert "重生成" in head, "要给重新生成的命令，否则下一个人只能手工拼"


def test_sample_report_keeps_the_core_sections() -> None:
    """结构漂了（少节 / 被截断）要在这里红，而不是让读者先发现。"""
    text = SAMPLE.read_text(encoding="utf-8")
    for section in (
        "## 评分总览",
        "## 置信度与采样噪声",
        "## 与基线对比",
        "## 成对盲评",
        "## 版本迭代",
        "## 最终提示词",
    ):
        assert section in text, f"样例报告少了这一节：{section}（重生成后请核对结构）"
