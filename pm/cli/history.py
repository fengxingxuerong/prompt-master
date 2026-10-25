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

# 没有评委配置记录的归档归到这一桶，**不与任何已知代合并**——
# "没记录"不等于"同一把尺子"，把两者并起来正是 §十八 那类"代码里分不出实测与配置"的翻版。
UNKNOWN_GENERATION = "（未记录评委配置）"


def _ruler_generation(d: dict[str, Any]) -> str:
    """这一轮到底用哪把尺子打的分——评委席的模型身份（§十八 / §三十一）。

    为什么 history 也要分代：Δ 虽然在同一次 run 内配对，但**换评委 = 换算尺的刻度与噪声**
    （flash-lite 把读数挤成常数、放水 +3.7，deepseek-v4-flash 的 MAE 0.88），跨代的 Δ
    塞进同一个均值，读出来的就是两把尺的算术平均，不代表任何一把。`calibrate --aggregate`
    早已按 `model×rubric` 分代（§三十一），这一侧一直只按 task 字符串分组——本函数补上缺口。

    身份取自 `judge_health.models`（`pm.nodes.judge._judge_health` 落的 state，run_*.json 里有）；
    按角色名排序拼串，与字典写入顺序无关。取不到就归 UNKNOWN。
    """
    jh = d.get("judge_health")
    models = jh.get("models") if isinstance(jh, dict) else None
    if not isinstance(models, dict) or not models:
        return UNKNOWN_GENERATION
    return "|".join(f"{role}={models[role]}" for role in sorted(models))


def _group_key(r: dict[str, Any]) -> tuple[str, str]:
    """分组键：**同一任务且同一把尺子**才配对。缺 `judge_gen` 的调用方（脚本 / 单测）按未知代。"""
    return (r["task"], str(r.get("judge_gen") or UNKNOWN_GENERATION))


def _arm_is_trusted(d: dict[str, Any]) -> bool:
    """这一条臂的数据本身可不可信（与"噪声带测不测得出来"无关）。

    新归档直接读 `untrusted_case_indices`（§三十·一 那个字段）；
    旧归档没有它，按实况回推。

    ⚠️ 回推判据试过两版，都不够：

    - 第一版"均分 ≤ 1.0"只抓得住**全**崩的那条（`7d87c5065c55`，四用例全 1.0），
      漏掉**半崩**的 `1b234ac88efa`（两条评 1.0、一条 6.7，均分被拉到 2.9）。
    - 第二版"`n_passed == 0` 且均分 ≤ 5.0"更糟：`a43e44adcc9f` 是
      **一条都没通过**（n_passed=0，均分 7.83），但它的用例分是
      `[7.81, 4.0, 4.0, 4.0]` —— 那是一次真实测量（优化确实没提上去），
      却被判成崩溃。**判据吃掉了我们要看的信号。**

    能分开的是**有没有用例真的触到量纲下限**：`min_score` 就是那个下限，
    评委只在没有可评的东西（连接断了 / 输出为空）时才给 1.0。
    崩掉的臂有 4/4 或 2/3 条触底，真实臂一条都没有。

    所以判据是 `min_score` 触底 **且** 一条都没通过 ——
    只触底不通过的 `a43e44adcc9f` 留得住（它有 7.81 分的用例），全崩的两条抓得住。
    """
    flagged = (d.get("aggregate") or {}).get("untrusted_case_indices")
    if isinstance(flagged, list):
        return not flagged
    agg = d.get("aggregate") or {}
    floor = agg.get("min_score")
    if not isinstance(floor, (int, float)):
        return True  # 没有下限可判，交给别的闸
    return not (floor <= 1.0 and not agg.get("n_passed"))


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
                # 这一轮的尺子身份（§三十一）：Δ 只有在同一把尺上才比得。
                "judge_gen": _ruler_generation(d),
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


# 小样本必须用 t 分位而不是 1.96（2026-10-31 修）。1.96 是**大样本**正太分位，
# 在 n=2~4 时会把区间算窄到近乎一半，于是"两三条臂碰巧同向"就被判成显著。
# 表只列到 df=30，更大的按 1.96（与正太渐近一致）。
_T_CRIT_95: dict[int, float] = {
    1: 12.706,
    2: 4.303,
    3: 3.182,
    4: 2.776,
    5: 2.571,
    6: 2.447,
    7: 2.365,
    8: 2.306,
    9: 2.262,
    10: 2.228,
    11: 2.201,
    12: 2.179,
    13: 2.160,
    14: 2.145,
    15: 2.131,
    16: 2.120,
    17: 2.110,
    18: 2.101,
    19: 2.093,
    20: 2.086,
    25: 2.060,
    30: 2.042,
}


