"""calibrate 子命令：评委校准 + 跨次漂移告警。（自 run.py 拆出，逻辑逐字保留）"""

from __future__ import annotations

import argparse
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
    if not samples:
        print(
            f"样本为空：{ns.samples}（先跑 python calibrate_judge.py --write-template 生成模板，"
            "并人工核对 human_score）",
            file=sys.stderr,
        )
        return EXIT_CONFIG

    analysis, errors = calib.calibrate(samples, ns.judge)
    for sid, err in errors:
        print(f"  [SKIP] {sid}：{err}", file=sys.stderr)
    if not analysis:
        print("全部锚点评估失败，无法校准（先跑 run.py --preflight 检查端点）", file=sys.stderr)
        return EXIT_FAILED

    # 漂移对比：与上次**同角色**记录比 bias / mae 的变化量
    history: list[dict[str, Any]] = []
    if _CALIB_HISTORY.exists():
        try:
            history = json.loads(_CALIB_HISTORY.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            history = []  # 历史损坏按空账本处理，本次照常记录
    prev = next((h for h in reversed(history) if h.get("judge") == ns.judge), None)
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

    if not ns.no_save:
        history.append(
            {
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                "judge": ns.judge,
                "n": analysis["n"],
                "bias": analysis["bias"],
                "mae": analysis["mae"],
                "r": analysis["r"],
            }
        )
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
    if drift:
        mark = "⚠️ 漂移告警" if drift["drifted"] else "✅ 稳定"
        print(
            f"\n[{mark}] 与上次（{drift['prev_ts']}）对比：bias {drift['prev_bias']} → "
            f"{analysis['bias']}（Δ{drift['delta_bias']:+.2f}），mae {drift['prev_mae']} → "
            f"{analysis['mae']}（Δ{drift['delta_mae']:+.2f}）；告警线 ±{_CALIB_DRIFT_ALERT}"
        )
    print(f"校准记录已{'保存' if not ns.no_save else '跳过保存'}：{_CALIB_HISTORY}")
    return 0
