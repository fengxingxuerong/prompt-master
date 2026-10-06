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
   `pm/calibration.py`（run.py calibrate 的引擎）会跳过未确认条目。评委自己给的分记在 `judge_score` 里，
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
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

from pm.bootstrap import ensure_utf8_stdio  # noqa: E402

ensure_utf8_stdio()

from pm.calibration import MIN_ESTIMABLE_CELL, PASS_THRESHOLD, classify_human_source  # noqa: E402
from pm.scoring import unsourced_numbers  # noqa: E402

ROOT = Path(__file__).parent
DEFAULT_LOG_DIR = ROOT / "logs"
DEFAULT_OUT = ROOT / "judge_calibration" / "samples.candidates.json"
DEFAULT_REVIEW = ROOT / "judge_calibration" / "REVIEW.md"
# 参与去重的锚点文件：不写死清单。写死的两份（samples.json / samples_disputed.json）
# 恰好漏掉了 samples.candidates.json —— 那正是 48 条已确认人工分住的地方，
# 于是重采会把已标过的同一份输出再摊到人面前（实测 25 条里 18 条重复）。
ANCHOR_DIR = ROOT / "judge_calibration"

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


def _hints(test_input: str, output: str) -> list[str]:
    """不依赖 LLM 的客观线索。人工分最难判的是"这算不算编造"，先把可核的部分核掉。

    数字回查改用 `pm.scoring.unsourced_numbers`：与判定式评分协议同一口径。
    两处各写一套的话，采集时给人的线索和线上算分判的违规就不是同一批数字。
    """
    out: list[str] = []
    suspect = unsourced_numbers(test_input, output)
    if suspect:
        out.append(
            f"输出中 {len(suspect)} 个数字在输入里找不到来源（需人工判断是否推算）：{suspect[:6]}"
        )
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
    # 同一份输出会被多次跑批复现：id 按 out_sha 生成，不去重就是同 id 两条，
    # `load_samples` 的「样本 id 重复」防线会拒收整份文件——2026-09-26 实测发生
    # （50 条候选里 2 条重复 id，校准被卡死）。保留首次出现（glob 排序序 = 旧→新），
    # 确定性可复现；被丢的只是同一份输出在第 N 次跑批里的复读。
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for r in rows:
        if r["out_sha"] in seen:
            continue
        seen.add(r["out_sha"])
        deduped.append(r)
    return deduped


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


