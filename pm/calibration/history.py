"""跨轮聚合：漂移账本的合并读数与考卷身份。

2026-10-19 从 `pm/calibration.py` 拆出。只读账本（`logs/judge_calibration_history.json`），零调用。
"""

from __future__ import annotations

import json
import random
from typing import Any

from ..schemas import PASS_THRESHOLD
from .render import _ROLE_LABEL, render_discrimination
from .samples import (
    HUMAN_SOURCE_AI_PROXY,
    HUMAN_SOURCE_OWNER,
    count_example_anchors,
    load_human_sources,
    render_example_anchor_disclosure,
    render_human_source_split,
    split_by_human_source,
)
from .stats import (
    _decision_stats,
    _percentile,
    discrimination_stats,
)

# --------------------------------------------------------------------------
# 跨轮聚合：把账本里同口径的最近几轮合并成「带不确定度的读数」（纯代码，零 LLM）
# --------------------------------------------------------------------------
# README §三 的问题："这些读数本身不稳……单次读数不能当'这把尺子的精度'"。
# 单轮校准把 n 条锚点读成一个数（MAE/bias/κ），锚点本身的取样波动 + 评委的逐轮抖动
# 都被抹进这一个数里。聚合做的事：① 逐锚点聚类自助法（cluster bootstrap）给出
# "换一批锚点重测"的重抽不确定度；② 轮间极差给出"同一把尺子逐轮的漂移幅度"。
# 两个数回答两个不同的问题，混用任何一个都会把噪声读成信号。
AGGREGATE_BOOTSTRAP = 2000


AGGREGATE_LAST_ROUNDS = 4


# 与 repeatability 文档里"极差阈值"同一件事的披露线：CI 宽过它，两次校准差值小于它的比较不成立
AGGREGATE_CI_WIDTH_NOTE = 0.5


def _round_mode(r: dict[str, Any]) -> str:
    """账本记录的评分协议。mode 字段引入之前的记录一律是印象式（与漂移对比同口径）。"""
    return str(r.get("mode") or "impression")


