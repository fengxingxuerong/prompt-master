"""原稿改进模式（seed_prompt）的回归。

竞品的第一用法是"把我已经写好的提示词改好"，而本产品的入口此前只有 `--task`
（从零生成）。这条线一旦接进来，有两个必须钉住的语义：
1. 基线臂跟着换成原稿 —— 否则 Δ 还在回答"比不优化好多少"，
   而读者要的是"比你自己的版本好多少"；报告必须写清跑的是哪臂。
2. 原稿的代码侧体检结果不许进 `prompt_quality_issues` —— 那一列会按当前轮次
   注入评委，让改进版为原稿的毛病挨扣分。
"""

from __future__ import annotations

import argparse

import pm.nodes.optimize as optimize_mod
import pm.report as report_mod
import pytest
from pm.cli.support import read_seed_prompt
from pm.mcp_server import optimize_submit
from pm.nodes import baseline_node, optimize_node
from pm.prompts import (
    OPTIMIZER_SYSTEM,
    REFINER_SYSTEM,
    REFINER_USER,
)
from pm.report import render_report
from pm.server import OptimizeRequest
from pm.state import SEED_PROMPT_MAX_CHARS, initial_state
from pydantic import ValidationError

META = {"model": "fake", "channel": "fake", "attempts": 1, "latency_ms": 1}

# 能过代码侧质量门的交付物（≥100 字、无元话语、约束段无编号超标）
GOOD_PROMPT = (
    "[角色] 你是资深销售数据分析师。\n"
    "[任务] 依据用户提供的销售数据，输出趋势结论、异常点与下一步建议。\n"
    "[输出格式] Markdown 表格：区域｜销售额｜同比，随后给出 3 条结论。\n"
    "[边界处理] 输入完全为空时标注「数据缺失」；仅部分字段缺失时已有数据照常输出。"
)

# 用户原稿：故意带两类实测会出事的缺陷（约束超载 + 抑制型条款）
DEFECTY_SEED = (
    "[角色] 你是销售数据助手。\n[任务] 分析用户给的销售数据，给出结论。\n[约束]\n"
    + "\n".join(f"{i}. 第 {i} 条约束，必须逐条遵守不能漏。" for i in range(1, 11))
    + "\n11. 输入异常时停止处理，不输出任何结论。"
)


def _capture_plain(outcome: str = GOOD_PROMPT):
    """返回 (stub, captured)：stub 记录每次调用的 (role, system, user)。"""
    seen: list[dict[str, str]] = []

    def plain(role, system, user, overrides=None):
        seen.append({"role": role, "system": system, "user": user})
        return outcome, dict(META)

    return plain, seen


# --------------------------------------------------------------------------
# state 层
# --------------------------------------------------------------------------
def test_initial_state_stores_stripped_seed():
    st = initial_state(task="分析销售数据", seed_prompt=f"  {DEFECTY_SEED}  ")
    assert st["seed_prompt"] == DEFECTY_SEED
    assert st["seed_quality_findings"] == []


def test_initial_state_seed_defaults_empty():
    """不给原稿时必须是空串而不是 None：节点用 `or ""` 判分支，None 会一路传到报告层。"""
    assert initial_state(task="t")["seed_prompt"] == ""


# --------------------------------------------------------------------------
# optimize 节点：分支选择
# --------------------------------------------------------------------------
def test_seed_routes_to_refiner_template(monkeypatch):
    plain, seen = _capture_plain()
    monkeypatch.setattr("pm.llm.plain_call", plain)
    state = initial_state(task="分析销售数据并给结论", seed_prompt=DEFECTY_SEED)

    out = optimize_node(state)  # type: ignore[arg-type]

    assert out["trace"][0]["event"] == "optimize_done"
    assert len(seen) == 1
    assert seen[0]["system"] == REFINER_SYSTEM
    assert seen[0]["system"] != OPTIMIZER_SYSTEM
    assert DEFECTY_SEED in seen[0]["user"]
    # 体检命中项作为判据进改进器（不是只写进报告）
    assert "约束超载" in seen[0]["user"]
    assert out["prompt_versions"][0]["note"] == "改进自用户原稿"
    assert out["seed_quality_findings"]
    assert out["trace"][0]["seed_chars"] == len(DEFECTY_SEED)


def test_refiner_prompt_has_no_unrendered_placeholder(monkeypatch):
    """REFINER_USER 加了占位符却忘了在 render() 传参时，模板会原样发给模型。"""
    plain, seen = _capture_plain()
    monkeypatch.setattr("pm.llm.plain_call", plain)
    optimize_node(initial_state(task="分析销售数据", seed_prompt=DEFECTY_SEED))  # type: ignore[arg-type]
    assert "<<" not in seen[0]["user"], "user 段残留未渲染占位符"
    declared = {t for t in REFINER_USER.split("<<") if t.endswith(">>")}
    assert declared == set(), f"模板里仍有未替换的占位符：{declared}"


