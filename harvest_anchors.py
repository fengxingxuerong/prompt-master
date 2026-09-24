#!/usr/bin/env python3
"""从真实跑批记录里采集评委校准锚点候选，产出待人工确认的样本文件。

为什么需要它：`judge_calibration/samples.json` 只有 11 条锚点，而校准的一切结论
（bias / MAE / r）都建立在它上面 —— 11 个人工分里只有 3 条落在 8.0 判定线附近，
这条线恰恰是整个闭环的唯一决策点。手工编锚点有个致命问题：**谁来编都会编出一套
和自己一致的卷子**，评委与锚点出自同一套 rubric 理解时，r 高只证明同温层，不证明可信。

所以这里不从想象里造样本，而是从 `logs/run_*.json` 已经真实发生过的
（原始需求 × 被测提示词 × 测试输入 × 目标模型真输出）四元组里挖，并且：

1. **只挖真端点**：trace 通道含 fake、或 target_model 是 stub 的一律剔除。
   假输出的分数是噪声的函数，拿它当锚点等于给仪表校准一面镜子。
2. **本工具不填 human_score**（写 null + `confirmed: false`）。人工分只能由人给；
   `calibrate_judge.py` 会跳过未确认条目。评委自己给的分记在 `judge_score` 里，
   仅用于排序复核优先级 —— 让评委的分进人工分等于用被校对象当标准答案。
3. **附带机械线索** `hints`：数字回查、截断尾巴、伪装的"看起来认真"标记。
   它们不依赖任何 LLM，是人工复核时可以一眼验的客观事实。

用法：
    python harvest_anchors.py                    # 扫 logs/，写候选 + 复核清单
    python harvest_anchors.py --per-band 12      # 每个分数带上限
    python harvest_anchors.py --out X.json --review Y.md
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

from pm.bootstrap import ensure_utf8_stdio  # noqa: E402

ensure_utf8_stdio()

ROOT = Path(__file__).parent
DEFAULT_LOG_DIR = ROOT / "logs"
DEFAULT_OUT = ROOT / "judge_calibration" / "samples.candidates.json"
DEFAULT_REVIEW = ROOT / "judge_calibration" / "REVIEW.md"
DEFAULT_EXISTING = [
    ROOT / "judge_calibration" / "samples.json",
    ROOT / "judge_calibration" / "samples_disputed.json",
]

# 目标模型为这些值时，输出不是真模型产出的，不能当锚点素材。
_STUB_TARGETS = {"", "stub-model", "stub", "fake", "mock", "none", "test"}

# 分数带：判定线 8.0 两侧要分开统计——合并成"8 分以上"就看不见线附近的分歧。
BANDS: tuple[tuple[str, float, float], ...] = (
    ("<6.0", 0.0, 6.0),
    ("6.0-6.9", 6.0, 7.0),
    ("7.0-7.9", 7.0, 8.0),
    ("8.0-8.9", 8.0, 9.0),
    (">=9.0", 9.0, 10.01),
)

_NUM_RE = re.compile(r"\d+(?:[.,]\d+)?%?")
# rubric 点名"看着像质量、其实不是证据"的东西：命中即降权复核优先级。
_PACKAGING = ("思考过程：", "Thought process:", "分析如下：", "以上完全符合要求", "已达标")
_TRUNCATED = ("…", "...", "（略）", "[截断]")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:10]


def _band_of(score: float) -> str:
    for name, lo, hi in BANDS:
        if lo <= score < hi:
            return name
    return "unknown"


def _nums(text: str) -> set[str]:
    """抽取数字 token，去掉千分位；用于"输出里的数在输入里存不存在"的机械回查。"""
    return {t.replace(",", "").rstrip("%") for t in _NUM_RE.findall(text) if t.strip("%")}


def _hints(test_input: str, output: str) -> list[str]:
    """不依赖 LLM 的客观线索。人工分最难判的是"这算不算编造"，先把可核的部分核掉。"""
    out: list[str] = []
    src = _nums(test_input)
    # 只回查带小数或百分比的断言性数字：整数 1/2/3 多是序号与条目计数，命中全是噪音。
    suspect = sorted(
        t for t in _nums(output) if t not in src and ("." in t or t.endswith("%")) and len(t) > 1
    )
    if suspect:
        out.append(f"输出中 {len(suspect)} 个数字在输入里找不到来源（需人工判断是否推算）：{suspect[:6]}")
    if any(p in output for p in _PACKAGING):
        out.append("含 rubric 点名的伪装标记（空抬头/自评达标），这些不应算加分")
    if any(output.rstrip().endswith(t) for t in _TRUNCATED):
        out.append("结尾疑似截断")
    if len(output.strip()) < 40:
        out.append("输出极短（<40 字），按 rubric 属大幅扣分项")
    return out


def _eval_for(evaluations: list[dict[str, Any]], index: str) -> dict[str, Any] | None:
    """按 test_case_index 找对应评估。日志里该字段既可能是 int 也可能是 str。"""
    for ev in evaluations:
        if str(ev.get("test_case_index", 0)) == str(index):
            return ev
    return None


def collect(log_dir: Path) -> list[dict[str, Any]]:
    """扫全部 run 日志，返回真端点产出的候选四元组（含评委分与线索）。"""
    rows: list[dict[str, Any]] = []
    for path in sorted(log_dir.glob("run_*.json")):
        try:
            d: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        channels = {str(e.get("channel")) for e in (d.get("trace") or [])}
        if "fake" in channels:
            continue
        task = str(d.get("task") or "").strip()
        evaluations = list(d.get("evaluations") or [])
        if not task or not evaluations:
            continue
        for tr in d.get("test_runs") or []:
            target = str(tr.get("target_model") or "").strip()
            if target.lower() in _STUB_TARGETS or tr.get("error"):
                continue
            output = str(tr.get("output") or "").strip()
            test_input = str(tr.get("test_input") or "").strip()
            prompt = str(tr.get("prompt") or "").strip()
            if not (output and test_input and prompt):
                continue
            ev = _eval_for(evaluations, str(tr.get("test_case_index", "0")))
            if not ev:
                continue
            score = ev.get("weighted_score")
            if not isinstance(score, (int, float)):
                continue
            rows.append(
                {
                    "original_task": task,
                    "context": str(d.get("context") or ""),
                    "prompt": prompt,
                    "test_input": test_input,
                    "test_output": output,
                    "judge_score": round(float(score), 2),
                    "judge_dims": ev.get("dimension_scores") or {},
                    "judge_issues": list(ev.get("issues") or [])[:4],
                    "model_reported_score": ev.get("model_reported_score"),
                    "judge": str(ev.get("judge") or ""),
                    "target_model": target,
                    "source_run": path.name,
                    "out_sha": _sha(output),
                    "hints": _hints(test_input, output),
                }
            )
    return rows


def _existing_shas(paths: list[Path]) -> set[str]:
    """已在锚点集里的输出指纹，避免同一份输出第二次进集（会人为抬高 r）。"""
    seen: set[str] = set()
    for p in paths:
        if not p.exists():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and item.get("test_output"):
                    seen.add(_sha(str(item["test_output"]).strip()))
    return seen


def select(rows: list[dict[str, Any]], per_band: int, skip_shas: set[str]) -> list[dict[str, Any]]:
    """按分数带分层 + 带内任务族去重，控制规模。

    分层的理由不是"好看"：人工分离散度低时 Pearson r 无意义（11 条老锚点的极差
    只有 2.5-9.0，而 8 条争议集只在 7.0-8.7 之间——后者算出来的 r 纯属噪音放大）。
    所以每个带都要有人工分，且优先换任务族而不是在同一族里堆条数。
    """
    picked: list[dict[str, Any]] = []
    for name, lo, hi in BANDS:
        in_band = [
            r
            for r in rows
            if r["out_sha"] not in skip_shas and lo <= r["judge_score"] < hi
        ]
        in_band.sort(key=lambda r: (r["original_task"], -len(r["test_output"])))
        used_task: dict[str, int] = {}
        chosen: list[dict[str, Any]] = []
        queue = list(in_band)
        while queue and len(chosen) < per_band:
            nxt = min(queue, key=lambda r: (used_task.get(r["original_task"], 0), len(r["original_task"])))
            queue.remove(nxt)
            used_task[nxt["original_task"]] = used_task.get(nxt["original_task"], 0) + 1
            nxt["band"] = name
            chosen.append(nxt)
        picked.extend(chosen)
    return picked


def to_samples(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """转成 calibrate_judge 可读的样本结构。human_score=null + confirmed=false。"""
    out: list[dict[str, Any]] = []
    for r in rows:
        out.append(
            {
                "id": f"h-{r['out_sha']}",
                "original_task": r["original_task"],
                "context": r["context"],
                "prompt": r["prompt"],
                "test_input": r["test_input"],
                "test_output": r["test_output"],
                "human_score": None,
                "confirmed": False,
                "band": r.get("band", _band_of(r["judge_score"])),
                "note": "；".join(r["hints"]) or "无线索命中；需人工按 rubric 逐条核对",
                "provenance": {
                    "source_run": r["source_run"],
                    "target_model": r["target_model"],
                    "judge": r["judge"],
                    "judge_score": r["judge_score"],
                    "judge_dims": r["judge_dims"],
                    "judge_issues": r["judge_issues"],
                    "model_reported_score": r["model_reported_score"],
                },
            }
        )
    return out


def render_review(samples: list[dict[str, Any]], skipped: int) -> str:
    lines: list[str] = [
        "# 锚点复核清单",
        "",
        f"> 由 `harvest_anchors.py` 生成。{len(samples)} 条候选，另剔除 {skipped} 条与现有锚点集重复的输出。",
        "",
        "## 怎么复核",
        "",
        "1. 每条只需回答一个问题：**换你来看这份输出，值不值 8.0（可上线）**，再给一个 1-10 的分。",
        "2. 打开 `judge_calibration/samples.candidates.json`，把该条的 `human_score` 从 `null` 改成分数，",
        "   并把 `confirmed` 改成 `true`。**没改的条目不参与校准**——这是刻意的：",
        "   未确认的分数一旦进集，测出来的 r 只是在读评委自己的口味。",
        "3. 复核顺序按下方清单（判定线附近 + 线索命中的优先，它们对结论的信息量最大）。",
        "4. 跑校准：`python calibrate_judge.py --samples judge_calibration/samples.candidates.json`",
        "",
        "评委自己给的分列在每条里，**用途是让你先去看它和直觉不一致的那些**，不是让你抄它。",
        "",
    ]
    ordered = sorted(samples, key=lambda s: (abs(float(s["provenance"]["judge_score"]) - 8.0), -len(s["note"])))
    for i, s in enumerate(ordered, 1):
        prov = s["provenance"]
        dims = " / ".join(f"{k}={v}" for k, v in (prov["judge_dims"] or {}).items())
        lines += [
            f"### {i}. `{s['id']}` — 带 {s['band']}，评委 {prov['judge_score']}",
            "",
            f"- 原始需求：{s['original_task'][:120]}",
            f"- 目标模型：{prov['target_model']} ｜ 评定来源：{prov['judge']} ｜ 自报分：{prov['model_reported_score']}",
            f"- 评委维度分：{dims or '（无）'}",
            f"- 机械线索：{s['note']}",
            f"- 待填：`human_score = null` → ___ ｜ `confirmed = false` → true",
            "",
            "<details><summary>被测输出</summary>",
            "",
            "```",
            s["test_output"][:1200],
            "```",
            "",
            "</details>",
            "",
        ]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="从真实跑批记录采集评委校准锚点候选")
    ap.add_argument("--log-dir", default=str(DEFAULT_LOG_DIR))
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--review", default=str(DEFAULT_REVIEW))
    ap.add_argument("--per-band", type=int, default=10, help="每个分数带最多采集几条")
    ap.add_argument("--keep-duplicate", action="store_true", help="不剔除与现有锚点集重复的输出")
    args = ap.parse_args()

    log_dir = Path(args.log_dir)
    if not log_dir.is_dir():
        print(f"日志目录不存在：{log_dir}", file=sys.stderr)
        return 2
    rows = collect(log_dir)
    if not rows:
        print("没有可用的真端点记录（全部为 fake/stub）——先去跑一轮真端点批次", file=sys.stderr)
        return 1
    skip: set[str] = set()
    if not args.keep_duplicate:
        skip = _existing_shas(DEFAULT_EXISTING)
    chosen = select(rows, max(1, args.per_band), skip)
    samples = to_samples(chosen)
    if not samples:
        print("分层后为空（可能全部与现有锚点重复）", file=sys.stderr)
        return 1

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(samples, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    n_dupes = sum(1 for r in rows if r["out_sha"] in skip)
    Path(args.review).write_text(render_review(samples, n_dupes), encoding="utf-8")
    print(f"候选锚点：{len(samples)} 条 → {out}（素材 {len(rows)} 条，其中重复 {n_dupes} 条）")
    for name, lo, hi in BANDS:
        n = sum(1 for s in samples if lo <= float(s["provenance"]["judge_score"]) < hi)
        print(f"  带 {name:<8} {n} 条")
    print(f"复核清单：{args.review}")
    print("下一步：人工把要采纳的条目填 human_score 并置 confirmed=true，未填的不进校准。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