def aggregate_history(
    history: list[dict[str, Any]],
    *,
    mode: str = "impression",
    judge: str = "evaluator",
    model: str | None = None,
    rubric: str | None = None,
    last: int = AGGREGATE_LAST_ROUNDS,
    bootstrap: int = AGGREGATE_BOOTSTRAP,
    seed: int = 20260925,
) -> dict[str, Any] | None:
    """合并账本里同 judge+mode、带逐锚点明细的最近 `last` 轮，产出带不确定度的读数。

    返回 None 的两种情形都不是错误：账本里还没有该口径的带明细记录（旧账本只有
    轮级聚合数，重算不出逐锚点分布），或轮记录里一条有效配对都没有。调用方按
    "本轮不聚合"处理即可，不该报错吓人。

    为什么按锚点聚类而不是按配对重抽：同一锚点在 K 轮里出现 K 次，配对之间**不独立**
    ——按配对重抽会把"锚点好/坏"的方差撕碎进 2K 个独立样本里，CI 系统性偏窄，
    那是在用统计学制造精度幻觉。聚类自助法把每条锚点（连同它的全部轮次读数）作为
    一个整体重抽，锚点外推（换一批锚点重测）的不确定度才是这个 CI 想量的东西。

    与漂移对比（_comparable）刻意的不同：漂移只认"指纹完全一致"的轮，因为它是
    信号检测；聚合刻意放宽到同 judge+mode 即可，因为它是**量具刻画**——考卷不同
    时照常合并（对锚点总体的估计依然成立），但会显式声明，不给读数的人埋雷。

    `model`（2026-10-04 评委换型落地）：聚合的"量具"不只是考卷，还有**评委本身**
    ——bias 是"这把尺子相对人工分的系统性偏移"，换评委模型等于换尺子。flash-lite
    时代的 +3.09 与 deepseek-v4-flash 的 +0.03 混进同一份聚合，披露行刻画的就是
    "两把尺的平均"，对当前仪表是错的数。传 model 时只合并账本轮记录 `model` 字段
    匹配的轮次（该字段自换型前的记录起就有，见 _save_entry）；None = 不过滤
    （旧行为，历史读数复现路径保持不变）。换型后新模型尚无轮次时返回 None——
    披露行缺席，等第一次新校准入账。

    `rubric`（同日落地的第二层）：model 过滤后仍可能混进**旧 rubric 时代的同模型
    轮次**（实测：deepseek-v4-flash 在 2026-09-27 旧 rubric 下 bias +4.2、在现行
    rubric 下 +0.29——评分标准变了，同模型也不是同一把尺）。传 rubric 时要求轮
    记录的 `rubric` 指纹一致；模型×rubric 二元组才是"当前仪表"的完整刻画。锚点集
    刻意不进过滤——锚点不同的同代考卷照常合并是 §十四 立过的先例。
    """
    rounds = [
        r
        for r in history
        if r.get("judge") == judge
        and _round_mode(r) == mode
        # 空 items 列表 = 这轮没落明细（与"没有 items 字段"同罪），不算有明细的轮
        and isinstance(r.get("items"), list)
        and r.get("items")
        and (model is None or r.get("model") == model)
        and (rubric is None or r.get("rubric") == rubric)
    ]
    if not rounds:
        return None
    rounds = rounds[-max(1, last) :] if last > 0 else rounds
    pairs: list[dict[str, Any]] = []
    for r in rounds:
        for it in r.get("items") or []:
            if not isinstance(it, dict):
                continue
            try:
                float(it["human"]), float(it["judge"])
            except (KeyError, TypeError, ValueError):
                continue
            pairs.append(it)
    if not pairs:
        return None

    humans = [float(p["human"]) for p in pairs]
    judges = [float(p["judge"]) for p in pairs]
    deltas = [j - h for h, j in zip(humans, judges, strict=True)]
    n_pairs = len(pairs)
    pooled: dict[str, Any] = {
        "n_rounds": len(rounds),
        "n_pairs": n_pairs,
        "n_anchors": len({str(p["id"]) for p in pairs}),
        "mae": round(sum(abs(d) for d in deltas) / n_pairs, 3),
        "bias": round(sum(deltas) / n_pairs, 3),
        # 合并判定统计：κ/一致率是按混淆计数定义的，把各轮配对并起来直接重算即可
        "decision": _decision_stats(humans, judges, PASS_THRESHOLD),
        # 聚合读数是 README 真正引用的那一行，所以判别力/可估性必须在这里也算一遍：
        # 只在单轮报告里立门槛、最被引用的那个数不受管，等于门槛没上门。
        # 重抽单位按锚点聚类（同 §十四 的 MAE CI 口径）——同一锚点跨轮出现 K 次，
        # 按配对独立重抽会把 K 份相关读数当成 K 个独立样本，CI 系统性偏窄。
        "discrimination": discrimination_stats(
            humans, judges, PASS_THRESHOLD, group_keys=[str(p.get("id")) for p in pairs]
        ),
    }

    # 人工分出处拆分：把"bias +3.09"这一句拆成"严格人工 / AI 代判 / 没标注"三份读。
    # 纯账本重算，零调用；读不到锚点文件就是 unknown 一组，不编数。
    sources = load_human_sources()
    pooled["by_human_source"] = split_by_human_source(pairs, sources)
    _ai = (pooled["by_human_source"].get(HUMAN_SOURCE_AI_PROXY) or {}).get("n_anchors", 0)
    pooled["ai_proxy_anchors"] = _ai
    pooled["owner_anchors"] = (pooled["by_human_source"].get(HUMAN_SOURCE_OWNER) or {}).get(
        "n_anchors", 0
    )
    # 示例锚点计数（口径同 ai_proxy_anchors）：聚合刻意不按考卷过滤（§十四 先例），
    # 那就必须在读数旁边把"这批里有几条是编的"写出来，否则"准不准"这句话没有底。
    pooled["example_anchors"] = count_example_anchors(pairs)

    # 聚类自助法 CI：锚点为重抽单位（见 docstring）。锚点 <2 时重抽是常数，CI 退化为
    # 一个点——报出来就是"看起来很精确的零信息"，宁可声明跳过。
    clusters: dict[str, list[tuple[float, float]]] = {}
    for p in pairs:
        clusters.setdefault(str(p["id"]), []).append((float(p["human"]), float(p["judge"])))
    ids = sorted(clusters)
    if len(ids) >= 2 and bootstrap > 0:
        rng = random.Random(seed)
        by_id = [clusters[sid] for sid in ids]
        maes: list[float] = []
        biases: list[float] = []
        for _ in range(bootstrap):
            ds = [j - h for cl in (by_id[rng.randrange(len(by_id))] for _ in by_id) for h, j in cl]
            maes.append(sum(abs(d) for d in ds) / len(ds))
            biases.append(sum(ds) / len(ds))
        maes.sort()
        biases.sort()
        pooled["mae_ci95"] = [round(_percentile(maes, 2.5), 3), round(_percentile(maes, 97.5), 3)]
        pooled["bias_ci95"] = [
            round(_percentile(biases, 2.5), 3),
            round(_percentile(biases, 97.5), 3),
        ]
    else:
        pooled["ci_skipped"] = f"锚点 {len(ids)} 条 < 2，聚类自助法退化，不给 CI"

    def _span(key: str) -> dict[str, float] | None:
        vals = [float(r[key]) for r in rounds if isinstance(r.get(key), (int, float))]
        if not vals:
            return None
        return {
            "min": round(min(vals), 3),
            "max": round(max(vals), 3),
            "range": round(max(vals) - min(vals), 3),
            "n_rounds": len(vals),
        }

    rep_rounds = [
        r
        for r in rounds
        if isinstance(r.get("rep_range_max"), (int, float)) and r.get("rep_threshold")
    ]
    papers = _paper_identity(rounds)
    return {
        "judge": judge,
        "mode": mode,
        "rounds_used": len(rounds),
        # 旧→新，读的时候"最近一轮"是最后一行
        "rounds": [
            {
                "ts": r.get("ts"),
                "n": r.get("n"),
                "mae": r.get("mae"),
                "bias": r.get("bias"),
                "decision_agree": r.get("decision_agree"),
                "decision_kappa": r.get("decision_kappa"),
                "r": r.get("r"),
                "rep_range_max": r.get("rep_range_max"),
            }
            for r in rounds
        ],
        "pooled": pooled,
        "dispersion": {k: _span(k) for k in ("mae", "bias", "decision_agree", "decision_kappa")},
        "rep_threshold_crossings": sum(
            1 for r in rep_rounds if float(r["rep_range_max"]) > float(r["rep_threshold"])
        ),
        "n_rep_rounds": len(rep_rounds),
        "papers_differ": papers["papers_differ"],
        "paper_identity": papers,
    }


