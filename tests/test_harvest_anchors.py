"""harvest_anchors 的采集回归：同一输出跨跑批只采一次（id 不重复）。

背景（2026-09-26 实测事故）：同一份输出被两次跑批复现，id 按 out_sha 生成又不去重，
候选文件里出现两条同 id——`load_samples` 的「样本 id 重复」防线拒收整份文件，
校准被卡死，而卡死点距离采集已经隔了一层（用户填完表才发现），排查成本极高。
在采集端堵住，比在防线报错时让人回头查采集正确得多。
"""

from __future__ import annotations

import json
from pathlib import Path

from harvest_anchors import _sha, collect, to_samples


def _run_file(
    tmp_path: Path,
    name: str,
    *,
    task: str,
    output: str,
    channel: str = "real",
    target: str = "deepseek-v3",
) -> None:
    d = {
        "task": task,
        "trace": [{"channel": channel}],
        "evaluations": [
            {
                "test_case_index": 0,
                "weighted_score": 8.2,
                "issues": [],
                "dimension_scores": {},
                "judge": "evaluator",
            }
        ],
        "test_runs": [
            {
                "test_case_index": 0,
                "target_model": target,
                "test_input": "测试输入",
                "output": output,
                "prompt": "P",
            }
        ],
    }
    (tmp_path / name).write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")


def test_collect_dedups_same_output_across_runs(tmp_path: Path) -> None:
    out = "同一份输出" * 20
    _run_file(tmp_path, "run_a.json", task="任务A", output=out)
    _run_file(tmp_path, "run_b.json", task="任务B", output=out)  # 复读：id 必然撞车
    _run_file(tmp_path, "run_c.json", task="任务C", output="另一份输出" * 20)
    rows = collect(tmp_path)
    shas = [r["out_sha"] for r in rows]
    assert len(shas) == len(set(shas)) == 2
    assert shas[0] == _sha(out)  # 保留首次出现（glob 排序 = 旧→新），确定性可复现
    assert rows[0]["source_run"] == "run_a.json"


def test_fake_channel_and_stub_target_never_enter(tmp_path: Path) -> None:
    _run_file(tmp_path, "run_fake.json", task="任务", output="x" * 50, channel="fake")
    _run_file(tmp_path, "run_stub.json", task="任务", output="y" * 50, target="stub")
    assert collect(tmp_path) == []


def test_to_samples_ids_unique_and_unconfirmed(tmp_path: Path) -> None:
    _run_file(tmp_path, "run_a.json", task="任务A", output="输出一" * 30)
    _run_file(tmp_path, "run_b.json", task="任务B", output="输出一" * 30)
    _run_file(tmp_path, "run_c.json", task="任务C", output="输出二" * 30)
    samples = to_samples([dict(r, band="8.0-8.9") for r in collect(tmp_path)])
    ids = [s["id"] for s in samples]
    assert len(ids) == len(set(ids))
    # 采集器永远不填人工分：这是"人工分不许由模型代填"的那条线在代码里的落点
    assert all(s["confirmed"] is False and s["human_score"] is None for s in samples)
