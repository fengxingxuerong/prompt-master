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


def _arm_is_trusted(d: dict[str, Any]) -> bool:
    """这一条臂的数据本身可不可信（与"噪声带测不测得出来"无关）。

    新归档直接读 `untrusted_case_indices`（§三十·一 那个字段）；
    旧归档没有它，按实况回推 —— 平均分触到量纲下限 1.0 且一条都没通过，
    那不是一次差测量，是一次失败（连接断了 / 输出解析不出来）。
    """
    flagged = (d.get("aggregate") or {}).get("untrusted_case_indices")
    if isinstance(flagged, list):
        return not flagged
    agg = d.get("aggregate") or {}
    avg = agg.get("avg_score")
    return not (isinstance(avg, (int, float)) and avg <= 1.0 and not agg.get("n_passed"))


def _is_mock_run(d: dict[str, Any]) -> bool:
    """这一条是不是"用例生成降级"的假跑（§三十·二）。

    判据只有 `errors` 的 `mock:` 前缀，因为那是 `pm/nodes/execute.py` 里
    **唯一**会把用例来源标成不合格的地方（降级兜底 / 条数不够 / 场景覆盖缺失）。

    ⚠️ 原来这里判的是 `trace` 里的 channel 含不含 `"fake"`，那是 pytest 的
    fake 后端留下的痕迹，而 mock 跑走的是真实通道名（plain/json_fallback/
    function_calling），一个都匹配不上。实测：36 条 mock 臂**全部**漏判，
    混进 Δ 的统计当成了真运行 —— 其中一条还把 36 条 Δ 撑成全 0.0。

    补这一条不靠猜：判据来自写出该标记的那三行，字段名与前缀逐字照抄。
    """
    return any(str(e).startswith("mock:") for e in (d.get("errors") or []))


def _load_run_history(last: int, include_demo: bool) -> list[dict[str, Any]]:
    """扫描 logs/run_*.json，按修改时间从新到旧收集，抽出聚合所需的最小字段集。

    全量 pytest 的 fake 跑也会往 logs/ 落 run 文件（数量远多于真实运行）：
    所以 demo 过滤必须发生在取样窗口**之内**逐个进行，而不是先截断再过滤——
    否则最近 N 个文件可能全是测试产物，真实运行反而被挤出窗口。

    §三十·一：`noise_measurable` **不参与**取样过滤。实测它会把崩掉的臂
    （连接失败 / 结构化输出全崩，四用例全 1.0、极差全 0）判成"不可估"而
    一并删掉，而那正是 Δ 最差的两条臂 —— 筛掉的不是噪声，是结论。
    崩掉的臂改由 `trusted` 字段**点名**，让引用的人看见并自己决定。
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
        is_demo = "fake" in channels or _is_mock_run(d)
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
                # §三十·一：点名崩掉的臂，而不是靠 `noise_measurable` 顺手删掉。
                # 取不到旧字段（这些归档早于该字段）时按实况回推：平均分触底且一条没过。
                "trusted": _arm_is_trusted(d),
            }
        )
        if len(rows) >= max(0, last):
            break
    return rows


def _task_stats(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按 task 分组做 Δ 的配对统计：均值 ± 1.96×SE，置信区间不含 0 才算显著。

    独立成函数是因为它是"**哪些臂计入**"这个决定（§三十·一），而
    `_history_command` 只该管渲染。两件事挤在一个函数里时，
    渲染上的任何分支都会顶高统计那段的复杂度台账。
    """
    import math

    groups: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        if r["delta"] is not None:
            groups.setdefault(r["task"], []).append(r)

    task_stats: list[dict[str, Any]] = []
    for task, rs in groups.items():
        if len(rs) < 2:
            continue
        deltas = [float(r["delta"]) for r in rs if r["trusted"]]
        n = len(deltas)
        if n < 2:
            continue  # 崩的臂被点名之后，可信臂不足 2 条就不给显著性判定
        mean = sum(deltas) / n
        var = sum((x - mean) ** 2 for x in deltas) / (n - 1)
        se = math.sqrt(var / n)
        lo, hi = mean - 1.96 * se, mean + 1.96 * se
        task_stats.append(
            {
                "task": task,
                "n_runs": n,
                "deltas": deltas,
                "delta_mean": round(mean, 2),
                "delta_ci95": [round(lo, 2), round(hi, 2)],
                "significant": lo > 0 or hi < 0,
                # §三十·一：崩掉的臂**不参与**统计，但必须**点名**。
                # 曾经它们被"噪声不可估"这条过滤静默删掉（见 `_load_run_history`），
                # 于是 Δ 均值从 +1.34 顶到 +2.15、CI 从跨 0 变成不含 0 ——
                # 一个"显著"的结论是被筛出来的。现在它明写在输出里。
                "excluded_untrusted": [
                    {"run_id": r["run_id"], "delta": r["delta"], "opt_avg": r["opt_avg"]}
                    for r in rs
                    if not r["trusted"]
                ],
                "note": "样本少，结论仅供观察" if n < 5 else None,
                "run_ids": [r["run_id"] for r in rs if r["trusted"]],
            }
        )
    return task_stats


def _history_command(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="run.py history")
    ap.add_argument("--last", type=int, default=50, help="只看最近 N 条运行（默认 50）")
    ap.add_argument("--include-demo", action="store_true", help="包含演示模式的运行（默认排除）")
    ap.add_argument("--json", action="store_true", help="输出 JSON（智能体消费）")
    ns = ap.parse_args(argv)

    rows = _load_run_history(ns.last, ns.include_demo)
    task_stats = _task_stats(rows)
    task_stats.sort(key=lambda s: -s["n_runs"])

    if ns.json:
        print(
            json.dumps(
                {
                    "n_runs": len(rows),
                    "runs": rows,
                    "task_groups": task_stats,
                    "method": "同任务 Δ 的均值 ± 1.96×SE；置信区间不含 0 视为显著（小样本粗判）。"
                    "崩溃臂（连接失败/输出不可解析，均分触底）不计入统计但在 "
                    "`task_groups[].excluded_untrusted` 里逐条点名（§三十·一）",
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
            # §三十·一：崩掉的臂被排除时必须说出来，不能只报排除后的漂亮数字
            for e in s["excluded_untrusted"]:
                print(_excluded_line(e))
    else:
        print("\n（暂无同任务 ≥2 次的有效运行——对同一需求多跑几轮，这里会给出 Δ 的显著性判定）")
    return 0


def _excluded_line(e: dict[str, Any]) -> str:
    """排除说明那一行。

    独立成函数是为了不把 `_history_command` 的圈复杂度顶过 CC 10 ——
    渲染细节不该和"哪些臂计入统计"那个决定挤在同一个函数里。
    """
    return (
        f"      ⚠️ 已排除崩溃臂 {e['run_id']}（优化后均分 {e['opt_avg']}、"
        f"Δ {e['delta']:+}）—— 不计入上面的均值，列在这儿是为了不让你以为它没跑过"
    )
