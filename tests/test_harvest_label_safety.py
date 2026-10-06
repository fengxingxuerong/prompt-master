"""harvest_anchors 的采集安全回归：去重集要覆盖所有锚点文件、默认路径要真跑得通、
带人工分的目标文件不许被静默覆盖。

三条都是实测出来的问题（2026-10-06）：
1. `DEFAULT_EXISTING` 只写了 samples.json 与 samples_disputed.json，**恰好漏掉
   samples.candidates.json** —— 那正是 48 条已确认人工分住的地方。后果有两层：
   重采会把已标过的同一份输出再摊到人面前（实测 25 条里 18 条重复），
   而默认 `--out` 就是那个文件，跑一次"再补一轮"会把 48 条人工分整份覆盖。
2. 加判别力那轮改 CLI 时，我把 `log_dir = Path(args.log_dir)` 一行漏掉了，
   默认路径直接 NameError —— 而 1479 条用例**没有一条打到 harvest 的默认路径**。
   本文件第 1 条就是为这类"入口坏了、测试全绿"补的。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import harvest_anchors as H


def _write_run(log_dir: Path, name: str, task: str, output: str, score: float) -> None:
    """造一份能被 collect() 认作"真端点产出"的 run 归档。"""
    run = {
        "task": task,
        "context": "上下文",
        "trace": [{"node": "execute", "channel": "plain"}],
        "evaluations": [
            {
                "test_case_index": 0,
                "weighted_score": score,
                "dimension_scores": {"task_completion": score},
                "issues": [],
                "model_reported_score": score,
                "judge": "evaluator",
            }
        ],
        "test_runs": [
            {
                "test_case_index": 0,
                "target_model": "glm-5.2",
                "output": output,
                "test_input": f"{task} 的输入",
                "prompt": f"{task} 的提示词正文，够长以通过校验。" * 4,
            }
        ],
    }
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / name).write_text(json.dumps(run, ensure_ascii=False), encoding="utf-8")


def _anchor_file(path: Path, out_id: str, output: str, *, labeled: bool) -> None:
    item: dict[str, Any] = {
        "id": out_id,
        "original_task": "既有任务",
        "prompt": "既有提示词",
        "test_input": "既有输入",
        "test_output": output,
        "confirmed": labeled,
        "human_score": 7.0 if labeled else None,
        "provenance": {"human_score_source": "owner" if labeled else "unlabeled"},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([item], ensure_ascii=False), encoding="utf-8")


def _argv(ns: dict[str, str]) -> list[str]:
    out = ["harvest_anchors.py"]
    for k, v in ns.items():
        key = f"--{k.replace('_', '-')}"
        out += [key, v] if v else [key]
    return out


def _setup(tmp_path: Path) -> tuple[Path, Path, Path]:
    logs = tmp_path / "logs"
    _write_run(
        logs, "run_aaaa.json", "把工单按优先级排队", "输出甲：一段有实质内容的交付物" * 3, 8.6
    )
    _write_run(
        logs, "run_bbbb.json", "把周报整理成三段", "输出乙：另一段有实质内容的交付物" * 3, 9.4
    )
    anchors = tmp_path / "judge_calibration"
    anchors.mkdir(parents=True, exist_ok=True)
    return logs, anchors, tmp_path


def _run(
    monkeypatch: Any,
    tmp_path: Path,
    logs: Path,
    out: Path,
    review: Path,
    flags: tuple[str, ...] = (),
) -> int:
    """走真实 CLI 入口。`flags` 只放 store_true 开关，其余用 --key value 传。"""
    monkeypatch.setattr(H, "ROOT", tmp_path)
    argv = [
        "harvest_anchors.py",
        "--log-dir",
        str(logs),
        "--out",
        str(out),
        "--review",
        str(review),
        "--per-band",
        "5",
        *flags,
    ]
    monkeypatch.setattr("sys.argv", argv)
    return H.main()


# --------------------------------------------------------------- 1. 默认路径必须真能跑


def test_default_harvest_path_runs_end_to_end(monkeypatch: Any, tmp_path: Path) -> None:
    """漏一行 `log_dir = Path(args.log_dir)` 时这条必须红（实测就漏过）。

    它不打 --coverage 分支：那正是上次坏掉的那条路。
    """
    logs, _anchors, _ = _setup(tmp_path)
    out, review = tmp_path / "cand.json", tmp_path / "REVIEW.md"
    rc = _run(monkeypatch, tmp_path, logs, out, review)
    assert rc == 0, f"默认采集路径退出码 {rc}（0 才算跑通）"
    rows = json.loads(out.read_text(encoding="utf-8"))
    assert len(rows) == 2, "两条真端点输出都该进候选"
    assert review.exists() and "人工标签格子覆盖度" in review.read_text(encoding="utf-8")
    assert all(r["confirmed"] is False and r["human_score"] is None for r in rows), (
        "候选不得自带人工分"
    )


# ------------------------------------------------- 2. 去重集必须覆盖所有锚点文件


def test_already_labeled_output_is_not_re_proposed(monkeypatch: Any, tmp_path: Path) -> None:
    """实测缺陷：候选文件里的 48 条已标锚点不在去重集里，重采 25 条里 18 条是重复的。

    最贵的资源是所有者肯打的那点分，把已标过的再摊一遍等于把它浪费掉。
    """
    logs, anchors, _ = _setup(tmp_path)
    dup_out = "输出甲：一段有实质内容的交付物" * 3
    _anchor_file(anchors / "samples.candidates.json", "h-aaa", dup_out, labeled=True)
    out, review = tmp_path / "cand.json", tmp_path / "REVIEW.md"
    assert _run(monkeypatch, tmp_path, logs, out, review) == 0
    rows = json.loads(out.read_text(encoding="utf-8"))
    assert [r["test_output"] for r in rows] == ["输出乙：另一段有实质内容的交付物" * 3], (
        "已确认锚点的那份输出必须被去重掉，无论它住在哪个样本文件里"
    )


def test_anchor_files_enumeration_skips_the_example(tmp_path: Path) -> None:
    _anchor_file(tmp_path / "samples.example.json", "x", "示例输出", labeled=False)
    _anchor_file(tmp_path / "samples.json", "y", "真实输出", labeled=True)
    names = [p.name for p in H.anchor_files(tmp_path)]
    assert names == ["samples.json"], "示例文件里的假人工分不能进统计"


# ------------------------------------------------------------ 3. 覆盖写保护


def test_refuses_to_overwrite_a_labeled_anchor_file(monkeypatch: Any, tmp_path: Path) -> None:
    logs, anchors, _ = _setup(tmp_path)
    target = anchors / "samples.candidates.json"
    _anchor_file(target, "h-keep", "不能丢的人工分输出" * 4, labeled=True)
    before = target.read_bytes()
    rc = _run(monkeypatch, tmp_path, logs, target, tmp_path / "REVIEW.md")
    assert rc == 2, f"覆盖已确认锚点必须显式拒绝（退出码 2），实得 {rc}"
    assert target.read_bytes() == before, "拒绝路径上不许碰文件"


def test_explicit_flag_allows_the_overwrite(monkeypatch: Any, tmp_path: Path) -> None:
    logs, anchors, _ = _setup(tmp_path)
    target = anchors / "samples.candidates.json"
    _anchor_file(target, "h-keep", "不能丢的人工分输出" * 4, labeled=True)
    rc = _run(
        monkeypatch,
        tmp_path,
        logs,
        target,
        tmp_path / "REVIEW.md",
        ("--allow-overwrite-labeled",),
    )
    assert rc == 0
    assert json.loads(target.read_text(encoding="utf-8"))[0]["confirmed"] is False


def test_count_labeled_ignores_unscored_candidates(tmp_path: Path) -> None:
    _anchor_file(tmp_path / "pending.json", "h1", "输出", labeled=False)
    assert H.count_labeled(tmp_path / "pending.json") == 0
    _anchor_file(tmp_path / "pending.json", "h1", "输出", labeled=True)
    assert H.count_labeled(tmp_path / "pending.json") == 1
    assert H.count_labeled(tmp_path / "不存在.json") == 0