def test_seed_findings_do_not_enter_judge_channel(monkeypatch):
    """原稿的问题不许混进 prompt_quality_issues：judge 按轮次取用那一列注入评委。"""
    plain, _seen = _capture_plain()
    monkeypatch.setattr("pm.llm.plain_call", plain)

    out = optimize_node(initial_state(task="分析销售数据", seed_prompt=DEFECTY_SEED))  # type: ignore[arg-type]

    assert out["seed_quality_findings"]
    assert not out.get("prompt_quality_issues"), "交付版干净时不得继承原稿的问题"


def test_seed_mode_skips_memory_hint(monkeypatch):
    """原稿改进模式不进记忆层：相似资产的骨架会诱导整段重写，与"最小改动"对冲。"""

    def boom(*_a, **_k):
        raise AssertionError("seed 模式不该查相似资产")

    plain, _seen = _capture_plain()
    monkeypatch.setattr("pm.llm.plain_call", plain)
    monkeypatch.setattr(optimize_mod, "find_similar_asset", boom)

    out = optimize_node(initial_state(task="分析销售数据", seed_prompt=DEFECTY_SEED))  # type: ignore[arg-type]
    assert out["trace"][0]["event"] == "optimize_done"


def test_no_seed_keeps_generator_branch(monkeypatch):
    """回归护栏：没交原稿时行为与改动前逐字一致（模板、版本注记、无 seed 台账）。"""
    plain, seen = _capture_plain()
    monkeypatch.setattr("pm.llm.plain_call", plain)

    out = optimize_node(initial_state(task="分析销售数据"))  # type: ignore[arg-type]

    assert seen[0]["system"] == OPTIMIZER_SYSTEM
    assert out["prompt_versions"][0]["note"] == "初版"
    assert out["trace"][0]["seed_chars"] is None
    assert "seed_quality_findings" not in out


def test_oversized_seed_fails_before_any_llm_call(monkeypatch):
    """节点侧也要有长度闸：CLI/REST 之外还有人直接构造 state（scheduler、测试、脚本）。"""
    plain, seen = _capture_plain()
    monkeypatch.setattr("pm.llm.plain_call", plain)
    state = initial_state(task="分析销售数据")
    state["seed_prompt"] = "约" * (SEED_PROMPT_MAX_CHARS + 1)

    out = optimize_node(state)  # type: ignore[arg-type]

    assert out["status"] == "failed"
    assert seen == []
    assert any("超过上限" in e for e in out["errors"])


# --------------------------------------------------------------------------
# baseline 节点：基线臂选择
# --------------------------------------------------------------------------
class _Probe(Exception):
    """从 _run_matrix 的入参里取走基线提示词后立即中断，避开整套评分夹具。"""


def _arm_prompt_for(monkeypatch, state) -> str:
    monkeypatch.setenv("PM_BASELINE", "1")
    captured: dict[str, str] = {}

    def fake_matrix(cases, prompt, target_model, expected, mode, k, conc):
        captured["prompt"] = prompt
        raise _Probe

    monkeypatch.setattr("pm.nodes.baseline._run_matrix", fake_matrix)
    out = baseline_node(state)  # type: ignore[arg-type]
    assert out["trace"][0]["event"] == "baseline_failed", "探针异常应被基线兜底吞掉"
    return captured["prompt"]


def test_baseline_arm_uses_user_seed(monkeypatch):
    state = initial_state(task="分析销售数据", seed_prompt=DEFECTY_SEED)
    state["test_cases"] = ["Q1 华东 120 万"]
    assert _arm_prompt_for(monkeypatch, state) == DEFECTY_SEED


def test_baseline_arm_falls_back_to_raw_task(monkeypatch):
    state = initial_state(task="分析销售数据")
    state["test_cases"] = ["Q1 华东 120 万"]
    assert _arm_prompt_for(monkeypatch, state) == "分析销售数据"


# --------------------------------------------------------------------------
# 报告披露
# --------------------------------------------------------------------------
def _agg(**over):
    base = {
        "avg_score": 7.0,
        "min_score": 6.0,
        "max_score": 8.0,
        "n_cases": 1,
        "n_cases_expected": 1,
        "cases_complete": True,
        "n_passed": 0,
        "passed": False,
        "ci_lower": 6.5,
        "n_samples": 1,
        "sem": 0.2,
        "noise": 0.3,
        "unstable_cases": [],
        "judge_bias": 0.0,
        "judge_bias_warning": False,
        "n_assertions": 0,
        "n_assertions_failed": 0,
        "all_issues": [],
    }
    base.update(over)
    return base


def _rep_state(**over):
    base = {
        "run_id": "r-seed",
        "status": "max_iterations",
        "iteration": 1,
        "max_iterations": 3,
        "target_model": "some-model",
        "llm_calls": 12,
        "aggregate": _agg(),
        "prompt_versions": [
            {"iteration": 0, "prompt": GOOD_PROMPT, "avg_score": 7.0, "min_score": 6.0},
        ],
        "test_runs": [],
    }
    base.update(over)
    return base


def test_report_names_the_baseline_arm_and_mode():
    report, _best = render_report(
        _rep_state(
            seed_prompt=DEFECTY_SEED,
            seed_quality_findings=["约束超载：共 11 条"],
            baseline_aggregate=_agg(avg_score=6.0, min_score=5.0),
        )
    )
    assert "模式：原稿改进" in report
    assert "与基线对比（用户原稿直喂 target" in report
    assert "Δ 读作「比你自己的版本好多少」" in report
    assert "## 原稿体检" in report
    assert "约束超载：共 11 条" in report