def select(
    rows: list[dict[str, Any]],
    per_band: int,
    skip_shas: set[str],
    prefer: tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    """按分数带分层 + 带内任务族去重，控制规模。

    分层的理由不是"好看"：人工分离散度低时 Pearson r 无意义（11 条老锚点的极差
    只有 2.5-9.0，而 8 条争议集只在 7.0-8.7 之间——后者算出来的 r 纯属噪音放大）。
    所以每个带都要有人工分，且优先换任务族而不是在同一族里堆条数。
    """
    picked: list[dict[str, Any]] = []
    # prefer 只改**访问顺序**不改每带上限：补采队列要先把最缺的格子摊到人面前，
    # 但"每带 ≤per_band 条"这条防的是同一族输出灌满候选集，拆开看是两件事。
    ordered = [b for b in BANDS if b[0] in prefer] + [b for b in BANDS if b[0] not in prefer]
    for name, lo, hi in ordered:
        in_band = [r for r in rows if r["out_sha"] not in skip_shas and lo <= r["judge_score"] < hi]
        in_band.sort(key=lambda r: (r["original_task"], -len(r["test_output"])))
        used_task: dict[str, int] = {}
        chosen: list[dict[str, Any]] = []
        queue = list(in_band)
        while queue and len(chosen) < per_band:
            nxt = min(
                queue, key=lambda r: (used_task.get(r["original_task"], 0), len(r["original_task"]))
            )
            queue.remove(nxt)
            used_task[nxt["original_task"]] = used_task.get(nxt["original_task"], 0) + 1
            nxt["band"] = name
            chosen.append(nxt)
        picked.extend(chosen)
    return picked


def to_samples(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """转成 pm.calibration 可读的样本结构。human_score=null + confirmed=false。"""
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
    band_counts = {name: 0 for name, _, _ in BANDS}
    for s in samples:
        band_counts[s.get("band", _band_of(float(s["provenance"]["judge_score"])))] += 1
    lines: list[str] = [
        "# 锚点复核清单",
        "",
        f"> 由 `harvest_anchors.py` 生成。{len(samples)} 条候选，另剔除 {skipped} 条与现有锚点集重复的输出。",
        "",
        "分带分布：" + "　".join(f"{b}:{n}" for b, n in band_counts.items()),
        "条数少的带优先填——人工分覆盖的分数带数 < 3 时，排序一致性 r 在窄区间里算，没有意义。",
        "",
        "## 怎么复核",
        "",
        "1. 每条只需回答一个问题：**换你来看这份输出，值不值 8.0（可上线）**，再给一个 1-10 的分。",
        "2. 填分有两种做法，任选：",
        "   - **推荐：填表格** `judge_calibration/score_form.csv`（同目录下，只填 `human_score` 一列，",
        "     可用 Excel/WPS 直接打开）。填完回填：",
        "     `python run.py calibrate --samples judge_calibration/samples.candidates.json --apply-scores judge_calibration/score_form.csv`",
        "     —— 只改你**填了分**的那几条（并把它们置 `confirmed=true`）；有任何一行非法就一条都不写；",
        "     表格里没填的条目继续不参与校准。",
        "     （表重新生成：`python run.py calibrate --samples … --make-form judge_calibration/score_form.csv`，",
        "     已确认条目的现值会被带出来，不会丢。）",
        "   - 或者直接改 JSON：打开 `judge_calibration/samples.candidates.json`，把该条的 `human_score`",
        "     从 `null` 改成分数，并把 `confirmed` 改成 `true`。",
        "3. **没改的条目不参与校准**——这是刻意的：未确认的分数一旦进集，测出来的 r 只是在读评委自己的口味。",
        "4. 复核顺序按下方清单（判定线附近 + 线索命中的优先，它们对结论的信息量最大）。",
        "5. 跑校准：`python run.py calibrate --samples judge_calibration/samples.candidates.json --aggregate`",
        '   （注意：人工分进锚点集指纹，回填后第一次校准与历史记录的漂移对比会判为"换考卷"，这是对的；',
        "   `--aggregate` 会在报告末尾附上跨轮合并读数与置信区间，口径见 `docs/evaluation.md` §十四。）",
        "",
        "评委自己给的分列在每条里，**用途是让你先去看它和直觉不一致的那些**，不是让你抄它。",
        "",
    ]
    ordered = sorted(
        samples, key=lambda s: (abs(float(s["provenance"]["judge_score"]) - 8.0), -len(s["note"]))
    )
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


def anchor_files(anchor_dir: Path) -> list[Path]:
    """锚点目录里所有样本文件的唯一枚举点（示例文件不算数据）。

    去重集、覆盖度表、命中率表、覆盖写保护必须读同一份清单 —— 各写一份就一定会有
    其中一处漏掉某个文件，而漏掉的恰好是装着人工分的那个。
    """
    try:
        return [p for p in sorted(anchor_dir.glob("samples*.json")) if "example" not in p.name]
    except OSError:
        return []


def count_labeled(path: Path) -> int:
    """目标文件里已带人工分（或已 confirmed）的条目数。写保护用它。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    rows = (
        data
        if isinstance(data, list)
        else (data.get("samples") if isinstance(data, dict) else None)
    )
    return sum(
        1
        for x in rows or []
        if isinstance(x, dict) and (x.get("human_score") not in (None, "") or x.get("confirmed"))
    )


def _labeled_anchors(anchor_dir: Path) -> list[dict[str, Any]]:
    """读所有锚点样本文件里**已有人工分**的条目（示例文件不算数据）。"""
    out: dict[str, dict[str, Any]] = {}
    dupes = 0
    for path in anchor_files(anchor_dir):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        rows = (
            data
            if isinstance(data, list)
            else (data.get("samples") if isinstance(data, dict) else None)
        )
        for item in rows or []:
            if not isinstance(item, dict) or item.get("human_score") in (None, ""):
                continue
            # 同一 id 会同时出现在多个样本文件里（ab.json 与 samples.json 实测重叠 18 条），
            # 按行累加会把一格数成两格——覆盖度这张表本身就是"还差几条"的判据，重复即虚高。
            sid = str(item.get("id"))
            if sid in out:
                dupes += 1
                continue
            out[sid] = item
    if dupes:
        print(f"（已按 id 去重：{dupes} 条锚点跨文件重复出现）", file=sys.stderr)
    return list(out.values())


def _human_source(item: dict[str, Any]) -> str:
    """与 pm.calibration 同一判据：缺 provenance.human_score_source 一律 unknown。"""
    return classify_human_source(item)


def coverage_by_human_cell(anchor_dir: Path | None = None) -> dict[str, Any]:
    """现有锚点在"人工标签格子"上的覆盖度，以及每个格子还差几条。

    为什么要单算这一张表：`select()` 一直按**评委分带**分层（见其 docstring 的理由），
    但 κ/一致率的可估性取决于**人工标签**的格子是否填满。评委没有判别力时这两件事完全
    脱钩 —— 实测就是补采了一轮 45 条，所有者亲判的"达标"格还是只有 1 条。
    门槛与 pm.calibration 同源（MIN_ESTIMABLE_CELL），不在这儿另立一个数。
    """
    base = anchor_dir or (ROOT / "judge_calibration")
    cells: dict[str, dict[str, int]] = {}
    for item in _labeled_anchors(base):
        src = _human_source(item)
        band = _band_of(float(item["human_score"]))
        row = cells.setdefault(src, {name: 0 for name, _, _ in BANDS})
        row[band] = row.get(band, 0) + 1
    out: dict[str, Any] = {"min_cell": MIN_ESTIMABLE_CELL, "sources": {}}
    for src, row in sorted(cells.items()):
        out["sources"][src] = {
            "bands": row,
            "n": sum(row.values()),
            "deficient": [b for b in row if row[b] < MIN_ESTIMABLE_CELL],
        }
    return out


def band_yield(anchor_dir: Path | None = None) -> dict[str, dict[str, int]]:
    """历史映射：落在某评委分带的锚点，后来被人工判成"达标"的比例。

    它替代的是"评委分带 ≈ 人工分带"这个隐含假设 —— 那个假设在评委没判别力时不成立，
    但补采又只能按评委分带挑，所以把映射实测出来当权重，而不是按带平均分。
    """
    stats: dict[str, dict[str, int]] = {}
    for item in _labeled_anchors(anchor_dir or (ROOT / "judge_calibration")):
        prov = item.get("provenance") or {}
        js = prov.get("judge_score")
        if not isinstance(js, (int, float)):
            continue
        name = next((n for n, lo, hi in BANDS if lo <= float(js) < hi), None)
        if name is None:
            continue
        row = stats.setdefault(name, {"labeled": 0, "human_pass": 0})
        row["labeled"] += 1
        row["human_pass"] += 1 if float(item["human_score"]) >= PASS_THRESHOLD else 0
    return stats


def recommended_bands(
    coverage: dict[str, Any],
    yields: dict[str, dict[str, int]],
    min_support: int = MIN_ESTIMABLE_CELL,
) -> tuple[list[str], list[str]]:
    """按"最缺的人工格子 × 历史命中率"给评委分带排序，供 select(prefer=...) 用。

    只认所有者亲判的格子：AI 代判与评委同族，拿它排出来的队列是在加固同源偏见。

    **支持度不足 min_support 的带一律排到最后**，不参与"命中率高"的竞争。
    这条是被自己用出来的：实测 48 条锚点里评委带 `<6.0` 只有 3 条被人工判过
    （其中 1 条达标），平滑后命中率 0.417 反而全场最高 ⇒ 第一版把**已经最满的那格**
    排在了补采队列第一位。拿 n=3 的比率去决定"接下来花所有者 20 分钟判哪几条"，
    正是 §三十一 立可估性门槛时要挡的事——门槛这次得管到自己头上。
    低支持度的带不删（照样要补），只是不许靠一两个偶然样本插队。

    返回 `(补采顺序, 因支持度不足被排后的带)`；第二个值专给报告披露用。
    """
    owner = coverage.get("sources", {}).get("owner") or {}
    deficient = set(owner.get("deficient") or [])
    # 目标格子：缺的是"人工达标"那一侧（8.0-8.9 / >=9.0）；owner 别的格子缺时也照补
    target = {b for b in deficient if b in ("8.0-8.9", ">=9.0")} or deficient
    if not target:
        return [], []
    strong: list[tuple[float, str]] = []
    weak: list[str] = []
    for band, st in yields.items():
        if not st["labeled"]:
            continue
        if st["labeled"] < min_support:
            weak.append(band)
            continue
        # 拉普拉斯平滑只在支持度够的带上做：它防的是"1/1 = 100%"，
        # 防不了"3 条里 1 条达标"这种整体就没有信息量的比率
        rate = (st["human_pass"] + 0.5) / (st["labeled"] + 1.0)
        strong.append((rate, band))
    names = {n for n, _, _ in BANDS}
    ranked = [b for _, b in sorted(strong, key=lambda t: (-t[0], t[1])) if b in names]
    tail = [b for b in sorted(weak) if b not in ranked and b in names]
    return ranked + tail, tail


def render_coverage(coverage: dict[str, Any], yields: dict[str, dict[str, int]]) -> str:
    lines = ["", "## 人工标签格子覆盖度（零调用，门槛与 pm.calibration 同源）", ""]
    lines.append(
        f"每个格子要 ≥{coverage['min_cell']} 条才能读出 κ（推导见 docs/evaluation.md §三十一）。"
    )
    for src, info in coverage["sources"].items():
        label = {"owner": "所有者亲判", "ai_proxy": "AI 代判（与评委同族，量的是家族一致性）"}.get(
            src, src
        )
        detail = "　".join(f"{b}:{c}" for b, c in info["bands"].items() if c)
        gap = "、".join(info["deficient"]) or "无"
        lines.append(f"- {label}：{info['n']} 条 → 缺格 {gap}")
        if detail:
            lines.append(f"  - {detail}")
    if yields:
        lines.append("- 评委分带 → 人工达标的历史命中率（用它排补采优先级）：")
        for band in sorted(yields, key=lambda b: -yields[b]["labeled"]):
            st = yields[band]
            lines.append(
                f"  - {band}：{st['human_pass']}/{st['labeled']} 条后来被人工判达标"
                f"（{(st['human_pass'] / st['labeled']):.0%}）"
            )
    rec, weak = recommended_bands(coverage, yields)
    if rec:
        lines.append(f"- 建议补采顺序（--prefer-deficient 即按此走）：{' → '.join(rec)}")
    if weak:
        lines.append(
            f"  - ⚠️ {'、'.join(weak)} 排在末尾不是因为命中率低，而是**被人工判过的条数 < "
            f"{MIN_ESTIMABLE_CELL}**，比率估不出来——n=1~3 的命中率会把已经最满的格子顶到第一位。"
        )
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="从真实跑批记录采集评委校准锚点候选")
    ap.add_argument("--log-dir", default=str(DEFAULT_LOG_DIR))
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--review", default=str(DEFAULT_REVIEW))
    ap.add_argument("--per-band", type=int, default=10, help="每个分数带最多采集几条")
    ap.add_argument("--keep-duplicate", action="store_true", help="不剔除与现有锚点集重复的输出")
    ap.add_argument(
        "--allow-overwrite-labeled",
        action="store_true",
        help="明知 --out 里有已确认人工分仍然覆盖（默认拒绝，退出码 2）",
    )
    ap.add_argument(
        "--coverage", action="store_true", help="只看人工标签格子覆盖度（零采集、零写入）"
    )
    ap.add_argument(
        "--prefer-deficient",
        action="store_true",
        help="按「最缺的人工格子 × 历史命中率」排采集顺序（默认按固定分带顺序）",
    )
    args = ap.parse_args()

    log_dir = Path(args.log_dir)
    anchor_dir = ROOT / "judge_calibration"
    cov = coverage_by_human_cell(anchor_dir)
    ylds = band_yield(anchor_dir)
    if args.coverage:
        print(render_coverage(cov, ylds))
        return 0
    if not log_dir.is_dir():
        print(f"日志目录不存在：{log_dir}", file=sys.stderr)
        return 2
    rows = collect(log_dir)
    if not rows:
        print("没有可用的真端点记录（全部为 fake/stub）——先去跑一轮真端点批次", file=sys.stderr)
        return 1
    out_path = Path(args.out)
    if not args.allow_overwrite_labeled:
        n_lab = count_labeled(out_path)
        if n_lab:
            # 默认 --out 就是装着已确认锚点的那个文件。此前"再采一轮"这个动作
            # 会把它整份覆盖，48 条人工分一次性消失——而这条路径没有任何测试打过。
            hint = (
                f"拒绝覆盖：{out_path} 里已有 {n_lab} 条带人工分/已确认的锚点。"
                + chr(10)
                + "  - 想接着补采：换目标文件，例如"
                + "    --out judge_calibration/samples.candidates.2.json"
                + chr(10)
                + "  - 确认要重写这一份（人工分将丢失，而它们是唯一不可再生的东西）："
                + "    --allow-overwrite-labeled"
            )
            print(hint, file=sys.stderr)
            return 2
    skip: set[str] = set()
    if not args.keep_duplicate:
        skip = _existing_shas(anchor_files(anchor_dir))
    prefer: tuple[str, ...] = (
        tuple(recommended_bands(cov, ylds)[0]) if args.prefer_deficient else ()
    )
    chosen = select(rows, max(1, args.per_band), skip, prefer=prefer)
    samples = to_samples(chosen)
    if not samples:
        print("分层后为空（可能全部与现有锚点重复）", file=sys.stderr)
        return 1

    out = out_path
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(samples, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    n_dupes = sum(1 for r in rows if r["out_sha"] in skip)
    # render_coverage 自己以空行开头，不额外加分隔符（上一版在这里塞了个换行字面量，
    # 被脚本层吃掉成真换行，直接把文件写成语法错）
    Path(args.review).write_text(
        render_review(samples, n_dupes) + render_coverage(cov, ylds),
        encoding="utf-8",
    )
    print(f"候选锚点：{len(samples)} 条 → {out}（素材 {len(rows)} 条，其中重复 {n_dupes} 条）")
    for name, lo, hi in BANDS:
        n = sum(1 for s in samples if lo <= float(s["provenance"]["judge_score"]) < hi)
        print(f"  带 {name:<8} {n} 条")
    print(f"复核清单：{args.review}")
    print("下一步：人工把要采纳的条目填 human_score 并置 confirmed=true，未填的不进校准。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
