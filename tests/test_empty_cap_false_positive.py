"""空产出硬封顶的误伤：任务要求标注「数据缺失」时，照要求做反而被封到 4.0。

背景（2026-10-05 §三十·九 实测）：`34a205ecc9c3` 的优化臂 avg 归档写 8.62、
落盘 weighted_score 却全是 4.0——差值来自 `apply_empty_deliverable_cap`。
逐条查画像命中原因，四条**全部**只因为 `数据缺失` 这个标记词出现 ≥2 次。

而那批任务原文就写着「数据不足或缺失时必须显式标注『数据缺失』」——
**标记词与任务要求撞车**，于是合规输出被判成空壳。

全量归档统计：命中画像 160 条，其中 `数据缺失` **单独**致命中 126 条（79%）；
评委给了 ≥8.0 却被封到 4.0 的共 **50 条**。

判据与读数分开：
- 判据：一份**照任务要求标注缺失**、且交付了实质内容（分析/清单/结论）的报告，
  不是空壳。它命中标记词，恰恰证明它在响应任务。
- 读数：`2d306d72feec` case#1，1022 字，三章节齐全、5 条异常点带行动建议、
  明确标注缺失；评委 dimension 均分 8.6；被封到 4.0。

⚠️ `has_empty_deliverable_profile` 的设计注释写着「刻意不进判定链——告警误报的
代价（多看一眼）远低于判错一条真『无缺失』报告的代价」，但 `judge.py` 把它接进了
`weighted_score`，而那是 `avg`/`n_passed`/`passed` 的唯一输入。**意图与实现相反。**

修法：把「标记词计数」从判空产出的主判据降为**辅证**——要求输出同时缺少实质内容。
`数据缺失` 在正文里可以出现任意次；只有当输出**没有可交付的实质内容**时才算空壳。
"""

from __future__ import annotations

import pytest
from pm.scoring import (
    EMPTY_DELIVERABLE_CAP,
    apply_empty_deliverable_cap,
    has_empty_deliverable_profile,
)

# 一份照任务要求做的合规报告：逐条标注「数据缺失」，并给出异常点与建议。
COMPLIANT = """## 趋势结论

| 区域 | 销售额 | 状态 |
|------|--------|------|
| 华东 | null | 数据缺失 |
| 华南 | 98 万 | 有数据 |

- 华东区域数据完全缺失，无法进行区域间对比或趋势判断。

## 异常点列表

| 序号 | 异常类型 | 具体描述 | 建议行动 |
|------|----------|----------|----------|
| 1 | 数据缺失 | 华东区域销售额为 null | 回溯数据源确认是否采集失败 |
| 2 | 数据缺失 | 产品字段完全缺失 | 补全产品维度字段 |

## 数据完整性说明

| 问题项 | 状态 |
|--------|------|
| 华东销售额 | 数据缺失 |
| 产品字段 | 数据缺失 |
"""


def test_compliant_report_is_not_flagged_as_empty() -> None:
    """核心回归：合规报告不许命中空产出画像。

    这条在修复前是**红**的（`数据缺失` 出现 5 次 ≥ 阈值 2）。
    """
    assert not has_empty_deliverable_profile(COMPLIANT), (
        "照任务要求标注「数据缺失」的合规报告被判成空壳——"
        "标记词与任务要求撞车（§三十·九 实测 50 条高分输出被封）"
    )


def test_compliant_report_keeps_its_score() -> None:
    """封顶不得压在合规报告上——那是判定链上唯一能改分的一处。"""
    assert apply_empty_deliverable_cap(8.6, COMPLIANT) == 8.6


def test_real_skeleton_is_still_capped() -> None:
    """对照：真·空壳仍然必须封顶，否则这轮修复就把防线拆了。"""
    skeleton = "今日无可用工作记录，请提供后再生成\n\n工作日报\n【今日完成】\n无\n【明日计划】\n无"
    assert has_empty_deliverable_profile(skeleton)
    assert apply_empty_deliverable_cap(9.85, skeleton) == EMPTY_DELIVERABLE_CAP


def test_real_missing_template_is_still_capped() -> None:
    """对照：只有占位标记的模板（无任何实质交付物）仍然被封。"""
    missing_tpl = (
        "📋 日报\n✅ 今日完成\n• 数据缺失\n📅 明日计划\n• 数据缺失\n⚠️ 问题/风险\n• 数据缺失"
    )
    assert has_empty_deliverable_profile(missing_tpl)
    assert apply_empty_deliverable_cap(9.85, missing_tpl) == EMPTY_DELIVERABLE_CAP


def test_blank_is_still_empty() -> None:
    assert has_empty_deliverable_profile("")
    assert has_empty_deliverable_profile("   ")


@pytest.mark.parametrize(
    ("text", "why"),
    [
        ("今日无可用工作记录，请提供后再生成\n\n工作日报\n【今日完成】\n无", "空壳日报骨架"),
        ("## 分析\n数据缺失\n暂无\n请提供更多材料", "全是标记词、无结论"),
    ],
)
def test_marker_only_reports_still_flagged(text: str, why: str) -> None:
    """只要没有实质交付物，标记词仍应生效（辅证地位，不是取消）。"""
    assert has_empty_deliverable_profile(text), why


def test_low_score_is_not_raised_by_the_cap() -> None:
    assert apply_empty_deliverable_cap(2.0, "今日无可用工作记录，请提供后再生成\n\n工作日报") == 2.0
