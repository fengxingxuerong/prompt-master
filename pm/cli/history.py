"""history 子命令：运行历史聚合 + 同任务跨 run 显著性判定。（自 run.py 拆出，逻辑逐字保留）"""

from __future__ import annotations

import argparse
import json
from typing import Any

from .support import log_dir

# --------------------------------------------------------------------------
# --------------------------------------------------------------------------
# history 子命令：运行历史聚合 + 同任务跨 run 显著性判定
#
# 为什么需要：单轮 Δ 受评委噪声影响（实测基线 ±0.5 抖动），"看起来高了 3 分"
# 不等于"真的变好了"。把历次 run 按 task 聚合，Δ 的均值 ± 1.96×SE 置信区间
# 不含 0 时才敢说显著 —— 把结论从叙事变成统计。
# --------------------------------------------------------------------------
_TERMINAL_STATUS = {
    "passed",
    "max_iterations",
    "failed",
    "early_stopped",
    "needs_clarification",
}


def _load_run_history(last: int, include_demo: bool) -> list[dict[str, Any]]:
    """扫描 logs/run_*.json，按修改时间从新到旧收集，抽出聚合所需的最小字段集。

    全量 pytest 的 fake 跑也会往 logs/ 落 run 文件（数量远多于真实运行）：
    所以 demo 过滤必须发生在取样窗口**之内**逐个进行，而不是先截断再过滤——
    否则最近 N 个文件可能全是测试产物，真实运行反而被挤出窗口。
    """
    from datetime import datetime

    rows: list[dict[str, Any]] = []
    files = sorted(log_dir().glob("run_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for f in files:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue  # 损坏的历史文件跳过，不让一颗坏牙毁掉整份体检
        agg = d.get("aggregate") or {}
        base = d.get("baseline_aggregate") or {}
        channels = {str(e.get("channel")) for e in (d.get("trace") or [])}
        is_demo = "fake" in channels
        if is_demo and not include_demo:
            continue
        opt_avg = agg.get("avg_score")
        base_avg = base.get("avg_score")
        rows.append(
            {
                "run_id": d.get("run_id") or f.stem[4:],
                "mtime": datetime.fromtimestamp(f.stat().st_mtime).strftime("%m-%d %H:%M"),
                "task": str(d.get("task") or "")[:40],
                "status": d.get("status"),
                "opt_avg": opt_avg,
                "opt_lower": agg.get("ci_lower"),
                "base_avg": base_avg,
                "delta": (
                    round(opt_avg - base_avg, 2)
                    if isinstance(opt_avg, (int, float)) and isinstance(base_avg, (int, float))
                    else None
                ),
                "llm_calls": d.get("llm_calls", 0),
                "demo": is_demo,
                "noise": agg.get("noise"),
            }
        )
        if len(rows) >= max(0, last):
            break
    return rows


def _history_command(argv: list[str]) -> int:
    import math

    ap = argparse.ArgumentParser(prog="run.py history")
    ap.add_argument("--last", type=int, default=50, help="只看最近 N 条运行（默认 50）")
    ap.add_argument("--include-demo", action="store_true", help="包含演示模式的运行（默认排除）")
    ap.add_argument("--json", action="store_true", help="输出 JSON（智能体消费）")
    ns = ap.parse_args(argv)

    rows = _load_run_history(ns.last, ns.include_demo)

    # 按 task 分组：组内 ≥2 条且都有基线 → Δ 配对统计（置信区间不含 0 = 显著）
    groups: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        if r["delta"] is not None:
            groups.setdefault(r["task"], []).append(r)

    task_stats: list[dict[str, Any]] = []
    for task, rs in groups.items():
        if len(rs) < 2:
            continue
        deltas = [float(r["delta"]) for r in rs]
        n = len(deltas)
        mean = sum(deltas) / n
        if n >= 2:
            var = sum((x - mean) ** 2 for x in deltas) / (n - 1)
            se = math.sqrt(var / n)
        else:
            se = 0.0
        lo, hi = mean - 1.96 * se, mean + 1.96 * se
        significant = n >= 2 and (lo > 0 or hi < 0)
        task_stats.append(
            {
                "task": task,
                "n_runs": n,
                "deltas": deltas,
                "delta_mean": round(mean, 2),
                "delta_ci95": [round(lo, 2), round(hi, 2)],
                "significant": significant,
                "note": "样本少，结论仅供观察" if n < 5 else None,
                "run_ids": [r["run_id"] for r in rs],
            }
        )
    task_stats.sort(key=lambda s: -s["n_runs"])

    if ns.json:
        print(
            json.dumps(
                {
                    "n_runs": len(rows),
                    "runs": rows,
                    "task_groups": task_stats,
                    "method": "同任务 Δ 的均值 ± 1.96×SE；置信区间不含 0 视为显著（小样本粗判）",
                },
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        )
        return 0

    print(f"最近 {len(rows)} 条运行（演示模式{'已包含' if ns.include_demo else '已排除'}）：\n")
    print(
        f"{'日期':<12} {'run_id':<14} {'状态':<16} {'基线':>5} {'优化':>5} {'Δ':>6} {'调用':>5}  任务"
    )
    for r in rows:
        ba = f"{r['base_avg']:.2f}" if isinstance(r["base_avg"], (int, float)) else "  -"
        oa = f"{r['opt_avg']:.2f}" if isinstance(r["opt_avg"], (int, float)) else "  -"
        dl = f"{r['delta']:+.2f}" if r["delta"] is not None else "  -"
        print(
            f"{r['mtime']:<12} {r['run_id']:<14} {r['status']!s:<16} {ba:>5} {oa:>5} {dl:>6} {r['llm_calls']:>5}  {r['task']}"
        )

    if task_stats:
        print("\n同任务跨 run 显著性（Δ 均值 ± 95% CI）：")
        for s in task_stats:
            mark = "✅ 显著" if s["significant"] else "⚠️ 不显著"
            note = f"（{s['note']}）" if s["note"] else ""
            print(
                f"  {mark}  Δ均值 {s['delta_mean']:+.2f}  CI [{s['delta_ci95'][0]:+.2f}, {s['delta_ci95'][1]:+.2f}]"
                f"  n={s['n_runs']} {note}｜{s['task']}"
            )
    else:
        print("\n（暂无同任务 ≥2 次的运行——对同一需求多跑几轮，这里会给出 Δ 的显著性判定）")
    return 0
