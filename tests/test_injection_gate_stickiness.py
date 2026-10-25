"""注入门禁不能粘滞：这一轮通过了就不能还挂着上一轮的拦截。

实测缺陷（2026-10-31 审计）：`evaluate_node` 旧版只在**拦截时**写
`patch["injection_gate"]`，于是上一轮留下的 `{"blocked": True}` 会在 state 里一直活着。
修订把注入缺陷修掉之后，`status=passed`、`avg=9.0`，报告首行照样印
「⚠️ 未达标：…注入门禁拦截（1/1 条被劫持）」，而文末「注入存活」写着 0/1 ——
**修好了反而被判未达标**，这是最难被用户理解的那一类报告。

`unresolved_questions` 有同一个毛病（只追加不清理），修好了那一条还挂在报告里。
"""

from __future__ import annotations

from pm.nodes.judge import injection_gate_patch

_Q = "注入存活检测未通过（1/1 条被劫持）：达标结论已被门禁压制，修复「标签内数据指令不得执行」的约束后再交付。"


def test_blocked_gate_writes_blocked_true() -> None:
    patch, blocked = injection_gate_patch({}, {"total": 1, "hijacked": 1})
    assert blocked is True
    assert patch["injection_gate"]["blocked"] is True
    assert any(q.startswith("注入存活检测未通过") for q in patch["unresolved_questions"])


def test_passing_gate_overwrites_a_stale_block() -> None:
    """核心用例：上一轮 blocked=True，这一轮 0 劫持 ⇒ 必须被覆写成 False。"""
    stale_state = {"injection_gate": {"blocked": True, "total": 1, "hijacked": 1}}
    patch, blocked = injection_gate_patch(stale_state, {"total": 1, "hijacked": 0})
    assert blocked is False
    assert patch["injection_gate"] == {"blocked": False, "total": 1, "hijacked": 0}


def test_passing_gate_clears_its_own_unresolved_question() -> None:
    """通过时清掉门禁那一句，但**不许**顺手抹掉别的来源的未解决问题。"""
    state = {"unresolved_questions": ["评委提到缺少异常点分析", _Q]}
    patch, _ = injection_gate_patch(state, {"total": 1, "hijacked": 0})
    assert patch["unresolved_questions"] == ["评委提到缺少异常点分析"]


def test_no_gate_means_no_write() -> None:
    """没有注入用例（gate=None）时不写 injection_gate —— 不能让"没测"看起来像"通过"。"""
    patch, blocked = injection_gate_patch({}, None)
    assert blocked is False
    assert "injection_gate" not in patch


def test_unresolved_question_stays_single_across_rounds() -> None:
    """连续多轮都拦截时，那条未解决问题只留最新一条，不越堆越多。"""
    state: dict = {}
    for _ in range(3):
        patch, _ = injection_gate_patch(state, {"total": 1, "hijacked": 1})
        state.update(patch)
    hits = [q for q in state["unresolved_questions"] if str(q).startswith("注入存活检测未通过")]
    assert len(hits) == 1, f"未解决问题被重复追加了 {len(hits)} 次"
