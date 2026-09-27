"""空产出告警的回归：空壳/纯拒答输出被判可上线时，报告必须点名（提醒不否决）。

背景（2026-09-27 首轮人工锚点校准）：两条空壳日报（模板+占位标记，无实质交付物）
被评委打 7.0 / 9.85，人工分 2/2；AI 参考分也给 7.0/7.5——三个 AI 读者全军覆没，
只有产品所有者的判断把"拒答得体"和"可上线"分开。告警就是给这个裂缝装哨兵。
"""

from __future__ import annotations

from pm.calibration import analyze, render_report
from pm.scoring import has_empty_deliverable_profile

_SKELETON = "今日无可用工作记录，请提供后再生成\n\n工作日报\n【今日完成】\n无\n【明日计划】\n无"
_MISSING_TPL = "📋 日报\n✅ 今日完成\n• 数据缺失\n📅 明日计划\n• 数据缺失\n⚠️ 问题/风险\n• 数据缺失"
_GOOD = (
    "## 销售数据分析报告\n### 一、数据概览\n- 华东120万、华南98万、华北45万、西南12万\n"
    "### 二、趋势结论\n横截面数据无法判定时间趋势\n### 三、异常点\n西南低于均值50%阈值\n"
    "### 四、数据缺失说明\n无缺失\n### 五、建议\n排查西南区域渠道覆盖与客户开发情况"
)


def test_skeleton_profiles_are_detected() -> None:
    # 标记按出现次数累计：「数据缺失」×3 的模板只含一种标记，去重就会漏
    assert has_empty_deliverable_profile(_SKELETON)
    assert has_empty_deliverable_profile(_MISSING_TPL)
    assert has_empty_deliverable_profile("")  # 空白输出更是空产出


def test_substantive_report_is_not_flagged() -> None:
    # 真分析里出现一次「无法判定时间趋势」不构成空产出画像——告警宁可漏报不误伤
    assert not has_empty_deliverable_profile(_GOOD)


def test_analyze_flags_high_score_on_skeleton_only() -> None:
    samples = [
        {"id": "sk", "human_score": 2.0, "test_output": _SKELETON},
        {"id": "ok", "human_score": 8.0, "test_output": _GOOD},
        {"id": "sk_low", "human_score": 2.0, "test_output": _MISSING_TPL},
    ]
    analysis = analyze(samples, [9.5, 8.0, 4.0])
    # 告的是"高分给空壳"这个矛盾：低分的空壳不进名单
    assert analysis["empty_high"] == ["sk"]
    text = render_report("evaluator", analysis)
    assert "空产出获高分" in text
    assert "sk" in text


def test_code_cap_binds_where_prompt_could_not() -> None:
    """代码硬封顶:八轮实测评委两版条款都管不住空壳日报(8.85~9.85),代码管得住。"""
    from pm.scoring import EMPTY_DELIVERABLE_CAP, apply_empty_deliverable_cap

    assert apply_empty_deliverable_cap(9.85, _SKELETON) == EMPTY_DELIVERABLE_CAP
    assert apply_empty_deliverable_cap(9.85, _MISSING_TPL) == EMPTY_DELIVERABLE_CAP
    assert apply_empty_deliverable_cap(8.0, _GOOD) == 8.0  # 真分析不挨刀
    assert apply_empty_deliverable_cap(2.0, _SKELETON) == 2.0  # 本就更低不抬升
