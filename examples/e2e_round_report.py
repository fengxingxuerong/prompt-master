"""端到端轮次证据提取器：把一次真实运行的结果对照达标判据逐条核对。

为什么单独成脚本：每轮迭代都要回答同样几个问题（断言过没过、边界有没有过度泛化、
Δ 与盲评是否打架、最终提示词有没有那条互斥边界规则），手翻 run json 既慢又容易漏。
本脚本把判据固化成可复现输出，轮次之间可以直接对比。

用法：
    python examples/e2e_round_report.py                    # 分析最新一次 run_*.json
    python examples/e2e_round_report.py logs/run_xxx.json  # 指定某次运行
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 输入里出现金额/数字，却整段判成"数据缺失" —— P1-2 过度泛化的信号
_AMOUNT_RE = re.compile(r"\d+(?:\.\d+)?\s*(?:万|亿|元|块|美元)")
_MISSING_RE = re.compile(r"数据缺失|无数据|未提供")


def _newest_run() -> Path:
    runs = sorted((ROOT / "logs").glob("run_*.json"), key=lambda p: p.stat().st_mtime)
    if not runs:
        raise SystemExit("logs/ 下没有 run_*.json，先跑一次真实任务")
    return runs[-1]


def _load(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data.get("final_state") or data


def _final_prompt(state: dict) -> str:
    versions = state.get("prompt_versions") or []
    scored = [v for v in versions if v.get("avg_score") is not None]
    if scored:
        return max(scored, key=lambda v: (v.get("avg_score") or 0, v.get("min_score") or 0)).get(
            "prompt", ""
        )
    return (versions[-1].get("prompt", "") if versions else state.get("prompt", "")) or ""


def analyze(state: dict) -> dict:
    agg = state.get("aggregate") or {}
    base = state.get("baseline_aggregate") or {}
    pw = state.get("pairwise") or {}
    runs = state.get("test_runs") or []

    d_avg = None
    if base:
        d_avg = round(float(agg.get("avg_score", 0) or 0) - float(base.get("avg_score", 0) or 0), 2)

    assertions = []
    over_generalized = []
    for r in runs:
        a = r.get("assertion")
        if isinstance(a, dict):
            assertions.append(
                {
                    "case": r.get("test_case_index"),
                    "mode": a.get("mode"),
                    "passed": a.get("passed"),
                    "advisory": a.get("advisory"),
                    "expected": (a.get("expected") or "")[:40],
                }
            )
        inp, out = r.get("test_input") or "", r.get("output") or ""
        # 输入里有明确金额、输出却整体判缺失 → 记为过度泛化（除非输出同时给了具体数字）。
        # 比较前先去掉空白：输入写「98 万」、输出写「98万」是同一笔数，不做归一化会误判
        # （2026-09-13 第 3 轮踩过：正确输出被判成过度泛化）。
        if _AMOUNT_RE.search(inp) and _MISSING_RE.search(out):
            nums_in_inp = [re.sub(r"\s+", "", n) for n in _AMOUNT_RE.findall(inp)]
            flat_out = re.sub(r"\s+", "", out)
            if not any(n in flat_out for n in nums_in_inp):
                over_generalized.append(
                    {
                        "case": r.get("test_case_index"),
                        "input": " ".join(inp.split())[:70],
                        "output": " ".join(out.split())[:70],
                    }
                )

    prompt = _final_prompt(state)
    # 4b 代理指标：缺失口径是否**逐字段限定**（而不是整体兜底）。
    # 三种可接受的表述都算通过：
    #   ①「已有数据/其余字段照常输出」；②「完全没有 X 时，X 字段填『未提供』」这类逐字段回退；
    #   ③ 只要不出现整体兜底措辞（blanket）且确实写了缺失口径。
    # 教训（第 5 轮）：抽取类任务的合法写法是"逐字段填『未提供』"，只认「已有数据照常输出」
    # 会把合格交付物判成不合格 —— 判据要跟着任务形态走，不能绑死一种措辞。
    has_missing = bool(re.search(r"(缺失|未提供)", prompt))
    per_field_fallback = bool(
        re.search(
            # 窗口给到 24 字：字段名占位（如「订单号，order_id 填」）本身就能吃掉十几个字符
            r"(完全没有|不存在|若缺失|缺失时|缺失则|缺省时).{0,24}(填|输出|标注|标为|记为|写作)",
            prompt,
        )
    )
    loose_field_style = bool(
        re.search(r"(已有数据|其余|剩余|照常输出|照常列出|仍须输出|仍要输出)", prompt)
    )
    # 只认**正面**的逐字段写法：要么写明"已有数据照常输出"，要么写明"缺 X 时 X 字段填『未提供』"。
    # 不要加"只要不是整体兜底就算过"这种兜底分支 —— 那会让判据几乎恒真（第 5 轮试过，会把
    # 历史失败轮也判成通过）。
    per_field = has_missing and (loose_field_style or per_field_fallback)

    verdict = str(pw.get("verdict") or "") if pw else ""
    conflict = None
    if d_avg is not None and verdict:
        if d_avg > 0 and verdict == "worse":
            conflict = f"Δ={d_avg:+} 但盲评判基线胜"
        elif d_avg <= 0 and verdict == "better":
            conflict = f"Δ={d_avg:+} 但盲评判优化版胜"

    hard_failed = [a for a in assertions if not a["passed"] and not a["advisory"]]
    # 冲突被报告显式标注（渲染出的「结论冲突」段）→ 视为已处理：判据要的是"不许瞒着读者"，
    # 不是"必须没有冲突"（真实运行中两个信号确实会打架，见 2026-09-13 第 3 轮）。
    flagged = "结论冲突" in (state.get("final_report") or "")
    checks = {
        "1. 断言全通过": not hard_failed,
        "2. 达标（status=passed）": state.get("status") == "passed",
        "3. 无边界过度泛化": not over_generalized,
        "4a. Δ 与盲评一致，或冲突已在报告中标注": conflict is None or flagged,
        "4b. 缺失口径逐字段限定（非整体兜底）": per_field,
    }

    return {
        "run_id": state.get("run_id"),
        "status": state.get("status"),
        "iteration": state.get("iteration"),
        "llm_calls": state.get("llm_calls"),
        "avg": agg.get("avg_score"),
        "min": agg.get("min_score"),
        "ci_lower": agg.get("ci_lower"),
        "passed": agg.get("passed"),
        "n_cases": f"{agg.get('n_cases')}/{agg.get('n_cases_expected')}",
        "delta": d_avg,
        "baseline_avg": base.get("avg_score"),
        "pairwise": {"verdict": verdict, "votes": pw.get("votes")},
        "assertions": assertions,
        "over_generalized": over_generalized,
        "hard_failed": hard_failed,
        "injection_survival": state.get("injection_survival"),
        "quality_issues": state.get("prompt_quality_issues"),
        "checks": checks,
    }


def render(path: Path, rep: dict) -> str:
    L = [f"# 轮次报告：{path.name}", ""]
    L.append(
        f"- run_id `{rep['run_id']}` | status `{rep['status']}` | 迭代 {rep['iteration']} "
        f"| 调用 {rep['llm_calls']}"
    )
    L.append(
        f"- 均分 {rep['avg']} / 最低 {rep['min']} / 下界 {rep['ci_lower']} | 判定通过={rep['passed']} "
        f"| 用例 {rep['n_cases']}"
    )
    L.append(
        f"- 基线 {rep['baseline_avg']} → Δ {rep['delta']} | 盲评 {rep['pairwise']['verdict']} "
        f"{rep['pairwise']['votes']}"
    )
    L.append(f"- 注入存活：{rep['injection_survival']}")
    L.append("")
    L.append("## 判据核对")
    for k, v in rep["checks"].items():
        L.append(f"- {'✅' if v else '❌'} {k}")
    L.append("")
    if rep["assertions"]:
        L.append("## 事实断言")
        for a in rep["assertions"]:
            mark = "⚠️ 仅提醒" if a["advisory"] else ("✅" if a["passed"] else "❌")
            L.append(f"- case#{a['case']} {a['mode']} {mark} 期望：{a['expected']}")
        L.append("")
    if rep["over_generalized"]:
        L.append("## 边界过度泛化（输入有数据却判缺失）")
        for o in rep["over_generalized"]:
            L.append(f"- case#{o['case']} 输入：{o['input']}")
            L.append(f"  输出：{o['output']}")
        L.append("")
    if rep["quality_issues"]:
        L.append("## 质量门警告")
        for q in rep["quality_issues"]:
            L.append(f"- v{q.get('iteration')}：{q.get('issues')}")
        L.append("")
    return "\n".join(L)


if __name__ == "__main__":
    p = Path(sys.argv[1]) if len(sys.argv) > 1 else _newest_run()
    state = _load(p)
    rep = analyze(state)
    text = render(p, rep)
    print(text)
    out = ROOT / "logs" / f"round_analysis_{p.stem.replace('run_', '')}.md"
    out.write_text(text, encoding="utf-8")
    print(f"\n（已写入 {out}）")
