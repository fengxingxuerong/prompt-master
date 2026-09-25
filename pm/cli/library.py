"""library 子命令：跨任务提示词资产库（记忆层·读侧 + 推荐入口）。（自 run.py 拆出，逻辑逐字保留）"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .history import _TERMINAL_STATUS
from .support import EXIT_CONFIG, EXIT_FAILED, log_dir

# --------------------------------------------------------------------------
# library 子命令：跨任务提示词资产库（记忆层·读侧）
#
# 记忆不是另建一套存储，而是把已经存在的 run 历史"组织成可检索的资产"：
# 每个终态运行的最佳提示词 + 它的分数、用例集与报告路径。检索命中后用
# --export 直接导出成品 —— 让"上次优化过类似需求"从印象变成可查询的事实。
# 终态集合沿用 history 段的 _TERMINAL_STATUS 定义。
# --------------------------------------------------------------------------


def _text_bigrams(text: str) -> set[str]:
    """中文/混排文本的字符 bigram 集合：不依赖分词，对短文本相似度足够稳。"""
    t = "".join(text.split())
    if len(t) < 2:
        return {t} if t else set()
    return {t[i : i + 2] for i in range(len(t) - 1)}


def _dice(a: set[str], b: set[str]) -> float:
    """Dice 系数：比 Jaccard 对长短不等的文本更平滑。"""
    if not a or not b:
        return 0.0
    return 2 * len(a & b) / (len(a) + len(b))


# 所有任务描述共享的模板短语（"帮我写一个 prompt 让 AI ..."）——不剥离的话，
# 模板前缀的重叠会淹没真正的领域相似性（实测 0.124 的假相似全来自它）
_TEMPLATE_NOISE = ("帮我写", "写一个", "写个", "prompt", "提示词", "让ai", "让 ai", "的prompt")


def _task_sim_grams(text: str) -> set[str]:
    cleaned = (text or "").lower()
    for noise in _TEMPLATE_NOISE:
        cleaned = cleaned.replace(noise, "")
    return _text_bigrams(cleaned)


def _library_recommend(ns: argparse.Namespace) -> int:
    """记忆层·写侧入口：新任务 → 历史相似资产 top-3。

    相似度 = Dice(新任务 bigram, 历史任务 bigram)——只比 task 对 task：
    prompt 全文会稀释 Dice（长文档分母大），而"任务相似"本来就是任务层面的事。
    推荐结果建议两种用法：把高分资产当 few-shot 参考塞进新任务的 context，
    或经 --export 作为修订起点。
    """
    new_grams = _task_sim_grams(ns.task_text or "")
    scored: list[tuple[float, dict[str, Any]]] = []
    for f in sorted(log_dir().glob("run_*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if d.get("status") != "passed":
            continue
        channels = {str(e.get("channel")) for e in (d.get("trace") or [])}
        if "fake" in channels:
            continue
        prompt = str(d.get("prompt") or "")
        if not prompt.strip():
            continue
        score = _dice(new_grams, _task_sim_grams(str(d.get("task") or "")))
        if score > 0.05:
            agg = d.get("aggregate") or {}
            scored.append(
                (
                    score,
                    {
                        "run_id": d.get("run_id"),
                        "similarity": round(score, 3),
                        "avg_score": agg.get("avg_score"),
                        "task": str(d.get("task") or "")[:60],
                        "prompt_chars": len(prompt),
                    },
                )
            )
    # 同任务多轮去重：只保留每条 task 的最高分 run（否则 12 轮 e2e 的同一任务会霸榜）。
    # 同 task 的相似度必然相同，所以这里按分数挑，而不是按遍历序先到先得
    best_by_task: dict[str, tuple[float, dict[str, Any]]] = {}
    for score, item in scored:
        key = item["task"]
        cur = best_by_task.get(key)
        if cur is None or (item["avg_score"] or 0) > (cur[1]["avg_score"] or 0):
            best_by_task[key] = (score, item)
    ranked = sorted(
        best_by_task.values(),
        key=lambda t: (-t[0], -(t[1]["avg_score"] or 0)),
    )
    top = [item for _, item in ranked[:3]]

    if not top:
        print(
            json.dumps(
                {"recommendations": [], "note": "资产库中没有相似的历史任务，按全新需求跑"},
                ensure_ascii=False,
            )
        )
        return 0

    payload = {
        "recommendations": top,
        "usage_hint": "高分相似资产可作 few-shot 参考（塞进 --context），"
        "或 library --export 后作为新任务的修订起点；"
        "资产跑分高于本轮基线时，优先借鉴其结构而不是重写",
    }
    if ns.json:
        print(json.dumps({"task_text": ns.task_text, **payload}, ensure_ascii=False, indent=2))
        return 0
    print(f"新任务：{ns.task_text}\n\n相似资产 top-{len(top)}：")
    for t in top:
        print(f"  相似度 {t['similarity']:.3f}  {t['avg_score']} 分  {t['run_id']}  {t['task']}")
    print(f"\n{payload['usage_hint']}")
    return 0


def _library_command(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="run.py library")
    ap.add_argument(
        "--query", default=None, help="关键词：匹配任务描述或提示词正文（大小写不敏感）"
    )
    ap.add_argument("--all", action="store_true", help="包含未达标交付的运行（默认只列 passed）")
    ap.add_argument("--json", action="store_true", help="输出 JSON（含提示词全文，智能体消费）")
    ap.add_argument("--export", default=None, metavar="RUN_ID", help="导出指定运行的最佳提示词")
    ap.add_argument("--out", default=None, help="导出路径（默认 logs/exports/<run_id>.prompt.md）")
    ap.add_argument(
        "--recommend",
        action="store_true",
        help="记忆层·写侧入口：按新任务文本推荐历史相似资产（top-3，Dice bigram 相似度）",
    )
    ap.add_argument("--task-text", default=None, help="配合 --recommend：新任务的需求描述")
    ns = ap.parse_args(argv)

    if ns.recommend:
        if not (ns.task_text or "").strip():
            ap.error("--recommend 需要 --task-text 提供新任务描述")
        return _library_recommend(ns)

    if ns.export:
        target: dict[str, Any] | None = None
        for f in sorted(
            log_dir().glob("run_*.json"), key=lambda p: p.stat().st_mtime, reverse=True
        ):
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if d.get("run_id") == ns.export:
                channels = {str(e.get("channel")) for e in (d.get("trace") or [])}
                target = {
                    "run_id": d.get("run_id"),
                    "task": d.get("task"),
                    "prompt": d.get("prompt"),
                    "status": d.get("status"),
                    "avg_score": (d.get("aggregate") or {}).get("avg_score"),
                    "demo": "fake" in channels,
                }
                break
        if not target:
            print(f"找不到运行：{ns.export}", file=sys.stderr)
            return EXIT_CONFIG
        if not (target.get("prompt") or "").strip():
            print(f"运行 {ns.export} 没有可导出的提示词", file=sys.stderr)
            return EXIT_FAILED
        out = Path(ns.out) if ns.out else log_dir() / "exports" / f"{ns.export}.prompt.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        header = (
            f"# Prompt 资产（run_id: {target['run_id']}）\n\n"
            f"- 任务：{target.get('task', '')}\n"
            f"- 状态：{target.get('status')}  平均分：{target.get('avg_score')}\n\n"
            f"---\n\n"
        )
        out.write_text(header + str(target["prompt"]), encoding="utf-8")
        print(json.dumps({"exported": str(out), "run_id": ns.export}, ensure_ascii=False))
        return 0

    entries: list[dict[str, Any]] = []
    for f in sorted(log_dir().glob("run_*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        status = d.get("status")
        prompt = str(d.get("prompt") or "")
        if status not in _TERMINAL_STATUS or not prompt.strip():
            continue
        if not ns.all and status != "passed":
            continue
        channels = {str(e.get("channel")) for e in (d.get("trace") or [])}
        agg = d.get("aggregate") or {}
        entry = {
            "run_id": d.get("run_id"),
            "task": str(d.get("task") or "")[:60],
            "status": status,
            "avg_score": agg.get("avg_score"),
            "ci_lower": agg.get("ci_lower"),
            "delta_vs_baseline": (
                round(agg["avg_score"] - (d.get("baseline_aggregate") or {}).get("avg_score"), 2)
                if isinstance(agg.get("avg_score"), (int, float))
                and isinstance((d.get("baseline_aggregate") or {}).get("avg_score"), (int, float))
                else None
            ),
            "llm_calls": d.get("llm_calls", 0),
            "prompt_chars": len(prompt),
            "prompt": prompt,
            "demo": "fake" in channels,
        }
        if ns.query:
            q = ns.query.lower()
            hay = (str(d.get("task") or "") + "\n" + prompt).lower()
            if q not in hay:
                continue
        entries.append(entry)

    if ns.json:
        print(json.dumps({"n": len(entries), "assets": entries}, ensure_ascii=False, indent=2))
        return 0

    if not entries:
        hint = f"（query={ns.query!r}）" if ns.query else ""
        print(f"资产库没有匹配的提示词{hint}。跑几个真实任务后这里会积累起来。")
        return 0

    print(
        f"提示词资产 {len(entries)} 条{'（含未达标交付，加 --all 才显示）' if not ns.all else ''}：\n"
    )
    for e in entries:
        dl = f"{e['delta_vs_baseline']:+.2f}" if e["delta_vs_baseline"] is not None else "  -"
        print(
            f"  [{e['status']:<16}] {e['avg_score'] if e['avg_score'] is not None else ' -'} 分"
            f"  Δ基线 {dl:>6}  {e['llm_calls']:>3} 次调用  {e['run_id']}"
        )
        print(f"      {e['task']}")
    print("\n导出成品：python run.py library --export <run_id> [--out 路径]")
    return 0
