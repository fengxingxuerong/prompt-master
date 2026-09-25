"""评委校准引擎（`pm/calibration.py`）回归。

评估路径用 CallHook 注入固定分数的假评委（校准的数值逻辑必须确定性可测）；
一致性分析（Δ/MAE/偏差/Pearson）是纯代码，直接断言。
（2026-09-25：这引擎原来叫根目录 `calibrate_judge.py`，装包时装不走、`run.py calibrate`
在 site-packages 里就 import 不到，所以搬进了 `pm/`。）
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from pm import backend
from pm import calibration as cj
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


def _anchor(sid: str, score: object, **kw: object) -> dict[str, object]:
    base: dict[str, object] = {
        "id": sid,
        "original_task": "t",
        "prompt": "p",
        "test_input": "i",
        "test_output": "o",
        "human_score": score,
    }
    base.update(kw)
    return base


def test_unconfirmed_anchors_are_gated_out(tmp_path: Path):
    """候选不进分母。允许它带 null 人工分等人来填，但绝不能参与打分统计。"""
    p = tmp_path / "s.json"
    p.write_text(
        json.dumps(
            [
                _anchor("confirmed-one", 7.5),
                _anchor("candidate-a", None, confirmed=False),
                _anchor("candidate-b", 8.0, confirmed=False),
                _anchor("candidate-str", 9.0, confirmed="false"),
            ]
        ),
        encoding="utf-8",
    )
    samples = cj.load_samples(p)
    assert [s["id"] for s in samples] == ["confirmed-one"]
    assert {x["id"] for x in samples.pending} == {"candidate-a", "candidate-b", "candidate-str"}


def test_confirmed_with_null_human_score_is_an_error(tmp_path: Path):
    """写 confirmed=true 却没填人工分 = 想蒙混过关，必须炸而不是静默剔除。"""
    p = tmp_path / "s.json"
    p.write_text(json.dumps([_anchor("x", None, confirmed=True)]), encoding="utf-8")
    try:
        cj.load_samples(p)
        raise AssertionError("已确认但无分数必须报错")
    except ValueError as e:
        assert "null" in str(e)


def test_missing_confirmed_field_keeps_legacy_files(tmp_path: Path):
    """存量 samples.json 没有 confirmed 字段，缺省必须按"人工已确认"处理，
    否则这次改动会静默作废全部历史校准。"""
    p = tmp_path / "s.json"
    p.write_text(json.dumps([_anchor("legacy", 6.0)]), encoding="utf-8")
    samples = cj.load_samples(p)
    assert len(samples) == 1 and not samples.pending


def test_pending_duplicate_id_still_detected(tmp_path: Path):
    """pending 也要进 id 唯一性检查：同一份输出既当候选又当已确认锚点，
    等于同一条数据在报告里出现两次而分母只算一次。"""
    p = tmp_path / "s.json"
    p.write_text(
        json.dumps([_anchor("dup", 6.0), _anchor("dup", None, confirmed=False)]),
        encoding="utf-8",
    )
    try:
        cj.load_samples(p)
        raise AssertionError("与候选重复的 id 必须报错")
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
    assert "好" in cj._r_verdict(0.9, 0.9, 4)
    assert "差" in cj._r_verdict(0.3, 0.3, 4)
    assert "中等" in cj._r_verdict(0.65, 0.65, 4)
    assert "无法评估" in cj._r_verdict(None, None, 4)
    # 窄区间上算出来的 r 必须自己说清楚：n 很大但人工分全挤在一带时它毫无含义
    assert "窄区间" in cj._r_verdict(0.9, 0.9, 1)


def test_spearman_tracks_order_that_pearson_penalises():
    """严格单调但等距性差（分档跳变）的一组数：ρ=1，r<1。

    实测失效形态就是这种台阶——仲裁同一份内容稳定落在 5.15 与 7.10 两个模式，
    Pearson 会把台阶本身当成"不一致"，而闭环真正依赖的是序关系。
    """
    humans = [1.0, 2.0, 3.0, 4.0]
    judges = [1.0, 2.0, 4.0, 8.0]
    rho = cj.spearman(humans, judges)
    r = cj.pearson(humans, judges)
    assert rho == 1.0
    assert r is not None and r < 1.0


def test_spearman_none_on_zero_variance():
    assert cj.spearman([5.0, 5.0, 5.0], [1.0, 5.0, 9.0]) is None
    assert cj.spearman([5.0], [1.0]) is None


def test_analyze_counts_lenient_and_strict():
    """漏放与误杀要分开数：都算进 MAE 的话，一个只会抬分的评委和一个只会压分的评委同分。"""
    samples = [
        {"id": "a", "human_score": 6.0},
        {"id": "b", "human_score": 7.0},
        {"id": "c", "human_score": 9.0},
        {"id": "d", "human_score": 9.5},
    ]
    a = cj.analyze(samples, [8.5, 8.5, 7.0, 9.5])
    dec = a["decision"]
    assert dec["n_lenient"] == 2  # 人工不可用、评委判可用
    assert dec["n_strict"] == 1  # 人工可用、评委判不可用
    assert dec["usable"] is True
    assert dec["kappa"] is not None


def test_analyze_decision_unusable_when_line_one_side_empty():
    """全部锚点都在判定线同一侧时，一致率会是 100% —— 那读的是取样，必须标 unusable。"""
    samples = [{"id": f"s{i}", "human_score": 9.0} for i in range(6)]
    a = cj.analyze(samples, [9.0] * 6)
    assert a["decision"]["agree"] == 1.0
    assert a["decision"]["usable"] is False
    assert a["bands_covered"] == 1


def test_render_provenance_only_when_pending():
    assert cj.render_provenance(45, 34).count("34") == 1
    assert "未经人工确认" in cj.render_provenance(45, 34)
    assert cj.render_provenance(11, 0) == ""


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
    # 失败清单必须同时挂进 analysis：报告与账本只拿得到 analysis，
    # "n 变少了但不知是谁"就是 2026-09-25 那轮 A/B 无法归因的直接原因
    assert analysis["n_failed"] == 1
    assert analysis["failed"][0]["id"] == errors[0][0]
    assert "429" in analysis["failed"][0]["error"]


def test_render_report_names_the_dropped_anchors():
    """报告里要写清被排除的是谁、为什么，而不是一句"排除评估失败的锚点"。"""
    a = cj.analyze([{"id": "s1", "human_score": 8}, {"id": "s2", "human_score": 7}], [9.0, 8.0])
    a["n_failed"] = 2
    a["failed"] = [
        {"id": "real-triage-overconservative", "error": "GatewayError: 上游 500"},
        {"id": "anchor-fab-trend", "error": "ValidationError: 结构化输出解析失败"},
    ]
    text = cj.render_report("evaluator", a)
    assert "real-triage-overconservative" in text and "anchor-fab-trend" in text
    assert "2 条锚点评估失败" in text
    assert "同一张考卷" in text, "要同时说清'n 变了就不该与旧记录比'"
    # 一条都没失败时不许出现这段话：否则读者会以为每轮都在丢锚点
    clean = cj.render_report("evaluator", cj.analyze([{"id": "s1", "human_score": 8}], [9.0]))
    assert "评估失败被排除" not in clean


# --------------------------------------------------------------------------
# CLI：模板生成
# --------------------------------------------------------------------------
def test_cli_write_template_and_idempotent(tmp_path: Path):
    target = tmp_path / "samples.json"
    for _expected in (0, 0):
        sys.argv = ["-m pm.calibration", "--samples", str(target), "--write-template"]
        assert cj.main() == 0
    assert target.exists()
    assert len(cj.load_samples(target)) == 6