def _paper_identity(rounds: list[dict[str, Any]]) -> dict[str, Any]:
    """这几轮用的是不是同一张考卷。判据与漂移侧 `_comparable` 同源：账本里的锚点集指纹。

    条数只是**老记录没有指纹时**的替身，不是身份本身：换掉 5 条锚点再补进 5 条，
    在"条数"这一格里长得像"没换考卷"，而漂移侧拿着指纹会说"锚点集已更换"。
    同一本账本、两本读者、相反答案——§二十九 那一类错误换了个漂的对象
    （上次是尺子，这次是考卷），而产品报告引用的一直是聚合这本读者。见 §三十二。
    """
    stamps = [str(r.get("anchors") or "") for r in rounds]
    unstamped = sum(1 for st in stamps if not st)
    if unstamped:
        # 有记录没指纹就不许宣称"同一张考卷"：读不到当相同，等于把"没证据"
        # 当成"证据表明没变"。退化按条数判，并把退化本身报出去。
        distinct = len({r.get("n") for r in rounds})
        mode = "count-fallback"
    else:
        distinct = len(set(stamps))
        mode = "stamp"
    return {
        "mode": mode,
        "distinct_papers": distinct,
        "unstamped_rounds": unstamped,
        "papers_differ": distinct > 1,
    }


