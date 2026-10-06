"""评委席不完整时，报告必须说出来——现在它只在 errors 里躺着。

真实事故（§三十·十一·二，`run_13f9a99096e9`）：`evaluator_b` 5 次超时，
4 条用例里 2 条只剩单评委。那轮的 `avg=6.86` / 基线 `7.03` 进了归档、
进了报告表格，**正文从头到尾没有任何一句说"这轮是半个评委席跑的"**。

读者看到的是一张和平时一模一样的分数表。会读的人去翻 `judge_scores`，
不会读的人直接当成双评委共识——而 `report.py:529` 那段注释明写
"读者会以为表里每个数都是两位评委的共识"，**它只覆盖了仲裁一种形态**。

现有那条披露的条件是 `n_arb or n_cons`（仲裁成功 / 仲裁失败回退），
**判不到"评委直接缺席"**——缺席的用例 `judge` 字段是 `"evaluator"`，
既不是 `arbiter` 也不是 `conservative`。

判据：一条用例实际打分评委数 < 配置的评委数 ⇒ 该轮评委席降级。
读数：从归档现算，用真实 run，不用构造夹具。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pm.report import _judge_source_lines

RUN_13 = "13f9a99096e9"  # 双评委轮：2/4 条 evaluator_b 缺席
RUN_88 = "88dcfd4d9c29"  # 单评委轮：本来就该是单评委，不该报降级


def _load(run_id: str) -> dict:
    p = Path(__file__).resolve().parents[1] / "logs" / f"run_{run_id}.json"
    if not p.exists():
        pytest.skip(f"归档不在本机：{p.name}")
    return json.loads(p.read_text(encoding="utf-8"))


def _judge_n(d: dict) -> int:
    """从归档读这轮配了几个评委（judge 名单写进 state 的概率低，改读 judge_spec）。"""
    spec = d.get("judge_spec") or ""
    if "+" in spec:
        return spec.split("|")[0].count("+") + 1
    return (
        2
        if d.get("evaluations")
        and any(len(e.get("judge_scores") or {}) >= 2 for e in d["evaluations"])
        else 1
    )


def _state(evals: list[dict], panel: int = 2, errors: list[str] | None = None) -> dict:
    """按真实 state 的形状造输入。

    必须带 `judge_health.panel_size`：披露的分母取**配置值**而不是"见过几个评委"
    ——倒推的话，全员缺席那轮会得到 0，判不出降级。
    """
    seats = ["evaluator", "evaluator_b", "arbiter"][: panel + 1]
    st: dict = {
        "evaluations": evals,
        "judge_health": {
            "models": {s: f"m@{i}" for i, s in enumerate(seats)},
            "homogeneous": False,
            "note": "",
            "panel_size": panel,
        },
    }
    if errors is not None:
        st["errors"] = errors
    return st


def test_degraded_panel_is_disclosed() -> None:
    """核心回归：半个评委席必须被披露。"""

    d = _load(RUN_13)
    evals = d["evaluations"]
    errors = d.get("errors") or []
    failed = [e for e in errors if "evaluate#" in str(e)]
    assert failed, (
        "归档变了：run_13f9a99096e9 那轮本来有 5 条 evaluator_b 超时，"
        "若这里为空说明我记错了，需要重新判断"
    )
    assert any("case#2" in str(e) or "evaluate#2" in str(e) for e in failed) or any(
        "evaluate#2[" in str(e) for e in failed
    ), "case#2 是我记得的那条"

    text = "\n".join(_judge_source_lines(_state(evals, errors=errors)))
    assert "评委席降级" in text and "evaluator_b" in text, (
        f"披露文本里没有说评委席降级，实际输出：\n{text}"
    )


def test_no_panel_size_means_no_verdict() -> None:
    """没有 panel_size 就无法判定降级——必须沉默，不许猜。

    老归档（`judge_health` 里还没有这个键）走的就是这条路。
    """

    d = _load(RUN_13)
    st = _state(d["evaluations"])
    st["judge_health"].pop("panel_size")
    text = "\n".join(_judge_source_lines(st))
    assert "评委席降级" not in text, f"缺分母时仍下了结论：\n{text}"


def test_full_panel_is_not_disclosed_as_degraded() -> None:
    """评委齐全时不许报降级（否则永远在报警，读者会麻木）。"""

    evals = [
        {
            "test_case_index": 0,
            "judge": "merged",
            "judge_scores": {"evaluator": 8.0, "evaluator_b": 7.0},
        },
        {
            "test_case_index": 1,
            "judge": "merged",
            "judge_scores": {"evaluator": 9.0, "evaluator_b": 8.5},
        },
    ]
    text = "\n".join(_judge_source_lines(_state(evals)))
    assert "缺席" not in text and "降级" not in text, f"误报了降级：\n{text}"


def test_single_judge_run_is_not_a_degraded_panel() -> None:
    """单评委模式（PM_JUDGES=1）不是降级——那是配置，不是故障。

    关键：run_88 那轮**自己也带 errors**（case#3 超时），所以这条同时证明
    "有 errors" 本身不足以判降级——真正把两轮分开的是 panel_size 与 errors 的组合。
    """

    d = _load(RUN_88)
    errors = d.get("errors") or []
    assert errors, "run_88 自己就有超时错误；若为空说明归档变了"
    text = "\n".join(_judge_source_lines(_state(d["evaluations"], panel=1, errors=errors)))
    assert "降级" not in text, f"PM_JUDGES=1 的轮次被当成评委缺席了：\n{text}"


def test_judge_scores_alone_cannot_decide() -> None:
    """钉住那条两义陷阱：`judge_scores` 为空既可能是配置，也可能是失败。

    两种形态在归档里长得一模一样（实测 run_13 的 case#2/#3 与 run_88 的四条
    都是 `len(judge_scores)==0`），所以判据只能取失败证据 + 配置分母的组合。
    """

    evals_empty = [{"test_case_index": 0, "judge": "evaluator", "judge_scores": {}}]

    # 有失败证据 + 双评委配置 ⇒ 报降级
    with_err = _state(evals_empty, panel=2, errors=["evaluate#0[evaluator_b]: Request timed out."])
    assert "降级" in "\n".join(_judge_source_lines(with_err))

    # 无失败证据 + 单评委配置 ⇒ 不报
    no_err = _state(evals_empty, panel=1, errors=[])
    assert "降级" not in "\n".join(_judge_source_lines(no_err))