def test_report_wording_unchanged_without_seed():
    report, _best = render_report(_rep_state(baseline_aggregate=_agg(avg_score=6.0, min_score=5.0)))
    assert "与基线对比（原始需求直喂 target" in report
    assert "原稿体检" not in report
    assert "模式：原稿改进" not in report


def test_report_seed_mode_still_clears_when_no_seed_present():
    """只有空白原稿（判空后等同未交）时不得渲染原稿章节，避免报告吹一个不存在的输入。"""
    assert report_mod._seed_arm({"seed_prompt": "   \n "}) == ""
    report, _best = render_report(_rep_state(seed_prompt="   \n "))
    assert "原稿体检" not in report


def test_missing_baseline_wording_follows_the_arm():
    """没跑基线时那句解释也得跟着模式变，否则把"比你的原稿好多少"回答成"比不优化好多少"。"""
    report, _ = render_report(_rep_state(seed_prompt=DEFECTY_SEED))
    assert "比你的原稿好多少" in report

    report2, _ = render_report(_rep_state())
    assert "比不优化好多少" in report2


# --------------------------------------------------------------------------
# CLI 参数护栏（同步 CLI 与 submit 子命令共用 read_seed_prompt）
# --------------------------------------------------------------------------
def _ns(**over):
    base = {"prompt": None, "prompt_file": None}
    base.update(over)
    return argparse.Namespace(**base)


def test_cli_seed_both_args_rejected():
    with pytest.raises(SystemExit):
        read_seed_prompt(argparse.ArgumentParser(), _ns(prompt="x" * 20, prompt_file="a.md"))


def test_cli_seed_rejects_short_text_as_misused_task(capsys):
    """把需求粘进 --prompt 是最常见的误用，必须当场纠正而不是白烧一轮调用。"""
    with pytest.raises(SystemExit):
        read_seed_prompt(argparse.ArgumentParser(), _ns(prompt="分析数据"))
    assert "请改用 --task" in capsys.readouterr().err


def test_cli_seed_length_bounds(tmp_path):
    p = argparse.ArgumentParser()
    too_long = tmp_path / "long.md"
    too_long.write_text("约" * (SEED_PROMPT_MAX_CHARS + 5), encoding="utf-8")
    with pytest.raises(SystemExit):
        read_seed_prompt(p, _ns(prompt_file=str(too_long)))

    ok = tmp_path / "ok.md"
    ok.write_text(f"  {DEFECTY_SEED}\n", encoding="utf-8")
    assert read_seed_prompt(p, _ns(prompt_file=str(ok))) == DEFECTY_SEED


def test_cli_seed_missing_file_errors_not_traceback(tmp_path):
    with pytest.raises(SystemExit):
        read_seed_prompt(argparse.ArgumentParser(), _ns(prompt_file=str(tmp_path / "不存在.md")))


def test_cli_seed_absent_returns_empty():
    assert read_seed_prompt(argparse.ArgumentParser(), _ns()) == ""


# --------------------------------------------------------------------------
# REST / MCP 入口
# --------------------------------------------------------------------------
def test_rest_seed_bound_matches_single_source():
    """20000 只许有一个事实源：REST 侧写死字面量就会与 CLI/节点漂移。"""
    assert OptimizeRequest(
        task="分析销售数据", seed_prompt="约" * SEED_PROMPT_MAX_CHARS
    ).seed_prompt
    with pytest.raises(ValidationError):
        OptimizeRequest(task="分析销售数据", seed_prompt="约" * (SEED_PROMPT_MAX_CHARS + 1))


def test_rest_passes_seed_to_scheduler(monkeypatch):
    import pm.server as server_mod
    from fastapi.testclient import TestClient

    submitted: dict = {}

    class _FakeSched:
        def submit(self, run_id, **kwargs):
            submitted.update(kwargs)

    monkeypatch.setattr(server_mod, "_scheduler", _FakeSched())
    monkeypatch.delenv("PM_API_TOKEN", raising=False)
    client = TestClient(server_mod.app)
    r = client.post(
        "/api/optimize",
        json={"task": "分析销售数据", "seed_prompt": f"  {DEFECTY_SEED}  "},
    )
    assert r.status_code == 200, r.text
    assert submitted["seed_prompt"] == DEFECTY_SEED


def test_mcp_tool_forwards_seed(monkeypatch):
    seen: dict = {}

    def fake_http(method, url, payload=None, timeout=30.0):
        seen.update(payload or {})
        return {"run_id": "r-mcp"}

    monkeypatch.setattr("pm.mcp_server._http_json", fake_http)
    optimize_submit(task="分析销售数据", seed_prompt=DEFECTY_SEED)
    assert seen["seed_prompt"] == DEFECTY_SEED

    seen.clear()
    optimize_submit(task="分析销售数据", seed_prompt="   ")
    assert "seed_prompt" not in seen, "空白原稿不得作为原稿模式提交"
