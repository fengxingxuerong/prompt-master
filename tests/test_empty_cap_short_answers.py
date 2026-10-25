"""空产出封顶不能误杀短答案，且必须封在与分数对应的那条采样上。

三条实测缺陷（2026-10-31 审计，都会把 Δ 算反或让报告自相矛盾）：

1. **短答案被判空壳**（`pm/scoring.py::has_empty_deliverable_profile`）：
   旧版 `len(stripped) < 8` 无条件返回 True ⇒ "42" / "YES" / "通过" / "3.5%"
   这些**正确且完整**的回答被硬封顶到 4.0。而 `pm/nodes/execute.py` 注释里的
   真实用例正是"只回答一个数字"。
2. **封顶查错采样**（`cap_empty_deliverables`）：旧版按用例号取输出，同用例第二次
   采样覆盖第一次；但折叠后的分数来自**代表采样**。k=2、只有 #1 是拒答时，
   8.9 分的完整报告会被压成 4.0（Δ 反向）；真正该封的那条则漏封。
3. **封顶不改 passed**：只改 weighted_score 时 `passed` 仍是评委原判，
   报告并排出现「通过用例 1」与「未达标」。
"""

from __future__ import annotations

from pm.nodes.judge import cap_empty_deliverables
from pm.scoring import EMPTY_DELIVERABLE_CAP, apply_empty_deliverable_cap


# ---------------------------------------------------------------- 短答案不该被封
def test_correct_short_answers_are_not_capped() -> None:
    """短答案本身不是"缺交付物"的证据 —— 这是最容易把好输出判死的误杀面。"""
    for good in ("42", "YES", "通过", "3.5%", "华东", "12,000", "—7"):
        assert apply_empty_deliverable_cap(9.5, good) == 9.5, f"「{good}」被误判为空产出"


def test_truly_empty_outputs_are_still_capped() -> None:
    """真空白 / 纯标点 / 只有标记词，仍然要封顶（这条闸不能被上面的修复拆掉）。"""
    for bad in ("", "   ", "...", "——", "数据缺失", "暂无", "请提供输入数据"):
        assert apply_empty_deliverable_cap(9.5, bad) == EMPTY_DELIVERABLE_CAP, (
            f"「{bad}」没被识别为空产出"
        )


def test_long_empty_shell_still_capped() -> None:
    """长空壳（模板+占位标记、无实质内容）依旧封顶：原判据保留。"""
    shell = "数据缺失\n暂无可分析内容\n请提供销售数据后再试\n"
    assert apply_empty_deliverable_cap(9.0, shell) == EMPTY_DELIVERABLE_CAP


def test_marker_plus_substance_is_not_capped() -> None:
    """标记词是**辅证**：真有交付物时（哪怕逐条标注缺失）不该封顶（§三十·九 的教训）。"""
    body = (
        "## 结论\n华东区域销售额 120 万元，同比增长 8.4%。\n"
        "## 异常点\n- 华北字段「退款原因」数据缺失，其余照常输出。\n"
        "## 行动建议\n补录退款原因后重算退货率。\n"
    )
    assert apply_empty_deliverable_cap(8.9, body) == 8.9


# ------------------------------------------------- 封顶要落在产生分数的那条采样上
def _ev(idx: int, sample: int, score: float, passed: bool = True) -> dict:
    return {
        "test_case_index": idx,
        "sample_index": sample,
        "weighted_score": score,
        "passed": passed,
    }


def _run(idx: int, sample: int, output: str) -> dict:
    return {"test_case_index": idx, "sample_index": sample, "output": output}


def test_cap_uses_the_sample_that_produced_the_score() -> None:
    """代表采样是 #0（好输出 8.9 分）时，#1 的拒答不能把这条压成 4.0。"""
    evaluations = [_ev(0, 0, 8.9)]
    runs = [
        _run(0, 0, "## 结论\n华东 120 万，同比 8.4%。\n## 建议\n补录字段后重算。"),
        _run(0, 1, "数据缺失"),
    ]
    assert cap_empty_deliverables(evaluations, runs) == 0
    assert evaluations[0]["weighted_score"] == 8.9


def test_cap_fires_when_the_scored_sample_is_the_empty_one() -> None:
    """代表采样是 #1（拒答）时，必须按 #1 封顶 —— 旧版会被 #0 的好输出挡住而漏封。"""
    evaluations = [_ev(0, 1, 8.9)]
    runs = [
        _run(0, 0, "## 结论\n华东 120 万，同比 8.4%。\n## 建议\n补录字段后重算。"),
        _run(0, 1, "暂无"),
    ]
    assert cap_empty_deliverables(evaluations, runs) == 1
    assert evaluations[0]["weighted_score"] == EMPTY_DELIVERABLE_CAP


def test_cap_syncs_passed_flag() -> None:
    """封顶后 passed 必须跟着变，否则报告会出现「通过用例 1」与「未达标」并排。"""
    evaluations = [_ev(0, 0, 8.9, passed=True)]
    runs = [_run(0, 0, "数据缺失")]
    cap_empty_deliverables(evaluations, runs)
    assert evaluations[0]["weighted_score"] == EMPTY_DELIVERABLE_CAP
    assert evaluations[0]["passed"] is False


def test_cap_falls_back_to_case_level_when_sample_missing() -> None:
    """老归档没有 sample_index：退回用例级取值，不能因为查不到就静默不封。"""
    evaluations = [{"test_case_index": 0, "weighted_score": 9.0, "passed": True}]
    runs = [{"test_case_index": 0, "output": "暂无"}]
    assert cap_empty_deliverables(evaluations, runs) == 1


def test_cap_ignores_entries_without_numeric_score() -> None:
    """分数缺失/非数值的评估不能被这段改写（也不能炸）。"""
    evaluations = [{"test_case_index": 0, "weighted_score": None}]
    runs = [_run(0, 0, "暂无")]
    assert cap_empty_deliverables(evaluations, runs) == 0


def test_baseline_arm_uses_the_same_cap() -> None:
    """基线臂必须与主路同口径封顶 —— 否则 Δ 会把"两臂一样烂"读成"优化没用"。

    回归防线（2026-10-31）：封顶此前只接在 `judge.py`，基线臂（`baseline.py`）
    拿的是评委裸分。两臂都产出空壳时主路 4.0 / 基线 8.9 ⇒ Δ≈−5，
    报告据此写"优化未带来优势"，而真实情况是两臂一样烂。
    这里用静态判据（这段要跑真模型才够得着）+ 共享实现的存在性。
    """
    import inspect

    from pm.nodes import baseline, judge

    assert "cap_empty_deliverables" in inspect.getsource(baseline.baseline_node), (
        "基线臂没有走空产出封顶：Δ 两臂不同口径"
    )
    assert judge.cap_empty_deliverables is cap_empty_deliverables, "两臂应共用同一个实现"
