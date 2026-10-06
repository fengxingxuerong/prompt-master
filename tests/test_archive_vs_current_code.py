"""钉住 §三十·十 的重算结论：归档 avg 与当前代码现算 avg 的差。

为什么要有这条：§三十·七/八 那两节的结论建立在归档 `aggregate.avg_score` 上，
而归档的 aggregate 出自**空产出封顶之前**的裸分（封顶接线在那几条 run 之后才修）。
于是"新口径下第一次 C1∧C2"、"产品闸会翻转"两个结论都建立在错的数上。

本文件**不**试图重算所有统计口径（那需要真调用），只钉三件可复核的事：

1. **封顶接线的时间线**：那几条 run 的 mtime 早于 `1df7be9`；
2. **同源判据**：当前代码下 `from_evaluations` 的 avg / min / max 必须同源
   ——用一批 `weighted_score` 相同的 evaluations，若三个数不同就是回归；
3. **C1∧C2 归零**：按当前代码对真实归档重算，同时满足 C1∧C2 的轮必须是 0。
   这条最关键——它是"上一节的结论错了"的直接反证。
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path

import pytest
from pm.cli.history import _load_run_history
from pm.schemas import AggregateScore, EvaluationResult

LOGS = Path(__file__).resolve().parents[1] / "logs"
PASS_THRESHOLD = 8.0
CAP_FIX = datetime.datetime(2026, 10, 5, 15, 43)  # 1df7be9 落地时间

# 归档里 aggregate.avg 与当前代码现算 avg 不一致的臂（§三十·十 实测 10/16）
CAP_AFFECTED = (
    "923d7f3db781",
    "f44932ebd393",
    "3a68c8ed48bc",
    "9950147e2e40",
    "34a205ecc9c3",
    "2d306d72feec",
    "a43e44adcc9f",
    "87d9c16e0e48",
    "c4ccfd18c72d",
    "54b28925695a",
)


def _load(run_id: str) -> dict | None:
    p = LOGS / f"run_{run_id}.json"
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def _reaggregate(d: dict) -> dict:
    evs = [EvaluationResult.model_validate(e) for e in d["evaluations"]]
    return AggregateScore.from_evaluations(
        evs, n_expected=int(d.get("n_test_cases") or len(evs))
    ).model_dump()


def test_the_cap_affected_arms_predate_the_fix() -> None:
    """那 10 条臂跑在封顶接线修复**之前**——这是差值的来源。"""
    seen = 0
    for rid in CAP_AFFECTED:
        p = LOGS / f"run_{rid}.json"
        if not p.exists():
            continue
        seen += 1
        mtime = datetime.datetime.fromtimestamp(p.stat().st_mtime)
        assert mtime < CAP_FIX, (
            f"{rid}（{mtime}）已不早于封顶修复（{CAP_FIX}），"
            "它在当前代码下重跑应该会与归档一致——请重判这条是否还该列在 CAP_AFFECTED"
        )
    assert seen or not LOGS.exists(), "归档目录在但一条都没读到"


@pytest.mark.parametrize("run_id", CAP_AFFECTED)
def test_archive_avg_differs_from_current_code(run_id: str) -> None:
    """归档 avg ≠ 当前代码现算 avg —— 钉住"别拿归档当读数"这件事。"""
    d = _load(run_id)
    if d is None:
        pytest.skip("归档不在本机")
    cur = _reaggregate(d)
    assert float(cur["avg_score"]) != float(d["aggregate"]["avg_score"]), (
        f"{run_id}：归档 avg 与现算已经一致了（都是 {cur['avg_score']}）。"
        "要么归档被重跑过，要么代码回退了——§三十·十 的结论需要重算"
    )


def test_min_max_avg_are_same_source_now() -> None:
    """当前代码下 avg / min / max 必须同源（§十七·十 那条回归的加强版）。

    构造一批 `weighted_score` 相同的 evaluations：三个数必须一致。
    封顶接线坏掉时曾出现 avg 与 min/max 不同源，所以这条防的是那类回归。
    """
    evs = [
        EvaluationResult.model_validate(
            {
                "test_case_index": i,
                "weighted_score": 4.0,
                "score_spread": 0.1,
                "n_samples": 2,
                "passed": False,
                "issues": [],
                "suggestions": [],
                "model_reported_score": 4.0,
                "sample_scores": [4.0, 4.0],
                "dimension_scores": {
                    "quality": 4.0,
                    "task_completion": 4.0,
                    "format_adherence": 4.0,
                    "constraint_compliance": 4.0,
                    "robustness": 4.0,
                },
            }
        )
        for i in range(3)
    ]
    agg = AggregateScore.from_evaluations(evs, n_expected=3).model_dump()
    assert agg["avg_score"] == agg["min_score"] == agg["max_score"] == 4.0


def test_no_arm_meets_c1_and_c2_under_current_code() -> None:
    """C1∧C2 = 0 —— 直接反证 §三十·七 那个"首次通过"。

    这条会随真实运行变化（将来可能有臂真的达标）。它现在的作用是：
    在有人引用 §三十·七 的旧结论时，让引用者先撞上这条红。
    """
    rows = _load_run_history(last=10_000, include_demo=False)
    hits = []
    for r in rows:
        d = _load(r["run_id"])
        if d is None or not d.get("evaluations"):
            continue
        base = (d.get("baseline_aggregate") or {}).get("avg_score")
        if not isinstance(base, (int, float)) or not r["trusted"]:
            continue
        cur = _reaggregate(d)
        band = float(cur["noise"])
        delta = float(cur["avg_score"]) - float(base)
        ci = max(1.0, float(cur["avg_score"]) - 1.96 * float(cur["sem"]) - 0.5 * band)
        if ci >= PASS_THRESHOLD and delta > band:
            hits.append(r["run_id"])
    assert hits == [], (
        f"按当前代码口径这些臂满足 C1∧C2：{hits}。"
        "若确有臂达标，请更新 docs/evaluation.md §三十·十 并核对口径，"
        "不要只改这条断言"
    )
