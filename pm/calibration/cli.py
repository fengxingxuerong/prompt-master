"""`python -m pm.calibration` 与 `run.py calibrate` 共用的 argparse 入口。

2026-10-19 从 `pm/calibration.py` 拆出：CLI 只做参数解析与转发，判据都在别的模块里。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .paths import DEFAULT_SAMPLES, EXAMPLE_SAMPLES, JUDGE_ROLES


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def main() -> int:
    # 延迟 import，且**从包命名空间取名**（`from . import X` 而不是 `from .samples import X`）。
    # 为什么：本仓的排障与回归测试一律在 `pm.calibration.X` 上做替换
    # （见 tests/test_calibration_main_entry.py 与 test_calibration_aggregate.py）。
    # 若在模块顶层 `from .samples import load_samples`，名字会被焊死在 cli 的命名空间里，
    # 包上的补丁**静默失效** —— 测试变绿但改的是另一份函数。拆包前单文件时代没有这个坑。
    from . import (
        calibrate,
        load_samples,
        render_provenance,
        render_repeatability,
        render_report,
        repeatability,
    )

    ap = argparse.ArgumentParser(description="评委校准：锚点样本上对比评委分与人工分")
    ap.add_argument("--samples", default=str(DEFAULT_SAMPLES), help="锚点样本 JSON 路径")
    ap.add_argument(
        "--judge", choices=list(JUDGE_ROLES), default="evaluator", help="要校准的评委角色"
    )
    ap.add_argument(
        "--repeat",
        type=int,
        default=1,
        help="同一份输入连续打 N 次，量评委与**自己**的一致性（绕开评估缓存）。"
        "代价是 N 倍调用；1=不测",
    )
    ap.add_argument(
        "--scoring-mode",
        choices=("impression", "checklist"),
        default="impression",
        help="impression=现行五个 1-10 整数；checklist=二值判定 + 代码算分（两套协议的 A/B 入口）",
    )
    ap.add_argument(
        "--write-template",
        action="store_true",
        help=f"从示例 {EXAMPLE_SAMPLES.name} 生成样本文件（已存在则跳过）",
    )
    args = ap.parse_args()

    if args.write_template:
        target = Path(args.samples)
        if target.exists():
            print(f"已存在，跳过：{target}")
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(EXAMPLE_SAMPLES.read_text(encoding="utf-8"), encoding="utf-8")
            print(f"已生成样本模板：{target}")
            print("人工核对每条的 human_score（1-10）后重新运行本命令（不带 --write-template）。")
        return 0

    try:
        samples = load_samples(Path(args.samples))
    except (OSError, ValueError, json.JSONDecodeError) as e:
        print(f"样本加载失败：{e}", file=sys.stderr)
        return 2
    if not samples:
        n_pending = len(getattr(samples, "pending", []))
        hint = (
            f"（{n_pending} 条候选全部未人工确认——把 confirmed 改成 true 并填 human_score 再跑）"
            if n_pending
            else "（先用 --write-template 生成模板）"
        )
        print(f"样本为空：{args.samples}{hint}", file=sys.stderr)
        return 2
    if getattr(samples, "pending", None):
        print(
            render_provenance(len(samples) + len(samples.pending), len(samples.pending)),
            file=sys.stderr,
        )

    analysis, errors = calibrate(samples, args.judge, args.scoring_mode)
    for sid, err in errors:
        print(f"  [SKIP] {sid}：{err}", file=sys.stderr)
    if not analysis:
        print("全部锚点评估失败，无法校准（先跑 run.py --preflight 检查端点）", file=sys.stderr)
        return 1
    print(render_report(args.judge, analysis))
    if args.repeat >= 2:
        rep = repeatability(samples, args.judge, args.repeat, args.scoring_mode)
        analysis["repeatability"] = rep
        print(render_repeatability(rep))
    return 0
