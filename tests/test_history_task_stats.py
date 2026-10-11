"""`_task_stats` 的同任务显著性判定 + `_history_command` 的渲染尾段回归。

为什么单独一个文件：`test_history_mock_filter.py` 钉的是"**哪些臂算 demo / 算崩溃**"
（取样与准入），这里钉的是完全不同的另一半——**准入之后**：

1. `_task_stats`（132-179）：崩溃臂剔除之后，可信臂不足 2 条时不给判定；n<5 时
   必须带"样本少"的警告。这三条是"结论往前多走一步"的地方：
   - 剔到只剩 1 条还报"显著" = 假统计；
   - n=2 报"显著" = 两个点的置信区间宽到没有意义，看着却像结论；
   - 崩溃臂不参与统计却不出现在输出里 = 史料被静默改写（§三十·一的原始缺陷）。
2. `_history_command` 尾段（224-234, 246）：崩臂的**点名**行、空数据时的提示语。
   渲染丢了不算功能坏，但它是用户唯一能看到"有条臂被排除了"的地方。

打桩方式：`PM_LOG_DIR` 是晚绑定的（`pm/cli/support.py` docstring 说明了理由），
所以测试用 monkeypatch 设环境变量 + 落临时 run_*.json，不起真服务、不出网。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pm.cli.history import _history_command, _task_stats


def _run(
    run_id: str,
    task: str,
    delta: float,
    *,
    trusted: bool = True,
) -> dict[str, Any]:
    """按 `_load_run_history` 的形状造一行：delta 字段驱动分组，trusted 驱动准入。"""
    base_avg = 7.0
    return {
        "run_id": run_id,
        "task": task,
        "delta": delta,
        "opt_avg": round(base_avg + delta, 2),
        "base_avg": base_avg,
        "trusted": trusted,
        "status": "passed",
        "demo": False,
    }


# --------------------------------------------------------------------------
# 准入：剔到不够就不给判定
# --------------------------------------------------------------------------
def test_single_trusted_arm_after_exclusion_gets_no_verdict() -> None:
    """崩溃臂剔完只剩 1 条：不给显著性判定。n=1 的 SE 无定义，
    以前这条会走到 `var=(n-1)` 除零 / 或给出无意义的 CI。"""
    rows = [_run("r1", "t", 0.0), _run("r2", "t", 3.0, trusted=False)]
    assert _task_stats(rows) == []


def test_two_trusted_arms_are_judged_but_flagged_as_small_sample() -> None:
    """n=2 照判，但 note 必须是"样本少"。

    注意这里**不能**顺手断言"n=2 一定不显著"：两点样本的 CI 半宽是 0.98×|Δ|，
    只要 Δ 足够大 CI 就不含 0（1.0/3.0 → [0.04, 3.96]，确实显著）。
    n=2 的"可信度"是靠 note 那句话兜的，不是靠统计本身兜——所以 note 才是要钉的那半。
    """
    rows = [_run("r1", "t", 1.0), _run("r2", "t", 3.0)]
    out = _task_stats(rows)
    assert len(out) == 1
    assert out[0]["n_runs"] == 2
    assert out[0]["note"] == "样本少，结论仅供观察"
    assert out[0]["delta_ci95"] == [0.04, 3.96]


def test_zero_variance_group_reports_a_degenerate_interval() -> None:
    """两条 Δ 完全相同时 SE=0，CI 退化成一点 [5.0, 5.0]，不含 0 → 判显著。

    这是 `mean ± 1.96·SE` 的固有性质，不是实现缺陷；但它的**实际含义**远弱于
    字面的"✅ 显著"——所以 mitigation 是 note。把真实行为钉在这里，
    免得有人以为它一定给出"不显著"而改坏判据。
    """
    rows = [_run("r1", "t", 5.0), _run("r2", "t", 5.0)]
    out = _task_stats(rows)[0]
    assert out["delta_ci95"] == [5.0, 5.0]
    assert out["significant"] is True
    assert out["note"] == "样本少，结论仅供观察"


def test_five_arms_drop_the_small_sample_warning() -> None:
    rows = [_run(f"r{i}", "t", 2.0 + i) for i in range(5)]
    out = _task_stats(rows)
    assert out[0]["n_runs"] == 5 and out[0]["note"] is None


def test_arms_without_a_delta_never_form_a_group() -> None:
    """base/opt 有一边缺分（delta=None）的臂：不分组也不参与，否则 task 会被凭空建出来。"""
    bad = _run("r1", "t", 0.0)
    bad["delta"] = None
    assert _task_stats([bad, bad, bad]) == []


def test_tasks_are_grouped_independently() -> None:
    """同 run 数下不同 task 各判各的，不能互相借样本。"""
    rows = [_run("a1", "task-a", 1.0), _run("a2", "task-a", 3.0)]
    rows += [_run("b1", "task-b", 5.0), _run("b2", "task-b", 5.0)]
    out = {s["task"]: s for s in _task_stats(rows)}
    assert set(out) == {"task-a", "task-b"}
    # task-b 的 Δ 恒等 → SE=0 → CI 退化成一点，按"CI 不含 0 即显著"判显著（见上一个测试）
    assert out["task-b"]["significant"] is True
    assert out["task-a"]["significant"] is True


# --------------------------------------------------------------------------
# 崩臂：不进统计，但必须点名
# --------------------------------------------------------------------------
def test_crashed_arm_is_excluded_from_the_mean_but_named_in_the_output() -> None:
    """§三十·一 的原始缺陷：崩臂被静默删掉，Δ 均值从 +1.34 被顶到 +2.15、
    CI 从跨 0 变成不含 0 —— 一个"显著"的结论是被筛出来的。
    现在它必须从均值里消失，同时出现在 excluded_untrusted 里。"""
    rows = [
        _run("keep1", "t", 1.0),
        _run("keep2", "t", 1.2),
        _run("crash", "t", 9.9, trusted=False),
    ]
    out = _task_stats(rows)[0]
    assert out["deltas"] == [1.0, 1.2]  # 崩臂的 9.9 没进统计
    assert out["delta_mean"] == 1.1
    assert out["excluded_untrusted"] == [{"run_id": "crash", "delta": 9.9, "opt_avg": 16.9}]
    assert out["run_ids"] == ["keep1", "keep2"]


def test_significant_verdict_only_when_ci_excludes_zero() -> None:
    rows = [_run("r1", "t", 3.0), _run("r2", "t", 3.2), _run("r3", "t", 3.4)]
    out = _task_stats(rows)[0]
    assert out["significant"] is True
    assert out["delta_ci95"][0] > 0


def test_negative_significant_is_also_significant() -> None:
    """优化反而变差：CI 完全在 0 以下也是显著，不能只认正数那一侧。"""
    rows = [_run("r1", "t", -3.0), _run("r2", "t", -3.2), _run("r3", "t", -3.4)]
    out = _task_stats(rows)[0]
    assert out["significant"] is True and out["delta_ci95"][1] < 0


def test_task_with_exactly_one_armed_run_is_not_a_task_group() -> None:
    """同任务只有 1 条有 Δ 的运行：这个任务根本不成立（没有"跨 run"可比），
    要被整组跳过——而不是生成一个 n=1 的组让别人读到。"""
    rows = [_run("only", "t", 1.0), _run("other", "t2", 2.0)]
    assert _task_stats(rows) == []


# --------------------------------------------------------------------------
# 渲染尾段
# --------------------------------------------------------------------------
def _fake_logs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, runs: list[dict[str, Any]]) -> None:
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
    for i, r in enumerate(runs):
        p = tmp_path / "logs" / f"run_{r['run_id']}.json"
        # mtime 递推 + 时间间隔，避开"同一时间戳刻度两次写不触发重载/取样"的坑
        import os

        p.write_text(json.dumps(r, ensure_ascii=False), encoding="utf-8")
        os.utime(p, (1_700_000_000 + i * 60, 1_700_000_000 + i * 60))
    monkeypatch.setenv("PM_LOG_DIR", str(tmp_path / "logs"))


def _archive(
    run_id: str, task: str, avg: float, base: float, *, trusted: bool = True
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "task": task,
        "status": "passed",
        "aggregate": {
            "avg_score": avg,
            "min_score": min(avg, base),
            "n_passed": 2,
            "untrusted_case_indices": [] if trusted else [0, 1],
        },
        "baseline_aggregate": {"avg_score": base},
        "trace": [{"channel": "plain"}],
        "errors": [],
    }


def test_crashed_arm_gets_its_own_line_in_the_text_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """那份"排除崩溃臂"的说明行要真的印出来。它丢了不报错，
    但它是用户唯一知道有条臂没进统计的途径。"""
    _fake_logs(
        monkeypatch,
        tmp_path,
        [
            _archive("keep1", "上线登录", 8.0, 7.0),
            _archive("keep2", "上线登录", 8.2, 7.0),
            _archive("bad1", "上线登录", 1.0, 7.0, trusted=False),
        ],
    )
    assert _history_command(["--last", "50"]) == 0
    out = capsys.readouterr().out
    assert "同任务跨 run 显著性" in out
    assert "已排除崩溃臂 bad1" in out
    assert "不计入上面的均值" in out  # 说明行的后半句（excluded 的措辞）也要在


def test_a_single_corrupt_archive_does_not_void_the_whole_history(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """logs/ 里有一颗坏牙（写了一半的 JSON）时，体检不能整份报废——
    "让一颗坏牙毁掉整份体检"是 `_load_run_history` 的原话，钉住它。"""
    logs = tmp_path / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "run_broke.json").write_text('{"run_id": "trun', encoding="utf-8")
    _fake_logs(monkeypatch, tmp_path, [_archive("ok1", "上线登录", 8.0, 7.0)])
    assert _history_command(["--last", "50", "--json"]) == 0
    ids = [r["run_id"] for r in json.loads(capsys.readouterr().out)["runs"]]
    assert ids == ["ok1"]


def test_last_truncates_the_window_not_the_filter(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--last N` 截的是**取样窗口**（取够 N 行就 break），demo 过滤在窗口**之内**
    逐个做——顺序反了的话，"最近 N 个文件全是测试产物"会把真实运行全部挤出。

    这里把 demo 放在列表**最后**（`_fake_logs` 的 mtime 随列表顺序递增，
    而 `_load_run_history` 按 mtime 从新到旧取样）——于是 demo 是最新那个文件。
    `--last 1`：filter 先于 break，demo 被跳过、真实运行顶上，窗口仍取到 1 条。
    若实现改成"先截断再过滤"，这里会得到空结果。
    """
    demo = _archive("fake1", "上线登录", 8.0, 7.0)
    demo["trace"] = [{"channel": "fake"}]
    _fake_logs(
        monkeypatch,
        tmp_path,
        [
            _archive("real_old", "上线登录", 8.1, 7.0),
            _archive("real_mid", "上线登录", 8.2, 7.0),
            demo,
        ],
    )
    assert _history_command(["--last", "1", "--json"]) == 0
    ids = [r["run_id"] for r in json.loads(capsys.readouterr().out)["runs"]]
    assert ids == ["real_mid"]