def _t_crit(df: int) -> float:
    """95% 双侧 t 分位；表外按 1.96（渐近正太）。df>30 时差值 <2%，不影响判定。"""
    if df in _T_CRIT_95:
        return _T_CRIT_95[df]
    larger = [d for d in _T_CRIT_95 if d > df]
    return _T_CRIT_95[min(larger)] if larger else 1.96


def _task_stats(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按 (task, 尺子) 分组做 Δ 的配对统计：均值 ± t×SE，置信区间不含 0 才算显著。

    独立成函数是因为它是"**哪些臂计入**"这个决定（§三十·一），而
    `_history_command` 只该管渲染。两件事挤在一个函数里时，
    渲染上的任何分支都会顶高统计那段的复杂度台账。

    分组键带尺子身份（§三十一）：同一任务的跨代轮次（评委换型前后）**不配对**——
    Δ 的刻度与噪声随尺而变，混算出来的均值是两把尺的算术平均，不代表任何一把。

    ⚠️ 2026-10-31 三处修正（审计实测，每一条都会造出**假的"显著"**）：

    1. **SE=0 时不再判显著**：所有 Δ 完全相同 ⇒ var=0 ⇒ se=0 ⇒ 区间宽度为 0，
       于是 [0.5, 0.5] 也"显著"。但 Δ 本身是**两位小数取整**过的，
       "两次都恰好 0.5"不构成证据；这种情况要说"不可判"，而不是给一个零宽区间。
    2. **小样本用 t 分位**（见 `_t_crit`）：n=3 时 1.96 换成 4.303，
       实测 [0.1, 0.5, 0.9] 由"显著"翻成"不显著"。
    3. **可信臂不足 2 条的组不再被静默丢弃**：旧版 `continue` 之后，
       该组的崩溃臂连"被点名"的机会都没有，而注释里写的恰恰是"必须点名"。
    """
    import math

    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for r in rows:
        if r["delta"] is not None:
            groups.setdefault(_group_key(r), []).append(r)

    task_stats: list[dict[str, Any]] = []
    for (task, gen), rs in groups.items():
        if len(rs) < 2:
            continue
        deltas = [float(r["delta"]) for r in rs if r["trusted"]]
        n = len(deltas)
        excluded = [
            {"run_id": r["run_id"], "delta": r["delta"], "opt_avg": r["opt_avg"]}
            for r in rs
            if not r["trusted"]
        ]
        if n < 2:
            # 崩的臂被点名之后，可信臂不足 2 条：**不给判定，但要出现在结果里**
            # （旧版 continue ⇒ 这一组连同它的排除清单一起消失）。
            task_stats.append(
                {
                    "task": task,
                    "judge_gen": gen,
                    "n_runs": n,
                    "deltas": deltas,
                    "delta_mean": round(sum(deltas) / n, 2) if deltas else None,
                    "delta_ci95": None,
                    "significant": False,
                    "estimable": False,
                    "excluded_untrusted": excluded,
                    "note": "可信臂不足 2 条，无法给出显著性判定",
                    "run_ids": [],
                }
            )
            continue
        mean = sum(deltas) / n
        var = sum((x - mean) ** 2 for x in deltas) / (n - 1)
        se = math.sqrt(var / n)
        critical = _t_crit(n - 1)
        lo, hi = mean - critical * se, mean + critical * se
        # SE 为 0 = 从这批数据里**估不出**抖动，不是"抖动为零"。
        # 只在小样本时改成"不可判"：n≥5 且各轮 Δ 一模一样，那本身就是一致性证据，
        # 按显著处理并不离谱；n=2 的"两次都一样"没有任何信息量。
        # 与报告里"采样噪声 0.0 读作没测到"同一条纪律。
        se_unmeasurable = var <= 0.0 and n < 5
        task_stats.append(
            {
                "task": task,
                "judge_gen": gen,
                "n_runs": n,
                "deltas": deltas,
                "delta_mean": round(mean, 2),
                "delta_ci95": [round(lo, 2), round(hi, 2)],
                "significant": (not se_unmeasurable) and (lo > 0 or hi < 0),
                "estimable": not se_unmeasurable,
                "t_crit": critical,
                # §三十·一：崩掉的臂**不参与**统计，但必须**点名**。
                # 曾经它们被"噪声不可估"这条过滤静默删掉（见 `_load_run_history`），
                # 于是 Δ 均值从 +1.34 顶到 +2.15、CI 从跨 0 变成不含 0 ——
                # 一个"显著"的结论是被筛出来的。现在它明写在输出里。
                "excluded_untrusted": excluded,
                "note": _stat_note(n, se_unmeasurable),
                "run_ids": [r["run_id"] for r in rs if r["trusted"]],
            }
        )
    return task_stats


def _stat_note(n: int, se_unmeasurable: bool) -> str | None:
    """该组要带的那句实话；没有可说的话返回 None（别渲染空括号）。"""
    parts: list[str] = []
    if se_unmeasurable:
        parts.append("各轮 Δ 完全相同，抖动估不出来（不是抖动为零）")
    if n < 5:
        parts.append("样本少，结论仅供观察")
    return "；".join(parts) or None


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
                    "分组键含评委尺子身份（judge_gen，取自 judge_health.models，§三十一）："
                    "换评委 = 换算尺，跨代轮次不配对。"
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
        print("\n同任务跨 run 显著性（Δ 均值 ± 95% CI；按评委尺子分代）：")
        for s in task_stats:
            print(_stat_line(s))
            # §三十·一：崩掉的臂被排除时必须说出来，不能只报排除后的漂亮数字
            for e in s["excluded_untrusted"]:
                print(_excluded_line(e))
        note = _multi_ruler_note(task_stats)
        if note:
            print(note)
    else:
        print("\n（暂无同任务 ≥2 次的有效运行——对同一需求多跑几轮，这里会给出 Δ 的显著性判定）")
    return 0


def _stat_line(s: dict[str, Any]) -> str:
    """一个 (task, 尺子) 组的显著性行，带上尺子身份（§三十一）。"""
    if not s.get("estimable", True):
        # 不可判的时候不许印"不显著"——那也是一个判定，会让人以为"测过了，没效果"
        mark = "❔ 不可判"
    else:
        mark = "✅ 显著" if s["significant"] else "⚠️ 不显著"
    note = f"（{s['note']}）" if s["note"] else ""
    ci = s.get("delta_ci95")
    dm = s["delta_mean"]
    mean_text = f"{dm:+.2f}" if isinstance(dm, (int, float)) else "-"
    ci_text = f"CI [{ci[0]:+.2f}, {ci[1]:+.2f}]" if ci else "CI 不可估"
    return (
        f"  {mark}  Δ均值 {mean_text}  {ci_text}"
        f"  n={s['n_runs']} {note}｜评委 {s['judge_gen']}｜{s['task']}"
    )


def _multi_ruler_note(task_stats: list[dict[str, Any]]) -> str | None:
    """同一任务出现在 >1 把尺子上时给一句实话：上面那几行不是一列数。

    它挡的正是 §三十一 那类自欺——把 flash-lite 时代与 deepseek 时代的 Δ 并排看，
    会读出"评委变好了 / 效果变稳了"，其实只是两把尺的刻度不同。
    """
    per_task: dict[str, int] = {}
    for s in task_stats:
        per_task[s["task"]] = per_task.get(s["task"], 0) + 1
    n = sum(1 for c in per_task.values() if c > 1)
    if not n:
        return None
    return (
        f"  ⚠️ 有 {n} 个任务跨了不止一把尺（评委换型）：上面按尺分列，"
        "**不同尺的行不能读成一列**——Δ 的刻度与噪声随尺而变。"
    )


def _excluded_line(e: dict[str, Any]) -> str:
    """排除说明那一行。

    独立成函数是为了不把 `_history_command` 的圈复杂度顶过 CC 10 ——
    渲染细节不该和"哪些臂计入统计"那个决定挤在同一个函数里。
    """
    return (
        f"      ⚠️ 已排除崩溃臂 {e['run_id']}（优化后均分 {e['opt_avg']}、"
        f"Δ {e['delta']:+}）—— 不计入上面的均值，列在这儿是为了不让你以为它没跑过"
    )
