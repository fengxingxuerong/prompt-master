"""`run.py history` 的取样过滤不许把**假跑**当真运行（§三十·二）。

## 缺陷

`is_demo` 判的是 `trace` 里 channel 含不含 `"fake"` —— 那是 pytest 的
fake **后端**留下的痕迹。而"用例生成降级"的 **mock 跑**走的是真实通道名
（`plain` / `json_fallback` / `function_calling`），一个都匹配不上。

实测：36 条 mock 臂**全部漏判**，混进 Δ 的统计当成了真运行。它们全部来自同一个
任务、全部 Δ=0.0（avg 与 base 都是 8.25）、`n_samples` 恒为 1 ——
于是 `run.py history` 里那个 `n=36 Δ=0.00 CI[0,0]` 的大组，**整组都是假数据**。

## 为什么这条闸以前不存在

因为它**不会被发现**：mock 臂的 Δ 全是 0，不显著也不碍事，
混进哪个任务都只是让那个任务的样本数变大。没人会去核对"这 36 条哪来的"。

## 判据

1. mock 跑（`errors` 里有 `mock:` 前缀）必须判为 demo；
2. 真跑必须**不**被误判 —— 判别式要能分开，不是"把所有带 errors 的都排除"
   （真跑也会带 errors，比如 §三十·一 那条结构化输出崩掉的臂）；
3. 判据来源必须是 `pm/nodes/execute.py` 里**写出那个标记的那三行**，
   不是"看着像"的字段。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from pm.cli.history import _arm_is_trusted, _is_mock_run

ROOT = Path(__file__).resolve().parents[1]
LOGS = ROOT / "logs"
RUNS = LOGS


def _d(**kw) -> dict:
    base = {"errors": [], "trace": [], "aggregate": {}, "baseline_aggregate": {}}
    base.update(kw)
    return base


# ---------------------------------------------------------------------------
# 判据一 & 二：判别式能分开
# ---------------------------------------------------------------------------
def test_mock_marker_is_treated_as_demo() -> None:
    assert _is_mock_run(_d(errors=["mock: 场景覆盖缺失（main_path、boundary）"])) is True


def test_mock_marker_from_every_branch_is_caught() -> None:
    """execute.py:224 / :227 / :233 三处各写一种 mock 文案，三处都得认。"""
    for msg in (
        "mock: 用例生成降级，已退回原始需求",  # degraded
        "mock: 只得到 2/4 条用例，样本量不足以支撑达标判定",  # 条数不够
        "mock: 场景覆盖缺失（main_path、boundary、injection）",  # 覆盖缺失
    ):
        assert _is_mock_run(_d(errors=[msg])) is True, msg


def test_real_run_with_errors_is_not_treated_as_demo() -> None:
    """真跑也会带 errors —— §三十·一 那两条崩溃臂就是。误判会把真数据删光。"""
    for msg in (
        "evaluate#3[evaluator_b]: Connection error.",
        "empty_output",
        "evaluate#2[evaluator]: [evaluator] 结构化输出失败，已重试 3 次。最后错误：ValueError",
        "revise: 修订返回空内容，已保留上一版",
    ):
        assert _is_mock_run(_d(errors=[msg])) is False, msg


def test_mock_run_is_recognised_without_trace_fake_marker() -> None:
    """正是这一档漏的：trace 用的是真实通道名。"""
    d = _d(
        errors=["mock: 场景覆盖缺失"],
        trace=[{"channel": "plain"}, {"channel": "function_calling"}],
    )
    assert _is_mock_run(d) is True
    assert "fake" not in {str(e.get("channel")) for e in d["trace"]}


# ---------------------------------------------------------------------------
# 判据三：判据来源就是写出那个标记的那三行
# ---------------------------------------------------------------------------
def test_marker_matches_what_execute_node_writes() -> None:
    """从 `pm/nodes/execute.py` 里把字面量抠出来，要求前缀一致。

    判据与来源不同源，早晚有一边改了而另一边不知道 —— 这条让它们绑在一起。
    """
    src = (ROOT / "pm" / "nodes" / "execute.py").read_text(encoding="utf-8")
    prefixes = {
        line.split('"mock:', 1)[1].split('"', 1)[0] for line in src.splitlines() if '"mock:' in line
    }
    assert prefixes, "execute.py 里找不到 mock: 字面量——判据的来源被改名/挪走了"
    for p in prefixes:
        assert _is_mock_run(_d(errors=[f"mock: {p}"])) is True, p


# ---------------------------------------------------------------------------
# 钉住实际发生过的漏判
# ---------------------------------------------------------------------------
def test_the_real_mock_arms_are_now_excluded() -> None:
    """归档里那些 mock 臂必须被识别出来。

    最初按"Δ 恒 0.0 + n_samples 恒 1"当指纹写死了 36 条 —— 那只是**其中一批**
    的指纹：`run_081de7650d8c` 是 mock（`n_samples=2`、Δ 非 0），
    说明"Δ=0 / n_samples=1"从来不是 mock 的判据，只是那 36 条恰好长那样。

    所以这里只要求"认得出"，并**把 mock 臂的多样性显式记下来**：
    判据必须是 `errors` 的前缀，而不是分数或极差的形状。
    """
    files = sorted(RUNS.glob("run_*.json"))
    if not files:
        pytest.skip("本机没有归档")
    mocks = []
    for p in files:
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if _is_mock_run(d):
            mocks.append((p.name, d.get("aggregate") or {}))
    assert mocks, "一条 mock 臂都没认出来——判别式失效"
    # 指纹多样性：mock 臂不该只有一种形状，否则那条判据很可能是在拟合某一批
    shapes = {(a.get("n_samples"), float(a.get("noise") or 0) == 0.0) for _, a in mocks}
    assert len(shapes) > 1, f"所有 mock 臂形状一致（{shapes}）——判据可能在拟合单一指纹"


def test_history_output_no_longer_shows_the_zero_group() -> None:
    """端到端：那个 `n=36 Δ=0.00` 的大组必须从输出里消失。"""
    proc = subprocess.run(
        [sys.executable, "run.py", "history", "--last", "500", "--json"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=ROOT,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr[-500:]
    payload = json.loads(proc.stdout)
    for g in payload["task_groups"]:
        assert not (g["n_runs"] >= 30 and set(g["deltas"]) == {0}), (
            f"又出现了一条 Δ 恒为 0 的大组（n={g['n_runs']}）——mock 臂没被滤干净"
        )


# ---------------------------------------------------------------------------
# §三十·一 的另一半：崩掉的臂要能被点名
# ---------------------------------------------------------------------------
def test_crashed_arm_is_flagged_untrusted_but_not_mock() -> None:
    """崩掉的真跑：不是 demo（那是"假的"），但不可信（那是"坏的"）。两个词不一样。"""
    d = _d(
        errors=["evaluate#3[evaluator_b]: Connection error."],
        aggregate={"avg_score": 1.0, "n_passed": 0, "untrusted_case_indices": [0, 1, 2, 3]},
    )
    assert _is_mock_run(d) is False
    assert _arm_is_trusted(d) is False


def test_legacy_archive_without_the_field_is_recovered() -> None:
    """旧归档没有 `untrusted_case_indices`，按实况回推。

    判据挂在 `min_score` 上（第三版）：均分触底**不算**判据 ——
    `1b234ac88efa` 均分 2.9 而 min 是 1.0，反过来 `a43e44adcc9f` 均分 7.83、
    min 4.0。只有"确实有用例触到下限"才是失败兜底的证据。
    """
    crashed = _d(aggregate={"avg_score": 1.0, "min_score": 1.0, "n_passed": 0})
    assert _arm_is_trusted(crashed) is False
    normal = _d(aggregate={"avg_score": 7.6, "min_score": 6.1, "n_passed": 2})
    assert _arm_is_trusted(normal) is True


def test_missing_min_score_is_indeterminate_not_accused() -> None:
    """`min_score` 缺失时不做判定 —— 宁可放过，不可冤。

    拿均分代替判据就是上一版的错：均值是聚合量，会被好用例拉高，
    从而放过半崩的臂（`1b234ac88efa`）。没有下限证据就没有崩的证据。
    """
    d = _d(aggregate={"avg_score": 1.0, "n_passed": 0})
    assert _arm_is_trusted(d) is True


# ---------------------------------------------------------------------------
# 回推判据的第二版：抓得住**半崩**的臂
# ---------------------------------------------------------------------------
def test_half_crashed_arm_is_not_trusted() -> None:
    """`run_1b234ac88efa`：两条用例输出为空（评 1.0）、一条评了 6.7。

    均分被拉到 **2.9** —— 判据第一版写的是"均分 ≤ 1.0"，这一条正好从指缝里漏过去，
    于是 Δ=−4.81（全臂最差）被当成一次正常测量混进了统计。
    """
    half = _d(aggregate={"avg_score": 2.9, "n_passed": 0, "min_score": 1.0})
    assert _arm_is_trusted(half) is False, "半崩的臂不许因为均分>1.0 就当成可信"


def test_genuinely_bad_but_valid_arm_stays_trusted() -> None:
    """对照组，而且是**真实归档里的一条**：`a43e44adcc9f`。

    它一条都没通过（`n_passed=0`），均分 7.83，但用例分是 `[7.81, 4.0, 4.0, 4.0]`
    —— 一次真实测量，只是优化没提上去。那正是我们要看的信号。

    判据第二版写成"`n_passed==0` 且均分 ≤ 5.0"时这条会被误判成崩溃；
    第三版才把它留得住。这条测试钉的就是那个"最容易改坏的一侧"。
    """
    p = RUNS / "run_a43e44adcc9f.json"
    if not p.exists():
        pytest.skip("归档不在本机")
    d = json.loads(p.read_text(encoding="utf-8"))
    assert d["aggregate"]["n_passed"] == 0, "前提变了：这条臂不是一条都没通过"
    assert _arm_is_trusted(d) is True, "跑成了但没提分 = 真实信号，不许当崩溃丢掉"
    synthetic = _d(aggregate={"avg_score": 3.4, "n_passed": 0, "min_score": 4.0})
    assert _arm_is_trusted(synthetic) is True


def test_the_two_real_crashed_arms_are_both_flagged() -> None:
    """把两条实际发生过的崩溃臂钉住：全崩的那条与半崩的那条都要被抓到。"""
    for run_id in ("7d87c5065c55", "1b234ac88efa"):
        p = RUNS / f"run_{run_id}.json"
        if not p.exists():
            pytest.skip("归档不在本机")
        assert _arm_is_trusted(json.loads(p.read_text(encoding="utf-8"))) is False, run_id


def test_floor_score_alone_is_not_enough() -> None:
    """触底 + 一条没通过，两个条件缺一不可。

    只看"有没有触底"会把 `e76f9acfefab` 那种（有 1.0 的用例但 2 条通过、
    属于真实测量）也拖下水；只看"n_passed==0"会误伤 `a43e44adcc9f`。
    """
    passed_anyway = _d(aggregate={"avg_score": 5.0, "n_passed": 1, "min_score": 1.0})
    assert _arm_is_trusted(passed_anyway) is True
