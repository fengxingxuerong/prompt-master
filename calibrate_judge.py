#!/usr/bin/env python3
"""评委校准：在锚点样本上对比「评委分」与「人工专家分」，回答"评委打 8 分可信吗"。

为什么需要它：整个闭环的质量上限是评委模型的水位，而"防放水机制"只保证评委
**不虚高**，不回答"它与人类专家的判断差多远"。校准集 = 一批（提示词 × 测试输入 ×
输出 × 人工分）锚点：用与主管道完全相同的评估管线（EVALUATOR_SYSTEM + 同一渲染 +
同一 finalize 加权）跑一遍真实评委，计算偏差与排序一致性——换评委模型、调评分
提示词之后跑一次，"分数可信"就从设计主张变成实测数据。

用法：
    python calibrate_judge.py --write-template   # 从示例生成 samples.json（已存在则跳过）
    python calibrate_judge.py                    # 校准默认评委（evaluator）
    python calibrate_judge.py --judge evaluator_b

流程：
1. 人工给 `judge_calibration/samples.json` 里的每条锚点打 human_score（1-10）；
2. 本工具逐条调用真实评委，得到代码加权分（与主管道同口径）；
3. 输出逐条 Δ、MAE、平均偏差（正=偏松/放水倾向，负=偏严）、Pearson 排序一致性，
   并给对症结论。退出码：0=完成分析（结论好坏都算完成），1=全部评估失败，2=样本问题。

诚实边界：校准告诉你偏差**多大**，不替你提升评委能力；样本 <5 条时数字只有方向性。
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

from pm.bootstrap import ensure_utf8_stdio

ensure_utf8_stdio()

from pm.llm import structured_call  # noqa: E402
from pm.prompts import EVALUATOR_SYSTEM, EVALUATOR_USER, render  # noqa: E402
from pm.schemas import EvaluationResult  # noqa: E402

DEFAULT_SAMPLES = Path(__file__).parent / "judge_calibration" / "samples.json"
EXAMPLE_SAMPLES = Path(__file__).parent / "judge_calibration" / "samples.example.json"
JUDGE_ROLES = ("evaluator", "evaluator_b", "arbiter")

# 一致性判定阈值（与 schemas.JUDGE_BIAS_ALERT 的默认告警线对齐）
BIAS_ALERT = 1.0  # |平均偏差| 超过该值 → 系统性偏松/偏严
LARGE_DEVIATION = 2.0  # 单条 |Δ| 超过该值 → 点名大偏差样本
CORR_GOOD = 0.8  # Pearson r ≥ 该值 → 排序一致性好
CORR_BAD = 0.5  # r < 该值 → 排序一致性差
MIN_SAMPLES_FOR_R = 3  # 相关系数最少样本数
MIN_SAMPLES_FOR_VERDICT = 5  # 低于该数只给方向性参考

_ROLE_LABEL = {"evaluator": "评委A", "evaluator_b": "评委B", "arbiter": "仲裁评委"}


# --------------------------------------------------------------------------
# 样本加载
# --------------------------------------------------------------------------
def load_samples(path: Path) -> list[dict[str, Any]]:
    """加载并校验锚点样本。纯注释项（只有 _comment 等说明键）跳过；真实条目缺字段直接报错。

    注意跳过条件：只跳过「除说明键外什么都没有」的条目。
    像 {"id": "a"} 这种带真实键但缺关键字段的条目是**写错的样本**，
    静默跳过会让用户以为它参与了校准——必须报错。
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("样本文件顶层必须是数组")
    samples: list[dict[str, Any]] = []
    required = ("id", "original_task", "prompt", "test_input", "test_output", "human_score")
    skip_keys = {"_comment"}
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            raise ValueError(f"第 {i + 1} 条样本必须是对象")
        # 只有说明键、没有任何真实字段 → 注释项，跳过
        if set(item.keys()) <= skip_keys:
            continue
        missing = [k for k in required if k not in item]
        if missing:
            raise ValueError(f"第 {i + 1} 条样本缺字段：{missing}")
        score = float(item["human_score"])
        if not 1.0 <= score <= 10.0:
            raise ValueError(f"第 {i + 1} 条样本 human_score={score} 超出 1-10")
        samples.append(item)
    ids = [str(s["id"]) for s in samples]
    if len(ids) != len(set(ids)):
        raise ValueError("样本 id 重复")
    return samples


# --------------------------------------------------------------------------
# 评估（与主管道同口径）
# --------------------------------------------------------------------------
def evaluate_sample(role: str, sample: dict[str, Any]) -> EvaluationResult:
    """用与 pm.nodes._evaluate_one 相同的提示词渲染 + schema 调用真实评委。

    校准不另写评分提示词——否则校准的就不再是线上跑的那台评委。
    """
    user = render(
        EVALUATOR_USER,
        original_task=sample["original_task"],
        context=sample.get("context") or "（无）",
        prompt=sample["prompt"],
        test_input=sample["test_input"],
        test_output=sample["test_output"],
    )
    ev, _meta = structured_call(role, EvaluationResult, EVALUATOR_SYSTEM, user)
    return ev.finalize()


# --------------------------------------------------------------------------
# 一致性分析（纯代码，零 LLM）
# --------------------------------------------------------------------------
def pearson(xs: list[float], ys: list[float]) -> float | None:
    """Pearson 相关系数。样本 <3 或任一方差为零 → None（无法评估）。"""
    n = len(xs)
    if n < MIN_SAMPLES_FOR_R or n != len(ys):
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx == 0.0 or syy == 0.0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    return sxy / math.sqrt(sxx * syy)


