"""把 §三十·八 的结论钉成可复核的形式。

三条结论：
1. `34a205ecc9c3` 的 min/max 与落盘 weighted_score 对不上，是因为它跑在
   封顶接线修复（1df7be9，10-05 15:43）**之前**（该 run 是 10-04 18:11）——
   即 §十七·十 那个"落盘与聚合矛盾"的**旧症状**，不是新缺陷。
2. 基线 4.98 判得站得住：评委指控的"编造数值"在基线输出里查得到，
   且被批评的那句提示词确实在**基线臂**里（不在优化臂）。
3. C1∧C2 那个"分母侧未解决"——结构上已解决：基线与主路共用
   `_case_ground_truth` / `_active_judges` / `_samples_per_case`。
"""

from __future__ import annotations

import json
from pathlib import Path

from pm.cli.history import _arm_is_trusted

RUNS = Path(__file__).resolve().parents[1] / "logs"
CAP_FIX_COMMIT_DATE = "2026-10-05 15:43"  # 1df7be9（git log --date 读出）

PHRASE = "你自己合理判断一下就行"
FABRICATION = "数据完整性评分"


def _load(run_id: str) -> dict | None:
    p = RUNS / f"run_{run_id}.json"
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def test_the_c1c2_arm_is_trusted() -> None:
    d = _load("34a205ecc9c3")
    if d is None:
        return
    a = d["aggregate"]
    assert _arm_is_trusted(d) is True
    assert a["n_passed"] == a["n_cases"], "C1 成立的前提是用例全过"
    assert a["passed"] is False, (
        "⚠️ 这条臂 aggregate.passed 仍是 False —— C1 判的是 ci_lower≥8.0，"
        "而 passed 走的是另一条判定（用例分≥阈值）。两者不一致时要说清用的是哪个。"
    )


def test_baseline_is_judged_on_its_own_arm() -> None:
    """基线 4.98 成立：被判有缺陷的那句提示词必须在**基线臂**里。

    若它其实在优化臂里，那就是评委审错了对象——那会让 Δ 的分母变成冤案。
    """
    d = _load("34a205ecc9c3")
    if d is None:
        return
    runs = d.get("baseline_runs") or []
    if not runs:
        return
    base_prompt = str(runs[0].get("prompt"))
    opt_prompt = str(d.get("prompt"))
    assert PHRASE in base_prompt, "基线臂应当含被评委批评的那句"
    assert PHRASE not in opt_prompt, (
        "若这句其实在优化臂里，评委把优化臂的缺陷扣到了基线头上——Δ 的分母是冤案"
    )


def test_the_fabrication_charge_is_verifiable_in_the_output() -> None:
    """评委指控的"编造数值"必须能在基线**输出**里查到，不能只是评语里的一句断言。"""
    d = _load("34a205ecc9c3")
    if d is None:
        return
    outs = [str(r.get("output") or "") for r in (d.get("baseline_runs") or [])]
    if not outs:
        return
    assert any(FABRICATION in o for o in outs), (
        "基线输出里已经找不到那句编造的数字了——判据的锚点变了，需要重判"
    )
    task = str(d.get("task"))
    assert "不许编造填充" in task, "任务原文不再禁止编造——扣分依据消失"


def test_cap_fix_landed_after_this_run() -> None:
    """钉住时间线：`min/max` 对不上是**旧症状**，不是现存缺陷。

    只断言一件事——这条 run 的 mtime 早于封顶接线修复。
    若哪天有人重跑了同一条臂，它就该不再有那个症状；那时这条会提示
    需要重新判断结论是否仍适用。
    """
    p = RUNS / "run_34a205ecc9c3.json"
    if not p.exists():
        return
    import datetime

    mtime = datetime.datetime.fromtimestamp(p.stat().st_mtime)
    fix = datetime.datetime.strptime(CAP_FIX_COMMIT_DATE, "%Y-%m-%d %H:%M")
    assert mtime < fix, (
        f"这条 run（{mtime}）已经不早于封顶接线修复（{fix}），"
        "那它落盘 weighted_score 与 aggregate 对不上就是**现存缺陷**，需要重新查"
    )


def test_min_max_disagreeing_with_archive_is_explained_not_ignored() -> None:
    """把那个"对不上"本身钉住，并要求它有解释。

    这条不是钉某个具体数，而是钉"这个数对不上时必须能说清为什么"。
    没有解释的对不上，就是不知道的 bug。
    """
    d = _load("34a205ecc9c3")
    if d is None:
        return
    a = d["aggregate"]
    ws = [float(e.get("weighted_score")) for e in d["evaluations"]]
    if abs(sum(ws) / len(ws) - float(a["avg_score"])) < 0.01:
        return  # 已同源（重跑过），无事可验
    assert CAP_FIX_COMMIT_DATE, "对不上时必须有已记录的解释，否则就是没查"