def latest_pooled(
    judge: str = "evaluator", model: str | None = None, rubric: str | None = None
) -> dict[str, Any] | None:
    """当前评分口径下，校准账本的跨轮聚合读数（None=没有可聚合的账本）。

    给交付报告的校准披露行用：报告渲染不自己翻账本算 bootstrap——量具
    刻画全部收口在 aggregate_history，这里只负责"找到账本、对上口径"。
    读不到账本不是错误（新检出没有校准历史时报告不披露即可）；文件坏了
    也返回 None，披露行缺席总比渲染崩掉好。

    `model`（2026-10-04 换型纪元）：调用方应传**本轮实际解析到的评委模型**
    （llm.build_config(judge).model），聚合只合并同模型的轮次——换型后旧
    模型轮次不再污染新仪表的披露行。None = 不过滤（旧行为）。
    """
    # 拆包后层级深了一层：`..` 才是 pm/（原先 `.cli` 指 pm.cli）
    from ..cli.support import log_dir  # 局部 import：晚绑定 PM_LOG_DIR，不顶层拉 CLI 层
    from ..scoring import scoring_mode

    path = log_dir() / "judge_calibration_history.json"
    try:
        history = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(history, list):
        return None
    # aggregate_history 返回 {pooled, rounds, ...} 外层结构；披露行只消费 pooled。
    result = aggregate_history(
        history, judge=judge, mode=scoring_mode(), model=model, rubric=rubric
    )
    return result.get("pooled") if result else None


def _paper_identity_lines(agg: dict[str, Any]) -> list[str]:
    """考卷身份的披露行。单独成函数有两个理由：措辞必须跟着**代码实际比了什么**走，
    而 `render_aggregate` 的复杂度台账（11）不该被一行披露挤上去——想抬台账先想想
    是不是该拆函数（`tests/test_complexity_ratchet.py` 正是为此存在）。"""
    ident = agg.get("paper_identity") or {}
    if agg.get("papers_differ"):
        how = (
            "参与的锚点集指纹不同"
            if ident.get("mode") == "stamp"
            else "参与的锚点条数不同（有 "
            + str(ident.get("unstamped_rounds", 0))
            + " 轮没有锚点指纹，退化按条数判）"
        )
        return [
            f"- ⚠️ 各轮{how}（换过考卷）：合并读数是对**锚点总体**的估计依然成立，"
            "但轮间数值对比（含上表的逐轮趋势）不成立。"
        ]
    if ident.get("mode") == "count-fallback" and ident.get("unstamped_rounds"):
        return [
            f"- ℹ️ 考卷身份走的是退化判据：{ident['unstamped_rounds']} 轮没有锚点指纹，"
            "这里只比了条数——**n 相同不代表集合相同**，别把这几轮当同一张考卷做趋势比较。"
        ]
    return []