def analyze(samples: list[dict[str, Any]], judge_scores: list[float]) -> dict[str, Any]:
    """逐条 Δ + 聚合指标。调用方保证两列表等长且只含成功样本。"""
    pairs: list[dict[str, Any]] = []
    # strict=False：调用方已保证等长，防御性容忍调用侧偏差而不炸
    for i, (s, j) in enumerate(zip(samples, judge_scores, strict=False)):
        human = float(s["human_score"])
        delta = round(j - human, 2)
        pairs.append(
            {
                "id": str(s.get("id") or f"#{i}"),
                "human": human,
                "judge": round(j, 2),
                "delta": delta,
                "flag": abs(delta) > LARGE_DEVIATION,
            }
        )
    n = len(pairs)
    bias = round(sum(p["delta"] for p in pairs) / n, 2)
    mae = round(sum(abs(p["delta"]) for p in pairs) / n, 2)
    r = pearson([p["human"] for p in pairs], [p["judge"] for p in pairs])
    return {
        "n": n,
        "pairs": pairs,
        "bias": bias,
        "mae": mae,
        "r": None if r is None else round(r, 3),
        "flags": [p["id"] for p in pairs if p["flag"]],
    }


# --------------------------------------------------------------------------
# 报告渲染
# --------------------------------------------------------------------------
def _bias_verdict(bias: float) -> str:
    if bias > BIAS_ALERT:
        return f"⚠️ 评委系统性**偏松**（平均比人工高 {bias} 分）——存在放水倾向，建议降温或换模型家族"
    if bias < -BIAS_ALERT:
        return (
            f"⚠️ 评委系统性**偏严**（平均比人工低 {abs(bias)} 分）——会压制合格输出，建议校准评分锚点"
        )
    return "✅ 整体偏差在容差内（±1 分）"


def _r_verdict(r: float | None) -> str:
    if r is None:
        return "⚠️ 排序一致性无法评估（样本不足或方差为零）"
    if r >= CORR_GOOD:
        return f"✅ 排序一致性好（r={r}）：评委分高的人工分也高"
    if r < CORR_BAD:
        return f"⚠️ 排序一致性差（r={r}）：评委的**相对判断**不可信，比分数本身更严重"
    return f"排序一致性中等（r={r}）"


def render_report(role: str, analysis: dict[str, Any]) -> str:
    label = _ROLE_LABEL.get(role, role)
    lines: list[str] = [f"# 评委校准报告：{label}（{role}）", ""]
    lines.append("| 锚点 | 人工分 | 评委分 | Δ | 标记 |")
    lines.append("|---|---|---|---|---|")
    for p in analysis["pairs"]:
        mark = "⚠️ 大偏差" if p["flag"] else ""
        lines.append(f"| {p['id']} | {p['human']} | {p['judge']} | {p['delta']:+} | {mark} |")
    lines.append("")
    lines.append(f"- 样本数：{analysis['n']}（排除评估失败的锚点）")
    lines.append(f"- 平均绝对误差 MAE：{analysis['mae']}")
    lines.append(
        f"- 平均偏差（评委 − 人工）：{analysis['bias']:+} → {_bias_verdict(analysis['bias'])}"
    )
    lines.append(f"- 排序一致性：{_r_verdict(analysis['r'])}")
    if analysis["flags"]:
        lines.append(
            f"- ⚠️ 大偏差样本（|Δ| > {LARGE_DEVIATION}）：{', '.join(analysis['flags'])}——优先人工复核这几条"
        )
    if analysis["n"] < MIN_SAMPLES_FOR_VERDICT:
        lines.append(
            f"> ⚠️ 样本 {analysis['n']} < {MIN_SAMPLES_FOR_VERDICT}：以上数字仅方向性参考，"
            "不足以支撑「评委可信/不可信」的结论，继续积累锚点。"
        )
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 校准主流程（与 CLI 分离，便于测试注入假评委）
# --------------------------------------------------------------------------
def calibrate(
    samples: list[dict[str, Any]], role: str
) -> tuple[dict[str, Any], list[tuple[str, str]]]:
    """逐条评估并聚合。返回 (分析结果, 失败锚点列表)。"""
    ok_samples: list[dict[str, Any]] = []
    ok_scores: list[float] = []
    errors: list[tuple[str, str]] = []
    for s in samples:
        sid = str(s.get("id") or "?")
        try:
            ev = evaluate_sample(role, s)
            ok_samples.append(s)
            ok_scores.append(ev.weighted_score)
        except Exception as e:  # noqa: BLE001 - 单锚点失败不中断校准
            errors.append((sid, f"{type(e).__name__}: {e}"))
    if not ok_samples:
        return {}, errors
    return analyze(ok_samples, ok_scores), errors


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="评委校准：锚点样本上对比评委分与人工分")
    ap.add_argument("--samples", default=str(DEFAULT_SAMPLES), help="锚点样本 JSON 路径")
    ap.add_argument(
        "--judge", choices=list(JUDGE_ROLES), default="evaluator", help="要校准的评委角色"
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
        print(f"样本为空：{args.samples}（先用 --write-template 生成模板）", file=sys.stderr)
        return 2

    analysis, errors = calibrate(samples, args.judge)
    for sid, err in errors:
        print(f"  [SKIP] {sid}：{err}", file=sys.stderr)
    if not analysis:
        print("全部锚点评估失败，无法校准（先跑 run.py --preflight 检查端点）", file=sys.stderr)
        return 1
    print(render_report(args.judge, analysis))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
