"""修订器必须把 `[示例]` 当资产，并且给"精简"设下限。

2026-11-26 真实 run `e046e3d94085` 实测（83 次调用、双族评委）：

- v0：1411 字，**含 `[示例]` 少样本示范** → 均分 6.79，constraint_compliance 7.0；
- v1：1039 字，示例被整段删除 → 均分 5.29，constraint_compliance 4.0、robustness 4.0；
- v2：1398 字，只把约束写得更长，**示例仍未恢复** → 仍 5.29，平台期 early_stop。

根因不是评委：`REVISER_SYSTEM` 的结构清单只列了「角色 / 背景 / 任务 / 约束 / 输出格式」，
示例不在其中，于是它被当成冗余；而规则 7 只约束"增量 ≤30%"，没有减量下限，
一次 26% 的缩水就足以把最有价值的一段删掉，评分掉 1.5 分。

这三条用例把判据钉在提示词上：结构清单必须点名示例、删示例必须被明文禁止、
精简必须有下限。改文案可以，但别把这三条约束悄悄删掉。
"""

from __future__ import annotations

from pm.prompts import REVISER_SYSTEM


def test_structure_checklist_names_the_example_block():
    """示例必须出现在"保持结构"的白名单里，否则修订器会当它是冗余。"""
    assert "角色 / 背景 / 任务 / 约束 / 输出格式 / 示例" in REVISER_SYSTEM


def test_deleting_the_example_block_is_explicitly_forbidden():
    assert "不得删除整段" in REVISER_SYSTEM
    assert "原样保留" in REVISER_SYSTEM


def test_shrink_has_a_floor_not_only_a_ceiling():
    """只写上限不管下限，等于允许一轮砍掉四分之一正文。"""
    assert "净减少不得超过 20%" in REVISER_SYSTEM
    assert "净增量不超过原文的 30%" in REVISER_SYSTEM