def render_aggregate(agg: dict[str, Any] | None) -> str:
    """跨轮聚合读数的渲染。agg 为 None 返回空串——"没有可聚合的账本"不是一件事。"""
    if not agg:
        return ""
    pooled = agg["pooled"]
    label = _ROLE_LABEL.get(str(agg["judge"]), str(agg["judge"]))
    lines = [
        "",
        f"## 跨轮聚合（最近 {agg['rounds_used']} 轮 · {agg['mode']} · {label} · 纯账本重算，零调用）",
        "",
        "| 轮(旧→新) | 时间 | n | MAE | bias | 一致率 | κ | r | 复现极差max |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for i, r in enumerate(agg["rounds"], 1):
        lines.append(
            f"| {i} | {r.get('ts') or '-'} | {r.get('n') if r.get('n') is not None else '-'} "
            f"| {r.get('mae')} | {r.get('bias')} "
            f"| {r.get('decision_agree') if r.get('decision_agree') is not None else '-'} "
            f"| {r.get('decision_kappa') if r.get('decision_kappa') is not None else '-'} "
            f"| {r.get('r') if r.get('r') is not None else '-'} "
            f"| {r.get('rep_range_max') if r.get('rep_range_max') is not None else '-'} |"
        )
    mae_ci = pooled.get("mae_ci95")
    bias_ci = pooled.get("bias_ci95")
    mae_line = f"MAE **{pooled['mae']}**" + (
        f"［95% CI {mae_ci[0]}~{mae_ci[1]}］" if mae_ci else ""
    )
    bias_line = f"bias **{pooled['bias']:+}**" + (
        f"［95% CI {bias_ci[0]:+}~{bias_ci[1]:+}］" if bias_ci else ""
    )
    dec = pooled.get("decision") or {}
    agree_s = f"一致率 {dec.get('agree')}" if dec.get("agree") is not None else "一致率 -"
    kappa_s = "-" if dec.get("kappa") is None else f"κ {dec.get('kappa')}"
    lines.append("")
    lines.append(
        f"- 合并读数：{pooled['n_pairs']} 条配对（{pooled['n_anchors']} 条锚点）——"
        f"{mae_line}，{bias_line}，{agree_s}、{kappa_s}"
    )
    if pooled.get("ci_skipped"):
        lines.append(f"- ⚠️ {pooled['ci_skipped']}")
    lines.extend(render_discrimination(pooled.get("discrimination") or {}))
    lines.extend(render_human_source_split(pooled))
    lines.extend(render_example_anchor_disclosure(pooled))
    disp = agg.get("dispersion") or {}
    spans = []
    if agg.get("rounds_used", 0) >= 2:  # 单轮没有"轮间"可言，极差 0.0 是噪声不是信息
        for key, name in (
            ("mae", "MAE"),
            ("bias", "bias"),
            ("decision_agree", "一致率"),
            ("decision_kappa", "κ"),
        ):
            s = disp.get(key)
            if s:
                spans.append(f"{name} {s['min']}~{s['max']}（极差 {s['range']}）")
    if spans:
        lines.append(f"- 轮间极差（单轮读数的摆动幅度）：{'、'.join(spans)}")
    if agg.get("n_rep_rounds"):
        lines.append(
            f"- 复现极差越线：{agg['rep_threshold_crossings']}/{agg['n_rep_rounds']} 轮"
            "超过分差阈值——判定式「双评委分歧→仲裁」在这些轮里部分读的是评委自己的抖动"
        )
    lines.extend(_paper_identity_lines(agg))
    if mae_ci and mae_ci[1] - mae_ci[0] > AGGREGATE_CI_WIDTH_NOTE:
        lines.append(
            f"- ⚠️ MAE 的 CI 宽 {round(mae_ci[1] - mae_ci[0], 2)}：两次校准的 MAE 差值小于"
            "这个宽度的，都不构成「评委变好/变坏」的证据。"
        )
    lines.append(
        "> 怎么读（口径见 docs/evaluation.md §十四）：CI 是「换一批锚点重测」的重抽不确定度，"
        "轮间极差是「这台仪表逐轮的漂移」——前者管比较的分辨率，后者管单轮读数能信多宽。"
        "两者都不收敛之前，任何单轮读数都是 ±极差量级的方向参考。"
    )
    return "\n".join(lines)
