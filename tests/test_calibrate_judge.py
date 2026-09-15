"""评委校准工具（根目录 calibrate_judge.py）回归。

评估路径用 CallHook 注入固定分数的假评委（校准的数值逻辑必须确定性可测）；
一致性分析（Δ/MAE/偏差/Pearson）是纯代码，直接断言。
根目录工具按 test_prompt_eval 的惯例直接 import 模块名（pytest 以 `python -m`
运行时 cwd=仓库根在 sys.path 上）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import calibrate_judge as cj
from pm import backend
from pm.schemas import DimensionScores, EvaluationResult


def _ev_with_score(score: float) -> EvaluationResult:
    """五个维度同分的评估结果 → 加权分恰好等于 score（权重和为 1）。"""
    return EvaluationResult(
        dimension_scores=DimensionScores(
            task_completion=score,
            format_adherence=score,
            constraint_compliance=score,
            robustness=score,
            quality=score,
        ),
        model_reported_score=score,
        issues=[],
        suggestions=[],
        should_revise=False,
    ).finalize()


# --------------------------------------------------------------------------
# 样本加载
# --------------------------------------------------------------------------
def test_load_samples_from_example():
    samples = cj.load_samples(cj.EXAMPLE_SAMPLES)
    assert len(samples) == 6
    assert {s["id"] for s in samples} == {
        "demo-good",
        "demo-hallucinated",
        "demo-vague",
        "anchor-fab-trend",
        "anchor-fab-attribution",
        "anchor-mixed",
    }


def test_load_samples_skips_comment_entries(tmp_path: Path):
    p = tmp_path / "s.json"
    p.write_text(
        json.dumps(
            [
                {"_comment": "说明"},
                {
                    "id": "a",
                    "original_task": "t",
                    "prompt": "p",
                    "test_input": "i",
                    "test_output": "o",
                    "human_score": 7,
                },
            ]
        ),
        encoding="utf-8",
    )
    assert len(cj.load_samples(p)) == 1


def test_load_samples_rejects_missing_fields_and_bad_scores(tmp_path: Path):
    p = tmp_path / "s.json"
    p.write_text(json.dumps([{"id": "a"}]), encoding="utf-8")
    try:
        cj.load_samples(p)
        raise AssertionError("缺字段必须报错")
    except ValueError as e:
        assert "缺字段" in str(e)

    p.write_text(
        json.dumps(
            [
                {
                    "id": "a",
                    "original_task": "t",
                    "prompt": "p",
                    "test_input": "i",
                    "test_output": "o",
                    "human_score": 11,
                }
            ]
        ),
        encoding="utf-8",
    )
    try:
        cj.load_samples(p)
        raise AssertionError("human_score 超界必须报错")
    except ValueError as e:
        assert "1-10" in str(e)


def test_load_samples_rejects_duplicate_ids(tmp_path: Path):
    p = tmp_path / "s.json"
    dup = {
        "id": "a",
        "original_task": "t",
        "prompt": "p",
        "test_input": "i",
        "test_output": "o",
        "human_score": 5,
    }
    p.write_text(json.dumps([dup, dup]), encoding="utf-8")
    try:
        cj.load_samples(p)
        raise AssertionError("重复 id 必须报错")
    except ValueError as e:
        assert "重复" in str(e)


# --------------------------------------------------------------------------
# 一致性分析（纯代码）
# --------------------------------------------------------------------------
def test_pearson_basic_and_guards():
    assert cj.pearson([1, 2, 3], [2, 4, 6]) == 1.0  # 完全正相关
    assert cj.pearson([1, 2, 3], [3, 2, 1]) == -1.0  # 完全负相关
    assert cj.pearson([1, 2], [1, 2]) is None  # 样本不足
    assert cj.pearson([5, 5, 5], [1, 2, 3]) is None  # 人工分无方差
    assert cj.pearson([1, 2, 3], [5, 5, 5]) is None  # 评委分无方差


def test_analyze_deltas_bias_mae_flags():
    samples = [
        {"id": "s1", "human_score": 8},
        {"id": "s2", "human_score": 3},
        {"id": "s3", "human_score": 4},
    ]
    a = cj.analyze(samples, [9.0, 6.0, 4.0])
    assert a["n"] == 3
    assert [p["delta"] for p in a["pairs"]] == [1.0, 3.0, 0.0]
    assert a["bias"] == 1.33  # (1+3+0)/3
    assert a["mae"] == 1.33
    assert a["flags"] == ["s2"]  # |Δ|=3 > 2.0
    assert a["r"] is not None and a["r"] > 0.5  # 排序基本一致


def test_bias_verdict_thresholds():
    assert "偏松" in cj._bias_verdict(1.5)
    assert "偏严" in cj._bias_verdict(-1.5)
    assert "容差内" in cj._bias_verdict(0.4)


def test_r_verdict_thresholds():
    assert "好" in cj._r_verdict(0.9)
    assert "差" in cj._r_verdict(0.3)
    assert "中等" in cj._r_verdict(0.65)
    assert "无法评估" in cj._r_verdict(None)


def test_render_report_flags_small_sample():
    a = cj.analyze([{"id": "s1", "human_score": 8}], [9.0])
    text = cj.render_report("evaluator", a)
    assert "评委校准报告" in text
    assert "方向性参考" in text  # n < 5 必须声明诚实边界
    assert "s1" in text


# --------------------------------------------------------------------------
# calibrate 主流程（假评委注入）
# --------------------------------------------------------------------------
def test_calibrate_with_fake_judge():
    samples = cj.load_samples(cj.EXAMPLE_SAMPLES)
    calls = {"n": 0}
    # 6 锚点的假评委分：前 3 条与示例参考分接近，后 3 条编造变体刻意给偏松分
    fake_scores = [9.0, 6.0, 4.0, 5.5, 5.0, 6.0]

    def structured(role, model_cls, system, user, max_retries=3, overrides=None):
        calls["n"] += 1
        return _ev_with_score(fake_scores[calls["n"] - 1]), {"role": role}

    hook = backend.CallHook(structured=structured, plain=None, disable_cache=True)
    with backend.use(hook):
        analysis, errors = cj.calibrate(samples, "evaluator")
    assert not errors
    assert analysis["n"] == 6
    # demo-hallucinated(3→6) 与 anchor-fab-trend(2.5→5.5) 均超 2.0 大偏差线
    assert analysis["flags"] == ["demo-hallucinated", "anchor-fab-trend"]
    assert analysis["r"] is not None


def test_calibrate_skips_failing_samples_but_reports():
    samples = cj.load_samples(cj.EXAMPLE_SAMPLES)
    calls = {"n": 0}

    def structured(role, model_cls, system, user, max_retries=3, overrides=None):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("Error code: 429 - rate limit exceeded")
        return _ev_with_score(5.0), {"role": role}

    hook = backend.CallHook(structured=structured, plain=None, disable_cache=True)
    with backend.use(hook):
        analysis, errors = cj.calibrate(samples, "evaluator_b")
    assert len(errors) == 1
    assert "429" in errors[0][1]
    assert analysis["n"] == 5  # 失败锚点被排除，不污染一致性指标


# --------------------------------------------------------------------------
# CLI：模板生成
# --------------------------------------------------------------------------
def test_cli_write_template_and_idempotent(tmp_path: Path):
    target = tmp_path / "samples.json"
    for _expected in (0, 0):
        sys.argv = ["calibrate_judge.py", "--samples", str(target), "--write-template"]
        assert cj.main() == 0
    assert target.exists()
    assert len(cj.load_samples(target)) == 6
