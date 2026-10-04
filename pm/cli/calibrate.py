"""calibrate 子命令：评委校准 + 跨次漂移告警。（自 run.py 拆出，逻辑逐字保留）"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path
from typing import Any

from ..scoring import scoring_mode
from .support import EXIT_CONFIG, EXIT_FAILED, log_dir

# --------------------------------------------------------------------------
# calibrate 子命令：评委漂移监测（把 pm/calibration.py 的校准能力接进主流程）
#
# 校准回答"评委与人类专家差多远"；漂移对比回答"同一个评委今天和上次比变了没有"。
# 每次校准的结果追加进 logs/judge_calibration_history.json，与上次同角色记录对比：
# bias（系统性偏松/偏严）或 mae（绝对偏差）变化超过 _CALIB_DRIFT_ALERT 即告警。
# --------------------------------------------------------------------------
_CALIB_DRIFT_ALERT = 0.5


def _calib_history() -> Path:
    """校准账本路径。原来它是 `LOG_DIR / ...` 的 import 期常量，于是
    `PM_LOG_DIR` 对 `run_*.json` 生效、对这本账不生效 —— 同一个变量两种命运，
    结果是"我把产物挪到别处了"这个前提下，测试与运维都会以为账本也跟着走了。
    """
    return log_dir() / "judge_calibration_history.json"


def _calib_fingerprints(
    role: str, samples: list[dict[str, Any]], mode: str
) -> tuple[str, str, str]:
    """本次校准的口径指纹：(评委模型, 评分 rubric 指纹, 锚点集指纹)。

    读不到时返回占位符而不是抛错——漂移账本不该因为配置读不到就记不进去。

    `mode` 决定照哪份 rubric：判定式与印象式对同一份输入打的不是同一个量，
    指纹若相同，漂移检测就会把"换了协议"读成"评委漂了"。
    """
    try:
        from .. import llm
        from ..prompts import checklist_rubric_stamp, rubric_stamp

        stamp = checklist_rubric_stamp() if mode == "checklist" else rubric_stamp()
        return (llm.build_config(role).model, stamp, _anchors_stamp(samples))
    except Exception:  # noqa: BLE001 - 指纹缺失只影响"能不能比"，不影响本次校准
        return ("(unknown)", "", _anchors_stamp(samples))


def _anchors_stamp(samples: list[dict[str, Any]]) -> str:
    """锚点集指纹：每条 (id, 人工分) 的集合摘要。

    漂移检测原本只盯两把尺子——评委模型与评分提示词——漏了第三把：**考卷本身**。
    2026-09-18 把锚点从 6 条手写扩到 11 条（新增 5 条取自真实跑批的长输出），评委 A 的
    MAE 从 0.37 跳到 0.90。这不是评委变差，是新锚点专挑人机会分歧最大的地方。不记这一笔，
    下一次校准会把"换考卷"读成漂移；反过来拿 6 条的旧基线判 11 条的新读数为"稳定"也不对。
    人工分也算进指纹：重新核对某个 `human_score` 同样是在动尺子。
    """
    payload = "|".join(
        f"{s.get('id')}={s.get('human_score')}"
        for s in sorted(samples, key=lambda s: str(s.get("id")))
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:10]


_FORM_COLUMNS = (
    "id",
    "分数带",
    "评委分参考",
    "机械线索",
    "目标模型",
    "原始需求",
    "输出摘要",
    "human_score",
    "备注",
)


def _clip(text: Any, n: int) -> str:
    return " ".join(str(text or "").split())[:n]


def _make_score_form(samples_path: Path, out: Path) -> int:
    """把锚点文件摊成一张 CSV 打分表：人只需要填 `human_score` 一列。

    Why：之前让人复核 34 条的做法是"打开 211KB 的 JSON，找到那条，改两个字段"，
    重复 34 次 —— 拦住的不是判断力而是操作成本（REVIEW.md 已经写清了怎么判）。
    表里 `human_score` 一律留空：AI 填的"人工分"和被校的评委同源，
    进集之后测出来的 r 只是在读评委自己的口味（这一条是本项目明确的立场）。
    编码用 utf-8-sig，Excel/WPS 双击就能看中文，不用先导一次。
    """
    import csv

    raw = json.loads(samples_path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError(f"{samples_path}：锚点文件必须是 JSON 数组")
    rows: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict) or not item.get("id"):
            continue  # _comment 之类的分隔条目不进表
        prov = item.get("provenance") or {}
        rows.append(
            {
                "id": str(item["id"]),
                "分数带": _clip(item.get("band"), 12),
                "评委分参考": "" if prov.get("judge_score") is None else str(prov["judge_score"]),
                "机械线索": _clip(item.get("note"), 120),
                "目标模型": _clip(prov.get("target_model"), 24),
                "原始需求": _clip(item.get("original_task"), 90),
                "输出摘要": _clip(item.get("test_output"), 160),
                # 已确认的条目把现值带出来：表要能重复生成而不丢已填的分
                "human_score": "" if item.get("human_score") is None else str(item["human_score"]),
                "备注": "已确认" if item.get("confirmed") else "",
            }
        )
    if not rows:
        raise ValueError(f"{samples_path}：没有带 id 的锚点条目可摊")
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(_FORM_COLUMNS))
        writer.writeheader()
        writer.writerows(rows)
    print(
        f"已生成打分表：{out}\n"
        f"  共 {len(rows)} 条，只需填 human_score 一列（1-10，可用小数；不填 = 这条不参与校准）。\n"
        f"  「评委分参考」那一列是待校评委自己给的分，用途是先去看你和它不一致的那些，不是抄它。\n"
        f"  填完回填：python run.py calibrate --apply-scores {out} --samples {samples_path}"
    )
    return 0


def _apply_score_form(samples_path: Path, form_path: Path) -> int:
    """把填好的打分表写回锚点文件：填了分的那几条置 confirmed=true。

    两件事刻意坚持：
    1. **有任何一行非法就一条都不写**（失败关闭）。写一半再让人自己找哪行坏了，
       比让他在报错行号前自己修完更贵；
    2. **不猜分、不补分、不改没填的条目**。没填 = 还没判 = 不进分母。
    """
    import csv
    import os

    filled: dict[str, float] = {}
    problems: list[str] = []
    with form_path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames or "human_score" not in reader.fieldnames:
            print(f"打分表缺 human_score 列（先跑 --make-form 生成）：{form_path}", file=sys.stderr)
            return EXIT_CONFIG
        for no, row in enumerate(reader, 2):  # 第 1 行是表头
            sid = str(row.get("id") or "").strip()
            raw_score = str(row.get("human_score") or "").strip()
            if not sid:
                continue
            if not raw_score:
                continue
            try:
                value = float(raw_score)
            except ValueError:
                problems.append(f"第 {no} 行 {sid}：human_score={raw_score!r} 不是数字")
                continue
            if not 1.0 <= value <= 10.0:
                problems.append(f"第 {no} 行 {sid}：human_score={value} 超出 1-10")
                continue
            if sid in filled:
                problems.append(f"第 {no} 行：id {sid} 在表里出现两次（先合并再跑）")
                continue
            filled[sid] = value
    if problems:
        for p in problems:
            print(f"  [拒绝写入] {p}", file=sys.stderr)
        print(
            f"共 {len(problems)} 处问题；**本次一个字都没写回**（写一半比不写更难查）。",
            file=sys.stderr,
        )
        return EXIT_CONFIG
    if not filled:
        print(f"{form_path} 里一条 human_score 都没填 —— 什么都没改。", file=sys.stderr)
        return EXIT_CONFIG

    raw = json.loads(samples_path.read_text(encoding="utf-8"))
    known = 0
    for item in raw:
        if not isinstance(item, dict):
            continue
        sid = str(item.get("id") or "")
        if sid in filled:
            item["human_score"] = filled[sid]
            item["confirmed"] = True
            # 回填通道只有"人真的填了分"才会走到这里 ⇒ 出处必须一起落，否则下次代判/亲判
            # 又混成一锅（provenance 位是 2026-10-03 补的，缺它 bias 就拆不出严格人工那一半）。
            prov = item.get("provenance")
            if not isinstance(prov, dict):
                prov = {}
                item["provenance"] = prov
            prov["human_score_source"] = "owner"
            known += 1
    unknown = sorted(set(filled) - {str(i.get("id")) for i in raw if isinstance(i, dict)})
    if not known:
        print(
            f"表里 {len(filled)} 条分数没有一个 id 对得上 {samples_path}（表是旧版？）——未写入。",
            file=sys.stderr,
        )
        return EXIT_CONFIG
    tmp = samples_path.with_suffix(samples_path.suffix + ".tmp")
    tmp.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, samples_path)
    print(
        f"已回填 {known} 条（human_score + confirmed=true）→ {samples_path}\n"
        f"  表里没填的条目保持原样，继续不参与校准。"
    )
    if unknown:
        print(f"⚠️ 表里有 {len(unknown)} 个 id 不在锚点文件中：{unknown[:5]}", file=sys.stderr)
    print(
        "  注：人工分进锚点集指纹（_anchors_stamp），所以下一次校准与历史记录的漂移对比"
        "会判为「换考卷」而不报评委漂移 —— 这是对的，尺子确实动了。"
    )
    return 0


def _calibrate_command(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="run.py calibrate")
    ap.add_argument(
        "--judge",
        default="evaluator",
        choices=["evaluator", "evaluator_b", "arbiter"],
        help="要校准的评委角色（默认 evaluator）",
    )
    ap.add_argument(
        "--samples",
        default=str(Path(__file__).resolve().parents[2] / "judge_calibration" / "samples.json"),
        help="锚点样本 JSON 路径（需人工核对 human_score）",
    )
    ap.add_argument(
        "--repeat",
        type=int,
        default=1,
        help="额外做「评委与自己比」的复现性测量：同输入连打 N 次（绕缓存），"
        "代价是 N×锚点 次调用；1=不测",
    )
    ap.add_argument(
        "--scoring-mode",
        choices=("impression", "checklist"),
        default=None,
        help="评分协议；不填则读 PM_SCORING_MODE（默认 impression=现行五个 1-10 整数）。"
        "checklist=二值判定 + 代码算分，用来做两套协议的 A/B",
    )
    ap.add_argument("--json", action="store_true", help="输出 JSON（智能体消费）")
    ap.add_argument(
        "--no-save", action="store_true", help="本次结果不追加进漂移历史（只看不动账本）"
    )
    ap.add_argument(
        "--make-form",
        default=None,
        metavar="CSV",
        help="不跑校准：把 --samples 那个锚点文件摊成一张人工打分表（human_score 列留空待填）",
    )
    ap.add_argument(
        "--apply-scores",
        default=None,
        metavar="CSV",
        help="不跑校准：把填好的打分表写回 --samples（只改填了分的那几条）",
    )
    ap.add_argument(
        "--aggregate",
        action="store_true",
        help="不跑校准：只把账本里同 judge+协议的最近几轮合并成带 CI 的跨轮读数"
        "（纯账本重算，零调用不花钱；口径见 docs/evaluation.md §十四）",
    )
    ns = ap.parse_args(argv)

    # 两个"只动表、不花钱"的入口先分流：它们不需要 Key、不调模型，
    # 也不该被后面的样本加载/校准流程牵进去
    if ns.make_form:
        try:
            return _make_score_form(Path(ns.samples), Path(ns.make_form))
        except (OSError, ValueError, json.JSONDecodeError) as e:
            print(f"生成打分表失败：{e}", file=sys.stderr)
            return EXIT_CONFIG
    if ns.apply_scores:
        try:
            return _apply_score_form(Path(ns.samples), Path(ns.apply_scores))
        except (OSError, ValueError, json.JSONDecodeError) as e:
            print(f"回填打分表失败：{e}", file=sys.stderr)
            return EXIT_CONFIG
    if ns.aggregate:
        from .. import calibration as _calib

        history_path = _calib_history()
        ledger: list[dict[str, Any]] = []
        if history_path.exists():
            try:
                ledger = json.loads(history_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                msg = f"账本损坏，无法聚合：{history_path}"
                if ns.json:
                    print(json.dumps({"error": msg}, ensure_ascii=False))
                else:
                    print(msg, file=sys.stderr)
                return EXIT_CONFIG
        eff_mode = ns.scoring_mode or scoring_mode()
        agg = _calib.aggregate_history(ledger, mode=eff_mode, judge=ns.judge)
        if agg is None:
            msg = (
                f"账本里没有 {ns.judge} × {eff_mode} 口径、带逐锚点明细的记录可聚合"
                "（旧记录只有轮级聚合数，重算不出逐锚点分布）。"
            )
            # README 的约定：--json 时 stdout 恰好一个 JSON 对象——错误路径也不例外，
            # 否则 MCP 的 _run_cli_json 会在 json.loads("") 上炸出无关错误。
            if ns.json:
                print(json.dumps({"error": msg}, ensure_ascii=False))
            else:
                print(msg, file=sys.stderr)
            return EXIT_CONFIG
        if ns.json:
            print(json.dumps({"aggregate": agg}, ensure_ascii=False, indent=2))
        else:
            print(_calib.render_aggregate(agg))
        return 0

    from .. import calibration as calib

    try:
        samples = calib.load_samples(Path(ns.samples))
    except (OSError, ValueError, json.JSONDecodeError) as e:
        print(f"样本加载失败：{e}", file=sys.stderr)
        return EXIT_CONFIG
    # 未人工确认的候选已被 load_samples 挡在 samples 外面，这里只负责把这件事说出来：
    # 采了 45 条、实际用 11 条，如果不印这一行，读输出的人会以为 45 条全进了分母。
    pending = getattr(samples, "pending", []) or []
    if not samples:
        hint = (
            f"（{len(pending)} 条候选全部未人工确认——填 human_score 并把 confirmed 改成 true 再跑）"
            if pending
            else "（先跑 python -m pm.calibration --write-template 生成模板，并人工核对 human_score）"
        )
        print(f"样本为空：{ns.samples}{hint}", file=sys.stderr)
        return EXIT_CONFIG
    if pending:
        print(calib.render_provenance(len(samples) + len(pending), len(pending)), file=sys.stderr)

    # 协议：命令行优先，其次 PM_SCORING_MODE，都没有就是现行印象式。
    mode = ns.scoring_mode or scoring_mode()
    analysis, errors = calib.calibrate(samples, ns.judge, mode)
    for sid, err in errors:
        print(f"  [SKIP] {sid}：{err}", file=sys.stderr)
    if not analysis:
        print("全部锚点评估失败，无法校准（先跑 run.py --preflight 检查端点）", file=sys.stderr)
        return EXIT_FAILED

    # 复现性：与"准不准"正交的另一件事。一个每次调用在不同口径之间跳变的评委，
    # 在 bias/MAE/r 上可以看着完全正常（抖动甚至摊平 MAE），但它会让双评委分差、
    # 仲裁触发率与 Δ 的噪声带全部失去含义。绕缓存重复打，否则极差恒为 0。
    if ns.repeat >= 2:
        rep = calib.repeatability(samples, ns.judge, ns.repeat, mode)
        analysis["repeatability"] = rep

    # 漂移对比：只在**同口径**的历史记录之间比。
    # 旧写法是"与上一条同角色记录比"，但换评委模型、改评分提示词（锚点/权威顺序）
    # 都会让 MAE/bias 阶跃——那不是评委漂移，是我们动了尺子；2026-09-18 就把一次
    # 有意的锚点收紧（MAE 1.07→0.37）读成了"漂移"。所以每条记录额外存三个指纹：
    # 评委模型名 + 评分 rubric 指纹 + 锚点集指纹，比较时只认三者都与本次一致的最近一条。
    history: list[dict[str, Any]] = []
    history_path = _calib_history()
    if history_path.exists():
        try:
            history = json.loads(history_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            history = []  # 历史损坏按空账本处理，本次照常记录
    now_model, now_rubric, now_anchors = _calib_fingerprints(ns.judge, samples, mode)
    now_n = int(analysis["n"])

    def _comparable(h: dict[str, Any]) -> bool:
        """这条历史记录能不能当基线。

        **缺指纹的老记录按可比处理**（账本里真有：加这两个字段之前的记录就没有）——
        把它们判成"口径不同"等于在此后几轮里静默停掉漂移检测，比误报更糟。
        只有显式记着不同指纹的，才认定是尺子变了。

        锚点条数是那条兜底的例外：老记录虽然没指纹，`n` 一直都在账本里，
        所以"6 条的手写锚点 vs 11 条含真实跑批锚点"这种换考卷能被抓出来，
        而不是被当成评委漂移。

        协议（mode）在两个分支之外单独判：老记录没有这个字段，缺省就是印象式；
        但判定式跑出来的分与印象式的分不是同一个量，混在一起比"漂移"是自欺。
        """
        if h.get("mode", "impression") != mode:
            return False
        hm, hr, ha = h.get("model"), h.get("rubric"), h.get("anchors")
        if not (hm or hr or ha):
            return h.get("n") in (None, now_n)
        return (
            hm == now_model
            and (not hr or hr == now_rubric)
            and (not ha or ha == now_anchors)
            and now_n == h.get("n", now_n)
        )

    def _same_cohort(h: dict[str, Any]) -> bool:
        return h.get("judge") == ns.judge and _comparable(h)

    prev = next((h for h in reversed(history) if _same_cohort(h)), None)
    # 最近一条同角色记录：只在"没有可比基线、但有不可比记录"时才用来说明原因
    changed = next(
        (h for h in reversed(history) if h.get("judge") == ns.judge and not _comparable(h)),
        None,
    )
    drift: dict[str, Any] | None = None
    if prev is not None:
        d_bias = round(float(analysis["bias"]) - float(prev["bias"]), 2)
        d_mae = round(float(analysis["mae"]) - float(prev["mae"]), 2)
        drifted = abs(d_bias) >= _CALIB_DRIFT_ALERT or abs(d_mae) >= _CALIB_DRIFT_ALERT
        drift = {
            "prev_ts": prev.get("ts"),
            "prev_bias": prev["bias"],
            "prev_mae": prev["mae"],
            "delta_bias": d_bias,
            "delta_mae": d_mae,
            "drifted": drifted,
            "alert_line": _CALIB_DRIFT_ALERT,
        }
    elif changed is not None:
        reasons = "、".join(
            filter(
                None,
                [
                    (
                        f"评分协议已更换（{changed.get('mode', 'impression')} → {mode}）"
                        if changed.get("mode", "impression") != mode
                        else ""
                    ),
                    "评委模型已更换" if changed.get("model") not in (None, now_model) else "",
                    "评分提示词已改动" if changed.get("rubric") not in (None, now_rubric) else "",
                    ("锚点集已更换" if changed.get("anchors") not in (None, now_anchors) else ""),
                    # n 是"成功打出分的锚点数"，不只是集合大小：某条被端点抖动作废
                    # 也会让 MAE 的分母不同，所以同样算换考卷。老记录没指纹时，
                    # 这是唯一能看出 6 条→11 条的信号。
                    (
                        f"参与统计的锚点条数不同（{changed.get('n')} → {now_n}）"
                        if changed.get("n") not in (None, now_n)
                        else ""
                    ),
                ],
            )
        )
        drift = {
            "prev_ts": changed.get("ts"),
            "comparable": False,
            "why_not_comparable": reasons or "口径指纹不一致",
            "prev_bias": changed["bias"],
            "prev_mae": changed["mae"],
            "drifted": False,
        }

    if not ns.no_save:
        entry: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "judge": ns.judge,
            "mode": mode,
            "n": analysis["n"],
            "bias": analysis["bias"],
            "mae": analysis["mae"],
            "r": analysis["r"],
            "rho": analysis.get("rho"),
            # 采集了多少条不参与打分的候选也入账：只看 n 会把"扩了 3 倍锚点集"
            # 和"一条都没人工确认"读成同一件事。
            "n_pending": len(pending),
            # 逐条明细必须入账。只留聚合数的账本没法归因：2026-09-25 那轮
            # impression n=18 / checklist n=15 而 n_pending 都是 0，事后完全查不出
            # 判定式臂丢了哪 3 条、为什么丢，于是"MAE 0.83 → 1.85"这个换不换默认的依据
            # 至今无法判断是结论还是幸存者偏差。失败原因当场只打到 stderr（[SKIP] 行），
            # 关掉终端就没了 —— 账本是唯一能活过一轮跑批的地方。
            "n_failed": len(errors),
            "failed": [{"id": sid, "error": err} for sid, err in errors],
            "items": analysis.get("pairs") or [],
            "decision_agree": (analysis.get("decision") or {}).get("agree"),
            "decision_kappa": (analysis.get("decision") or {}).get("kappa"),
            "model": now_model,
            "rubric": now_rubric,
            "anchors": now_anchors,
        }
        rep = analysis.get("repeatability") or {}
        if rep.get("n_items"):
            # 复现性也进账本：MAE/bias 只说"准不准"，极差说"这台仪表自己稳不稳"。
            # 换评委型号后若极差变大，仲裁触发率与 Δ 分辨率都会变，那时漂移对比
            # 必须能把"仪表换了"和"评委漂了"分开。
            entry.update(
                {
                    "rep_times": rep["times"],
                    "rep_range_mean": rep["range_mean"],
                    "rep_range_max": rep["range_max"],
                    "rep_threshold": rep["disagreement_threshold"],
                    # 每锚点的极差也要落盘：均值会把"半数稳、半数疯"抹平成一个好数字，
                    # 而两臂配对比必须能只看"两边都成功打完 N 次"的那几条
                    "rep_items": [
                        {
                            "id": it.get("id"),
                            "n": it.get("n"),
                            "range": it.get("range"),
                            "error": it.get("error") or "",
                        }
                        for it in rep.get("per_item") or []
                    ],
                }
            )
        history.append(entry)
        path = _calib_history()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")

    # 跨轮聚合：把账本里同 judge+协议、带逐锚点明细的最近几轮（保存路径下含刚入账的本次）
    # 合并成带 CI 的读数。单轮校准把锚点取样波动 + 评委逐轮抖动都抹进一个数——这是对
    # README §三 "这些读数本身不稳，单次读数不能当尺子的精度"的正面回应：
    # CI 管两次比较的分辨率，轮间极差管单轮读数能信多宽，两个数不收敛就都别当结论。
    agg = calib.aggregate_history(history, mode=mode, judge=ns.judge)

    if ns.json:
        print(
            json.dumps(
                {
                    "judge": ns.judge,
                    "mode": mode,
                    "analysis": analysis,
                    "drift": drift,
                    "aggregate": agg,
                    "n_pending": len(pending),
                    # history 已 append（保存路径），len 即账本当前条数；
                    # 旧写法再 +1 会把本次记录数成两倍，误导"已校准过几轮"的判断
                    "history_len": len(history),
                    "saved": not ns.no_save,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    print(f"[评分协议] {mode}")
    print(calib.render_report(ns.judge, analysis))
    if drift and drift.get("comparable") is False:
        print(
            f"\n[ℹ️ 无可比基线] 最近一条同角色记录（{drift['prev_ts']}）与本次口径不同："
            f"{drift['why_not_comparable']}。"
            f"bias {drift['prev_bias']} → {analysis['bias']}、mae {drift['prev_mae']} → "
            f"{analysis['mae']} 属**尺子变了**，不计评委漂移；本次已作为新基线入账。"
        )
    elif drift:
        mark = "⚠️ 漂移告警" if drift["drifted"] else "✅ 稳定"
        print(
            f"\n[{mark}] 与上次（{drift['prev_ts']}）对比：bias {drift['prev_bias']} → "
            f"{analysis['bias']}（Δ{drift['delta_bias']:+.2f}），mae {drift['prev_mae']} → "
            f"{analysis['mae']}（Δ{drift['delta_mae']:+.2f}）；告警线 ±{_CALIB_DRIFT_ALERT}"
        )
    rep_out = analysis.get("repeatability")
    if rep_out:
        print(calib.render_repeatability(rep_out))
    agg_out = calib.render_aggregate(agg)
    if agg_out:
        print(agg_out)
    print(f"校准记录已{'保存' if not ns.no_save else '跳过保存'}：{_calib_history()}")
    return 0
