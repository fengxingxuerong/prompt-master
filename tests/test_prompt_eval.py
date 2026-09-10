"""节点提示词回归评测骨架（提示词#2）的 pytest 集成。

`eval_prompts.py` 的离线模式是零成本结构契约校验，这里让它进入常规回归：
任何人改 `pm/prompts.py` 破坏了模板结构（占位符、安全约束、专项规则块），
pytest 直接红，不用等到真实跑才发现。真实模式（--live）花真钱，保持手动执行。
"""

from __future__ import annotations

import inspect

import eval_prompts


def test_offline_prompt_eval_all_pass():
    """离线结构契约：全部用例必须通过（模板改动破坏结构时立刻失败）。"""
    cases = eval_prompts.load_cases()
    failures: list[str] = []
    for node in sorted(eval_prompts.TEMPLATES):
        for case in cases.get(node, []):
            issues = eval_prompts.check_structure(node, case)
            if issues:
                failures.append(f"{node}/{case['name']}: {issues}")
    assert not failures, "节点提示词结构契约被破坏：\n" + "\n".join(failures)


def test_eval_cases_cover_all_core_nodes():
    """用例集必须覆盖全部 5 个可评测节点（新提示词节点接入时同步补用例）。"""
    cases = eval_prompts.load_cases()
    for node in eval_prompts.TEMPLATES:
        assert cases.get(node), f"用例集缺少节点 {node} 的用例"


def test_live_check_reviser_growth_rule():
    """真实校验器的修订净增量规则存在且阈值正确（30% + 绝对下限 200 字）。

    --live 首轮验证发现纯相对阈值对短基准过严（65 字基准只有约 20 字余量），
    已改为 max(200 字, 30%)：短基准有合理补全空间，长基准仍守 30%。
    """
    # 不真正调用 LLM：只验证 check_live 的增量判定逻辑片段可独立复核
    src = inspect.getsource(eval_prompts.check_live)
    assert "REVISER_GROWTH_ABS_FLOOR" in src and "净增量" in src
    assert eval_prompts.REVISER_GROWTH_RATIO == 0.3
    assert eval_prompts.REVISER_GROWTH_ABS_FLOOR == 200


def test_growth_threshold_short_baseline():
    """短基准（65 字）的净增量余量应为绝对下限 200 字，而不是 30%（约 20 字）。"""
    prev = "a" * 65
    margin = max(
        eval_prompts.REVISER_GROWTH_ABS_FLOOR, len(prev) * eval_prompts.REVISER_GROWTH_RATIO
    )
    assert margin == 200
    # 265 字输出（旧逻辑必然超限）在新阈值下放行
    assert len(prev) + margin == 265
    # 长基准（1000 字）余量回到 30% = 300 字
    margin_long = max(
        eval_prompts.REVISER_GROWTH_ABS_FLOOR, 1000 * eval_prompts.REVISER_GROWTH_RATIO
    )
    assert margin_long == 300
