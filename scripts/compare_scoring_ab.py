"""A/B 对照：同一批锚点上「印象式 vs 判定式」的复现性与准确性配对比较。

为什么不直接看 `run.py calibrate` 打印的读数：实测同一命令、同一批 11 条锚点重跑两次，
`rep_range_max` 是 2.2 与 0.5、`rep_range_mean` 是 0.45 与 0.22 —— 极差的最大值本身就是
一次运气读数（双峰下"某条锚点在这 3 次窗口里翻不翻"是随机的）。拿单次 max 当判据
等于拿骰子点数当结论。所以这里做三件事：

1. **配对**：只比两臂都成功打出分的锚点。上游 500 会让两臂各丢几条，
   不配对的话"判定式更稳"可能只是因为这次它恰好分到几条简单的；
2. **每锚点极差**：把逐条 range 摊开看，而不是只报均值——均值会把"半数锚点稳、
   半数锚点疯"抹平成一个好数字；
3. **给出预写判据的结论**：判定式的极差均值要比印象式低出噪声地板（默认 0.25，
   来自上面那两次同命令重跑的散布），且越线（>阈值）条数不升；否则判"未测出改进"。

用法：
    # 从两次 `run.py calibrate --json` 的日志输出配对（原始方式）
    python scripts/compare_scoring_ab.py <impression 的 --json 输出> <checklist 的 --json 输出>

    # 直接从校准账本配对（推荐：明细已在账本里，不必留着两份日志文件）
    python scripts/compare_scoring_ab.py --ledger logs/judge_calibration_history.json
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path
from typing import Any

DEFAULT_THRESHOLD = 2.0  # 与 PM_JUDGE_DISAGREEMENT 同口径
DEFAULT_NOISE_FLOOR = 0.25  # 同命令重跑的 rep_range_mean 散布（实测 0.45 vs 0.22）


def load_run(path: Path) -> dict[str, Any]:
    """从合并了 stderr 的日志里挖出那一段 JSON 报告。

    `--json` 的输出是单个缩进对象，但同一个文件里还混着端点降级警告（每行以
    `[evaluator]` 开头）。用 raw_decode 从第一个独立成行的 `{` 起解，
    比"整文件 json.loads"耐操，也比按行号切更不容易被日志长度变化打脸。
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    start = next((i for i, ln in enumerate(lines) if ln.strip() == "{"), None)
    if start is None:
        raise ValueError(f"{path}：没找到 JSON 起始行（这一臂可能根本没跑完）")
    obj, _end = json.JSONDecoder().raw_decode("\n".join(lines[start:]))
    if not isinstance(obj, dict) or "analysis" not in obj:
        raise ValueError(f"{path}：解出来的对象没有 analysis 字段")
    return obj


def from_ledger(path: Path, mode: str) -> dict[str, Any]:
    """从校准账本取该模式**最近一条有逐条明细的**记录，包装成与 `--json` 输出同形的对象。

    `items` / `rep_items` 是 2026-09-25 才开始入账的。那之前的记录只有聚合数，
    配对根本无从做起 —— 这时要说"这轮 A/B 不能归因、得重跑"，而不是拿两个分母不同的
    聚合数硬算差值（当晚 impression n=18 / checklist n=15 就卡在这个状态：
    明明知道两臂不同卷，却查不出判定式臂丢了哪 3 条）。
    """
    recs = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(recs, list):
        raise ValueError(f"{path}：账本不是数组")
    same = [r for r in recs if r.get("mode", "impression") == mode]
    if not same:
        raise ValueError(f"{path}：账本里没有 mode={mode} 的记录")
    detailed = [r for r in same if r.get("items") or r.get("rep_items")]
    if not detailed:
        raise ValueError(
            f"mode={mode} 的 {len(same)} 条记录都没有逐条明细（items/rep_items 自 2026-09-25 起入账）"
            f"⇒ 无法配对，只能重跑：python run.py calibrate --scoring-mode {mode} --repeat 3 --json"
        )
    r = detailed[-1]
    return {
        "analysis": {
            **{k: r[k] for k in ("n", "bias", "mae", "r", "rho") if k in r},
            "pairs": r.get("items") or [],
            "repeatability": {"per_item": r.get("rep_items") or []},
        },
        "_ts": r.get("ts"),
        "_n_failed": r.get("n_failed"),
        "_anchors": r.get("anchors"),
    }


