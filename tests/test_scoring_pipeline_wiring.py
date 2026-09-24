"""判定式协议在**主管道**里的接线（`pm/nodes/judge.py`）。

纯映射已由 test_scoring_checklist.py 覆盖；这里只测接线：
`_evaluate_one` 有没有真的换协议、缓存会不会串、清单薄的时候走不走回头路。
这些是"被调函数全绿而入口坏"的高发位置——`scoring_mode()` 读了没人接、
或者接了但缓存键没分协议，测出来的都是旧路径。
"""

from __future__ import annotations

from typing import Any

import pm.nodes.judge as J
import pytest
from pm import prompts as P
from pm.cache import key_for_eval
from pm.schemas import ChecklistEvaluation, CheckVerdict, UnsourcedClaim

_PROMPT = "[任务]\n1. 给出趋势结论\n[约束]\n1. 每个结论必须附输入中的具体数值\n[输出格式]\n1. Markdown 表格"
_INPUT = "华东 120 万"
_OUTPUT = "| 区域 | 销售额 |\n|---|---|\n| 华东 | 120 万，环比 +22.4% |"
_RUN = {"test_case_index": 0, "test_input": _INPUT, "output": _OUTPUT}


def _checklist_ev(explain: bool) -> ChecklistEvaluation:
    def v(item: str) -> CheckVerdict:
        return CheckVerdict(item=item, satisfied=True, evidence="表格与数值均在输出中")

    items = [
        "[任务] 给出趋势结论",
        "[约束] 每个结论必须附输入中的具体数值",
        "[输出格式] Markdown 表格",
        "通用：原始需求点名要的那一件交付物是否真的给出（而不是推给「其他/信息不足」）",
        "通用：输入非空但字段不足时，是否只对缺失字段标注缺失、已有数据照常输出（而不是把整段判成无数据）",
        "通用：输出内部是否存在互相矛盾的说法（含与前文已声明缺失的内容冲突）",
    ]
    unsourced = (
        [UnsourcedClaim(claim="环比 22.4%", basis="(120-98)/98 由输入两值相除")] if explain else []
    )
    return ChecklistEvaluation(verdicts=[v(i) for i in items], unsourced=unsourced, quality_band=4)


@pytest.fixture()
def captured(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """把 `llm.structured_call` 换成假评委，记录它实际看到的 system / user 段。"""
    box: dict[str, Any] = {"calls": []}

    def fake_call(role: str, schema: Any, system: str, user: str) -> tuple[Any, dict[str, Any]]:
        box["calls"].append({"role": role, "schema": schema, "system": system, "user": user})
        if schema is ChecklistEvaluation:
            ev = _checklist_ev(explain=box.get("explain", False))
        else:
            ev = schema(
                dimension_scores={
                    "task_completion": 9,
                    "format_adherence": 9,
                    "constraint_compliance": 9,
                    "robustness": 9,
                    "quality": 9,
                },
                model_reported_score=9.0,
            )
        return ev, {"channel": "fake"}

    monkeypatch.setattr(J.llm, "structured_call", fake_call)
    monkeypatch.setattr(J, "_active_judges", lambda: ["evaluator"])
    monkeypatch.delenv("PM_SCORING_MODE", raising=False)
    return box


def _run_one(captured: dict[str, Any]) -> dict[str, Any]:
    ev, _calls, _hit = J._evaluate_one(
        dict(_RUN),
        task="分析销售数据",
        context="",
        prompt=_PROMPT,
        judges=["evaluator"],
        judge_spec="evaluator:m",
        q_warns=[],
        errors=[],
    )
    return ev


def test_checklist_mode_replaces_the_protocol(monkeypatch: Any, captured: dict[str, Any]) -> None:
    monkeypatch.setenv("PM_SCORING_MODE", "checklist")
    ev = _run_one(captured)
    call = captured["calls"][0]
    assert call["schema"] is ChecklistEvaluation, "换协议必须是换 schema，不只是换措辞"
    assert call["system"] == P.EVALUATOR_SYSTEM_CHECKLIST
    assert "<CHECKLIST>" in call["user"] and "[约束] 每个结论必须附输入中的具体数值" in call["user"]
    assert ev["scoring_mode"] == "checklist"
    assert ev["checklist_detail"]["n_items"] == 6


def test_unexplained_number_fails_the_case_even_when_every_rule_is_satisfied(
    monkeypatch: Any, captured: dict[str, Any]
) -> None:
    """这条是整套改动的理由：清单全判满足、格式全对，但 22.4% 没人解释 → 代码封顶 6.0 不通过。

    印象式下同一个用例实测评委给 8.6 并放行（人工 7.0），rubric 里那句"总分不得高于 6.0"
    写在提示词上、由评委自觉执行。
    """
    monkeypatch.setenv("PM_SCORING_MODE", "checklist")
    captured["explain"] = False
    ev = _run_one(captured)
    assert ev["passed"] is False
    assert ev["weighted_score"] == 6.0
    assert ev["checklist_detail"]["caps"] == [6.0]
    assert ev["checklist_detail"]["unsourced_unexplained"] == ["22.4"]


def test_explained_derivation_passes(monkeypatch: Any, captured: dict[str, Any]) -> None:
    monkeypatch.setenv("PM_SCORING_MODE", "checklist")
    captured["explain"] = True
    ev = _run_one(captured)
    assert ev["checklist_detail"]["caps"] == []
    assert ev["passed"] is True


def test_thin_prompt_falls_back_and_says_so(monkeypatch: Any, captured: dict[str, Any]) -> None:
    """基线臂常是裸需求：摊不出清单就不许走判定式（空桶会白送 9.5，把基线抬高）。"""
    monkeypatch.setenv("PM_SCORING_MODE", "checklist")
    ev, _c, _h = J._evaluate_one(
        dict(_RUN),
        task="分析销售数据",
        context="",
        prompt="帮我分析销售数据",
        judges=["evaluator"],
        judge_spec="evaluator:m",
        q_warns=[],
        errors=[],
    )
    assert captured["calls"][0]["schema"] is not ChecklistEvaluation
    assert ev["scoring_mode"] == "impression"


def test_cache_key_separates_the_two_protocols(monkeypatch: Any) -> None:
    """两协议共用缓存键的话，A/B 就成了拿旧结果比旧结果。

    同时反向守住一条：印象式（缺省）的 spec 必须与改动前逐字相同，
    否则这次改动会白白作废全部存量评估缓存。
    """
    monkeypatch.delenv("PM_SCORING_MODE", raising=False)
    impression = J._judge_spec(["evaluator"])
    assert impression.endswith(f"|rubric={P.rubric_stamp()}")
    monkeypatch.setenv("PM_SCORING_MODE", "checklist")
    checklist = J._judge_spec(["evaluator"])
    assert checklist != impression
    assert checklist.endswith(f"|rubric={P.checklist_rubric_stamp()}")
    # 清单文本本身也进键：同一条输出换了提示词、清单条目跟着变，判定不能复用
    a = key_for_eval(_PROMPT, _INPUT, _OUTPUT, checklist, extra="\n<CHECKLIST>x")
    b = key_for_eval(_PROMPT, _INPUT, _OUTPUT, checklist, extra="\n<CHECKLIST>y")
    assert a != b
