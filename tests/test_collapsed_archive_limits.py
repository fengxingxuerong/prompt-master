"""不许再拿"折叠后的归档"去推"逐采样的形态"——§三十·十五 的教训。

§三十·十四·四 我看着 `run_71ee15abd123` 的 case#0
（`sample_scores=[8.1, 1.0]`、judge="evaluator"、issues 是评委评语）
写下"那个 1.0 是失败兜底、判据漏了它"。**结论是错的**：读它的两条
`test_runs` 输出全文，600~900 字、三章节齐全、`error=None`——
那个 1.0 是评委真给出的一份低分，是**信号**不是废样本。

错因：归档（`run_*.json` 与 `eval_cache`）**只存折叠后的结果**。
`eval_cache` 160 条全是 `sample_index=0`；`evaluations[*].sample_scores`
只是一个分数列表，**不带各自的 issues / judge / dimension_scores**。
拿它去推"哪一次采样废了"，等于凭一个不存在的证据做推理。

判据（这些是可查的事实，不是我的断言）：
- 归档里是否存在**逐采样**的评估产物；
- 折叠产物能支撑什么粒度的结论。
读数：从真实归档现查。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

LOGS = Path(__file__).resolve().parents[1] / "logs"


def _archives() -> list[Path]:
    if not LOGS.exists():
        return []
    out = []
    for p in LOGS.glob("run_*.json"):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        # 夹具造出来的短跑（test_runs 极少）不参与
        if len(d.get("test_runs") or []) >= 4:
            out.append(p)
    return out


def test_archive_only_stores_collapsed_results() -> None:
    """钉住：归档里的 evaluations 是**每用例一条**，不是每次采样一条。"""
    files = _archives()
    if not files:
        pytest.skip("本机没有归档")

    for p in files:
        d = json.loads(p.read_text(encoding="utf-8"))
        evals = d.get("evaluations") or []
        n_cases = len({e.get("test_case_index") for e in evals})
        assert len(evals) == n_cases, (
            f"{p.name}: evaluations 有 {len(evals)} 条但只有 {n_cases} 条用例——"
            "若这里变了，说明归档开始存逐采样产物，本文件的判据要重写"
        )


def test_sample_scores_carry_no_per_sample_evidence() -> None:
    """`sample_scores` 只有分数；逐采样的 issues 不在里面。

    这是 §三十·十四·四 推理前提为假的直接证据：拿它推不出"哪次采样废了"。
    """
    files = _archives()
    if not files:
        pytest.skip("本机没有归档")

    checked = 0
    for p in files:
        for e in json.loads(p.read_text(encoding="utf-8")).get("evaluations") or []:
            ss = e.get("sample_scores")
            if not isinstance(ss, list) or len(ss) < 2:
                continue
            checked += 1
            assert all(
                isinstance(x, (int, float)) for x in ss
            ), f"{p.name}: sample_scores 里出现了非数字——判据要重写"
            # 逐采样数量最多等于该用例的采样数；且**没有**逐采样的 issues 字段
            assert not any(k.startswith("sample_issues") for k in e), (
                "若将来给 sample_scores 配了逐采样证据，"
                "§三十·十四·四 的推理前提就不再是假的"
            )
    if not checked:
        pytest.skip("没有多条采样的用例可查")


def test_the_low_score_in_case0_was_a_real_verdict() -> None:
    """钉住这次翻案本身：那个 1.0 不是废样本，剔掉才是错的。

    用归档里可查的事实说话：两次输出都完整 ⇒ 1.0 是评委给的分。
    """
    p = LOGS / "run_71ee15abd123.json"
    if not p.exists():
        pytest.skip("归档不在本机")
    d = json.loads(p.read_text(encoding="utf-8"))
    runs = [r for r in (d.get("test_runs") or []) if r.get("test_case_index") == 0]
    assert len(runs) == 2, "归档变了，需要重新判断这个用例"

    for r in runs:
        out = str(r.get("output") or "").strip()
        assert len(out) >= 400, f"case#0 某次采样输出只有 {len(out)} 字——那时才是空产出"
        assert r.get("error") in (None, ""), f"case#0 某次采样 error={r.get('error')!r}"
        assert "趋势结论" in out, "case#0 的输出没有三章节骨架"

    e0 = next(x for x in d["evaluations"] if x["test_case_index"] == 0)
    assert e0["sample_scores"] == [8.1, 1.0]
    # 真低分必须仍然留在统计里：判据不许把它当成废样本剔掉
    from pm.nodes.judge import _is_empty_output_eval

    assert _is_empty_output_eval(e0) is False, (
        "case#0 的 1.0 是真低分（两次输出都完整合规），"
        "判据若把它当废样本，就是把信号删掉——§三十·十五 的翻案就白翻了"
    )


def test_errors_count_is_not_a_waste_sample_signal() -> None:
    """顺带钉住 §三十·十五·三 否掉的方案：errors 条数 > 0 ≠ 存在废样本。

    反证用 run_13f9a99096e9 的 case#0：errors 里有 1 条 evaluator_b 超时，
    可它的两次采样 [8.85, 6.22] 两票都评出来了（失败被仲裁吸收）。
    """
    p = LOGS / "run_13f9a99096e9.json"
    if not p.exists():
        pytest.skip("归档不在本机")
    d = json.loads(p.read_text(encoding="utf-8"))
    errs = [str(e) for e in (d.get("errors") or []) if "evaluate#0[evaluator_b]" in str(e)]
    assert errs, "归档变了：case#0 本来有 1 条 evaluator_b 失败"

    e0 = next(x for x in d["evaluations"] if x["test_case_index"] == 0)
    scores = e0.get("sample_scores") or []
    assert len(scores) == 2 and all(s > 5.0 for s in scores), (
        f"case#0 两次采样本该都高分，实际 {scores}"
    )
    assert e0.get("judge") == "merged"