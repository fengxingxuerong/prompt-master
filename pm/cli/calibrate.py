"""calibrate 子命令：评委校准 + 跨次漂移告警。（自 run.py 拆出，逻辑逐字保留）"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path
from typing import Any

from .support import EXIT_CONFIG, EXIT_FAILED, LOG_DIR

# --------------------------------------------------------------------------
# calibrate 子命令：评委漂移监测（把 calibrate_judge 的校准能力接进主流程）
#
# 校准回答"评委与人类专家差多远"；漂移对比回答"同一个评委今天和上次比变了没有"。
# 每次校准的结果追加进 logs/judge_calibration_history.json，与上次同角色记录对比：
# bias（系统性偏松/偏严）或 mae（绝对偏差）变化超过 _CALIB_DRIFT_ALERT 即告警。
# --------------------------------------------------------------------------
_CALIB_DRIFT_ALERT = 0.5
_CALIB_HISTORY = LOG_DIR / "judge_calibration_history.json"


def _calib_fingerprints(role: str, samples: list[dict[str, Any]]) -> tuple[str, str, str]:
    """本次校准的口径指纹：(评委模型, 评分 rubric 指纹, 锚点集指纹)。

    读不到时返回占位符而不是抛错——漂移账本不该因为配置读不到就记不进去。
    """
    try:
        from .. import llm
        from ..prompts import rubric_stamp

        return (llm.build_config(role).model, rubric_stamp(), _anchors_stamp(samples))
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
    ap.add_argument("--json", action="store_true", help="输出 JSON（智能体消费）")
    ap.add_argument(
        "--no-save", action="store_true", help="本次结果不追加进漂移历史（只看不动账本）"
    )
    ns = ap.parse_args(argv)

    try:
        import calibrate_judge as calib
    except ImportError:  # pm 包装进 site-packages 后从任意 cwd 调用时，补仓库根找顶层脚本
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        import calibrate_judge as calib

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
            else "（先跑 python calibrate_judge.py --write-template 生成模板，并人工核对 human_score）"
        )
        print(f"样本为空：{ns.samples}{hint}", file=sys.stderr)
        return EXIT_CONFIG
    if pending:
        print(calib.render_provenance(len(samples) + len(pending), len(pending)), file=sys.stderr)

    analysis, errors = calib.calibrate(samples, ns.judge)
    for sid, err in errors:
        print(f"  [SKIP] {sid}：{err}", file=sys.stderr)
    if not analysis:
        print("全部锚点评估失败，无法校准（先跑 run.py --preflight 检查端点）", file=sys.stderr)
        return EXIT_FAILED

    # 复现性：与"准不准"正交的另一件事。一个每次调用在不同口径之间跳变的评委，
    # 在 bias/MAE/r 上可以看着完全正常（抖动甚至摊平 MAE），但它会让双评委分差、
    # 仲裁触发率与 Δ 的噪声带全部失去含义。绕缓存重复打，否则极差恒为 0。
    if ns.repeat >= 2:
        rep = calib.repeatability(samples, ns.judge, ns.repeat)
        analysis["repeatability"] = rep

    # 漂移对比：只在**同口径**的历史记录之间比。
    # 旧写法是"与上一条同角色记录比"，但换评委模型、改评分提示词（锚点/权威顺序）
    # 都会让 MAE/bias 阶跃——那不是评委漂移，是我们动了尺子；2026-09-18 就把一次
    # 有意的锚点收紧（MAE 1.07→0.37）读成了"漂移"。所以每条记录额外存三个指纹：
    # 评委模型名 + 评分 rubric 指纹 + 锚点集指纹，比较时只认三者都与本次一致的最近一条。
    history: list[dict[str, Any]] = []
    if _CALIB_HISTORY.exists():
        try:
            history = json.loads(_CALIB_HISTORY.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            history = []  # 历史损坏按空账本处理，本次照常记录
    now_model, now_rubric, now_anchors = _calib_fingerprints(ns.judge, samples)
    now_n = int(analysis["n"])

    def _comparable(h: dict[str, Any]) -> bool:
        """这条历史记录能不能当基线。

        **缺指纹的老记录按可比处理**（账本里真有：加这两个字段之前的记录就没有）——
        把它们判成"口径不同"等于在此后几轮里静默停掉漂移检测，比误报更糟。
        只有显式记着不同指纹的，才认定是尺子变了。

        锚点条数是那条兜底的例外：老记录虽然没指纹，`n` 一直都在账本里，
        所以"6 条的手写锚点 vs 11 条含真实跑批锚点"这种换考卷能被抓出来，
        而不是被当成评委漂移。
        """
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
            "n": analysis["n"],
            "bias": analysis["bias"],
            "mae": analysis["mae"],
            "r": analysis["r"],
            "rho": analysis.get("rho"),
            # 采集了多少条不参与打分的候选也入账：只看 n 会把"扩了 3 倍锚点集"
            # 和"一条都没人工确认"读成同一件事。
            "n_pending": len(pending),
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
                }
            )
        history.append(entry)
        _CALIB_HISTORY.parent.mkdir(parents=True, exist_ok=True)
        _CALIB_HISTORY.write_text(
            json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    if ns.json:
        print(
            json.dumps(
                {
                    "judge": ns.judge,
                    "analysis": analysis,
                    "drift": drift,
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
    print(f"校准记录已{'保存' if not ns.no_save else '跳过保存'}：{_CALIB_HISTORY}")
    return 0