def test_empty_history_prints_guidance_not_a_traceback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _fake_logs(monkeypatch, tmp_path, [])
    assert _history_command(["--last", "50"]) == 0
    out = capsys.readouterr().out
    assert "暂无同任务" in out
    assert "Traceback" not in out


def test_json_output_is_one_machine_readable_object(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--json` 是智能体的消费面：必须是**单个** JSON 对象，且带 method 说明。

    method 不是装饰——它是"结论怎么算出来的"的自证，也是崩溃臂被点名这件事
    在 JSON 侧的对应物。少了它，消费方只拿到数字、拿不到口径。
    """
    _fake_logs(
        monkeypatch,
        tmp_path,
        [
            _archive("keep1", "上线登录", 8.0, 7.0),
            _archive("keep2", "上线登录", 8.2, 7.0),
            _archive("bad1", "上线登录", 1.0, 7.0, trusted=False),
        ],
    )
    assert _history_command(["--last", "50", "--json"]) == 0
    out = capsys.readouterr().out
    payload = json.loads(out)  # 单个对象：解析失败会在这里炸，正是要的
    assert payload["n_runs"] == 3
    assert [g["task"] for g in payload["task_groups"]] == ["上线登录"]
    assert "excluded_untrusted" in payload["method"]
    assert payload["task_groups"][0]["excluded_untrusted"][0]["run_id"] == "bad1"
    assert payload["runs"][0]["trusted"] is False or True  # 字段存在即可，信任度点名不靠它


def test_demo_runs_are_excluded_from_the_history_load(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """全量 pytest 的 fake 跑也往 logs/ 落 run 文件（数量远多于真实运行）：
    默认必须排除，否则最近 50 条可能全是测试产物，真实运行被挤出窗口。
    `--include-demo` 才放进来。"""
    base = _archive("fake1", "上线登录", 8.0, 7.0)
    base["trace"] = [{"channel": "fake"}]
    _fake_logs(monkeypatch, tmp_path, [base, _archive("real1", "上线登录", 8.1, 7.0)])
    assert _history_command(["--last", "50", "--json"]) == 0
    ids = [r["run_id"] for r in json.loads(capsys.readouterr().out)["runs"]]
    assert ids == ["real1"], ids
    assert _history_command(["--last", "50", "--json", "--include-demo"]) == 0
    ids = [r["run_id"] for r in json.loads(capsys.readouterr().out)["runs"]]
    assert set(ids) == {"real1", "fake1"}