def ranges_of(run: dict[str, Any]) -> dict[str, float]:
    """每锚点的自我极差。没打完 3 次的条目不在里面（它没有可信的 range）。"""
    rep = (run.get("analysis") or {}).get("repeatability") or {}
    out: dict[str, float] = {}
    for item in rep.get("per_item") or []:
        if isinstance(item.get("range"), (int, float)) and int(item.get("n") or 0) >= 2:
            out[str(item["id"])] = float(item["range"])
    return out


def scores_of(run: dict[str, Any]) -> dict[str, dict[str, float]]:
    """每锚点的人工分与评委分（单发那一段，来自 analysis.pairs）。"""
    out: dict[str, dict[str, float]] = {}
    for p in (run.get("analysis") or {}).get("pairs") or []:
        out[str(p["id"])] = {"human": float(p["human"]), "judge": float(p["judge"])}
    return out


def summarize(ranges: dict[str, float], threshold: float) -> dict[str, Any]:
    vals = list(ranges.values())
    if not vals:
        return {"n": 0, "mean": None, "max": None, "exceed": 0}
    return {
        "n": len(vals),
        "mean": round(statistics.fmean(vals), 3),
        "max": round(max(vals), 2),
        "median": round(statistics.median(vals), 3),
        "exceed": sum(1 for v in vals if v > threshold),
        "worst": max(ranges, key=lambda k: ranges[k]),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="两种评分协议的配对 A/B")
    ap.add_argument("impression", type=Path, nargs="?", help="impression 臂的 --json 输出日志")
    ap.add_argument("checklist", type=Path, nargs="?", help="checklist 臂的 --json 输出日志")
    ap.add_argument(
        "--ledger",
        type=Path,
        default=None,
        help="改为直接从校准账本配对（logs/judge_calibration_history.json）",
    )
    ap.add_argument("--a-mode", default="impression", choices=("impression", "checklist"))
    ap.add_argument("--b-mode", default="checklist", choices=("impression", "checklist"))
    ap.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    ap.add_argument("--noise-floor", type=float, default=DEFAULT_NOISE_FLOOR)
    args = ap.parse_args()

    try:
        if args.ledger:
            a = from_ledger(args.ledger, args.a_mode)
            b = from_ledger(args.ledger, args.b_mode)
            if a.get("_anchors") and b.get("_anchors") and a["_anchors"] != b["_anchors"]:
                # 配对是按 id 求交集，理论上能兜住；但锚点集指纹都不同还硬比，
                # 十有八九是拿两批不同考卷的幸存者凑数——先拦下来问清楚
                raise ValueError(
                    f"两臂锚点集指纹不同（{a['_anchors']} vs {b['_anchors']}），不是同一批考卷"
                )
        else:
            if not (args.impression and args.checklist):
                ap.error("要么给两个 --json 日志文件，要么给 --ledger <账本路径>")
            a = load_run(args.impression)
            b = load_run(args.checklist)
    except (OSError, ValueError, json.JSONDecodeError) as e:
        # 读不懂就明确退出，别把"这一臂没跑完"演算成"两臂都没极差"再输出一个 0.00 的降幅
        print(f"输入不可用：{e}", file=sys.stderr)
        return 2
    if a.get("_ts") or b.get("_ts"):
        print(
            f"[账本] {args.a_mode} @{a.get('_ts')}（n_failed={a.get('_n_failed')}） / "
            f"{args.b_mode} @{b.get('_ts')}（n_failed={b.get('_n_failed')}）"
        )
    ra, rb = ranges_of(a), ranges_of(b)
    sa, sb = summarize(ra, args.threshold), summarize(rb, args.threshold)

    print("## 单臂读数（每锚点极差，各自全部成功条目）")
    for name, s in (("impression", sa), ("checklist", sb)):
        print(f"- {name}: n={s['n']} 均值={s['mean']} 中位={s.get('median')} 最大={s['max']}")
        print(f"  越线(>{args.threshold})={s['exceed']} 条，最差={s.get('worst')}")

    paired = sorted(set(ra) & set(rb))
    if not paired:
        print("\n无配对锚点（两臂没有一条同时成功）——这轮 A/B 作废，得重跑")
        return 1
    pa = summarize({k: ra[k] for k in paired}, args.threshold)
    pb = summarize({k: rb[k] for k in paired}, args.threshold)
    diffs = [ra[k] - rb[k] for k in paired]
    better = sum(1 for d in diffs if d > 0)
    worse = sum(1 for d in diffs if d < 0)

    print(f"\n## 配对比较（两臂都成功的 {len(paired)} 条）")
    print(f"- impression 均值={pa['mean']} 越线={pa['exceed']}")
    print(f"- checklist  均值={pb['mean']} 越线={pb['exceed']}")
    print(f"- 均值差（impression − checklist）= {round(pa['mean'] - pb['mean'], 3)}")
    print(
        f"- 逐条：判定式更稳 {better} 条 / 更抖 {worse} 条 / 持平 {len(paired) - better - worse} 条"
    )

    # 准确性：配对集上的 |评委−人工|。稳定性赢了但把分打歪，等于换了把稳的尺去量错的东西。
    # ⚠️ 配对集**不能沿用上面那个"两臂都跑满 N 次重复"的交集**：重复测量比单发更容易被
    # 上游 500/空内容打掉（实测单发两臂各有 18/16 条成功，而跑满 3 次重复的交集只剩 8 条）。
    # 拿 8 条算 MAE 会把样本量悄悄砍掉一半 —— 稳定性按"都跑满重复"配对，
    # 准确性按"都打出过单发分"配对，两个集合各自如实报出条数。
    s_a, s_b = scores_of(a), scores_of(b)
    both = [k for k in paired if k in s_a and k in s_b]  # 稳定性配对集（两臂都跑满重复）
    acc_pair = sorted(set(s_a) & set(s_b))  # 准确性配对集（两臂都有单发分，通常更大）
    if acc_pair:
        mae_a = statistics.fmean(abs(s_a[k]["judge"] - s_a[k]["human"]) for k in acc_pair)
        mae_b = statistics.fmean(abs(s_b[k]["judge"] - s_b[k]["human"]) for k in acc_pair)
        bias_a = statistics.fmean(s_a[k]["judge"] - s_a[k]["human"] for k in acc_pair)
        bias_b = statistics.fmean(s_b[k]["judge"] - s_b[k]["human"] for k in acc_pair)
        print(
            f"\n## 准确性配对（两臂都有单发分的 {len(acc_pair)} 条；其中跑满重复的 {len(both)} 条）"
        )
        print(f"- MAE：impression {mae_a:.2f} → checklist {mae_b:.2f}")
        print(f"- bias：impression {bias_a:+.2f} → checklist {bias_b:+.2f}（正=偏松）")
        flip_a = sum(1 for k in acc_pair if (s_a[k]["judge"] >= 8.0) != (s_a[k]["human"] >= 8.0))
        flip_b = sum(1 for k in acc_pair if (s_b[k]["judge"] >= 8.0) != (s_b[k]["human"] >= 8.0))
        print(f"- 过线判断与人工相反：impression {flip_a} 条 → checklist {flip_b} 条")
        if len(acc_pair) > len(both):
            print("- ⚠️ 两个配对集大小不同是刻意的：稳定性只用跑满重复的，准确性用单发都成功的。")

    gain = (pa["mean"] or 0) - (pb["mean"] or 0)
    ok_mean = gain > args.noise_floor
    ok_exceed = pb["exceed"] <= pa["exceed"]
    print("\n## 判据（跑前预写：均值降幅 > 噪声地板，且越线条数不升）")
    print(f"- 降幅 {gain:.3f} vs 噪声地板 {args.noise_floor} → {'满足' if ok_mean else '不满足'}")
    print(f"- 越线 {pa['exceed']} → {pb['exceed']} → {'满足' if ok_exceed else '不满足'}")
    verdict = (
        "判定式胜出，可议改默认" if ok_mean and ok_exceed else "未测出改进：默认保持 impression"
    )
    print(f"- 结论：**{verdict}**")
    if not (ok_mean and ok_exceed) and gain > 0:
        print(
            f"  注：方向是好的（降 {gain:.2f}），但**没超出这把量具自己的读数散布**"
            f"（同命令重跑实测 0.45 / 0.22）。要说'真的更稳'得加重复次数或加锚点，"
            "不是把判据调低。"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
