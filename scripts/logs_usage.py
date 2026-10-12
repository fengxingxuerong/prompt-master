"""logs/ 只读用量审计：先看清楚什么在膨胀，再决定删不删（本工具**从不删**）。

为什么做成只读（2026-10-13 立）：
`logs/` 是 gitignored 的运行产物目录，README 明说"会持续膨胀"；实测本机 169 MB /
15221 文件，其中 `archive/2026-09-17-交付归档/docker_sim_*/venv/` 一族内嵌虚拟环境
（site-packages 里的 .pyd/.dll）是纯死重，而 `run_*.json` / `report_*.md` 是
`docs/evaluation.md` 逐轮引用过的**证据**（按 run id 点名）。两者的处置方式完全不同
——把它们混在一个"清理脚本"里，迟早会有人手滑删掉证据。
所以本工具只做两件事：
1. 按类别汇总（文件数 / 体积 / 占比）与最老最新的时间跨度；
2. 标出"可回收"清单（内嵌 venv、__pycache__、可再生的缓存），并**只打印**建议命令。

删除动作要么由所有者手动执行，要么等一条显式授权的新命令——不在这里发生。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 可回收 = 可再生 / 纯死重；证据类 = 被文档按 id 引用过，别删。
RECLAIMABLE_DIRS = ("venv", "site-packages", "__pycache__", ".pytest_cache")
RECLAIMABLE_SUFFIXES = ("_cache.json",)


def classify(path: Path, root: Path) -> str:
    """给一条路径归类。类别名直接进报表，改这里就要同步改测试。"""
    rel_parts = path.relative_to(root).parts
    name = path.name
    if any(part in RECLAIMABLE_DIRS for part in rel_parts):
        return "reclaimable_dirs"
    if name.endswith(RECLAIMABLE_SUFFIXES) or name.startswith("target_cache"):
        return "reclaimable_cache"
    if name.startswith("run_") and name.endswith(".json"):
        return "evidence_runs"
    if name.startswith("report_") and name.endswith(".md"):
        return "evidence_reports"
    if name.startswith("_"):
        return "scratch"
    if name.endswith(".log"):
        return "logs"
    if name.endswith(".py"):
        return "probes"
    return "other"


def scan(root: Path) -> dict[str, dict[str, int]]:
    cats: dict[str, dict[str, int]] = {}
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        cat = classify(p, root)
        rec = cats.setdefault(cat, {"files": 0, "bytes": 0})
        rec["files"] += 1
        rec["bytes"] += p.stat().st_size
    return cats


def top_files(root: Path, n: int) -> list[tuple[int, str]]:
    files = [p for p in root.rglob("*") if p.is_file()]
    files.sort(key=lambda p: p.stat().st_size, reverse=True)
    return [(p.stat().st_size, str(p.relative_to(root))) for p in files[:n]]


def reclaimable_dirs(root: Path) -> list[tuple[int, str]]:
    """列出可回收的顶层内嵌目录（venv/site-packages/__pycache__），按体积降序。

    只报**目录**不报里面的散文件：回收建议是按目录整删的，列一万个 .pyd 没有意义。
    """
    out: list[tuple[int, str]] = []
    for d in root.rglob("*"):
        if not d.is_dir() or d.name not in RECLAIMABLE_DIRS:
            continue
        # 只取最外层命中目录：venv/Lib/site-packages 只报 venv 那条
        if any(part in RECLAIMABLE_DIRS for part in d.relative_to(root).parts[:-1]):
            continue
        size = sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
        out.append((size, str(d.relative_to(root))))
    out.sort(reverse=True)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="logs/ 只读用量审计（从不删除，只给建议）")
    ap.add_argument("--root", default="logs", help="审计根目录（默认 logs/）")
    ap.add_argument("--json", action="store_true", help="stdout 恰好一个 JSON 对象")
    ap.add_argument("--top", type=int, default=8, help="附带列出的最大文件数（默认 8）")
    args = ap.parse_args(argv)

    root = Path(args.root)
    if not root.is_dir():
        print(f"目录不存在：{root}", file=sys.stderr)
        return 2

    cats = scan(root)
    total_bytes = sum(v["bytes"] for v in cats.values())
    total_files = sum(v["files"] for v in cats.values())
    rec_dirs = reclaimable_dirs(root)
    reclaimable_bytes = sum(b for b, _ in rec_dirs) + cats.get("reclaimable_cache", {}).get(
        "bytes", 0
    )
    report = {
        "root": str(root),
        "total_files": total_files,
        "total_bytes": total_bytes,
        "categories": cats,
        "reclaimable_bytes": reclaimable_bytes,
        "reclaimable_dirs": [{"bytes": b, "path": p} for b, p in rec_dirs],
        "top_files": [{"bytes": b, "path": p} for b, p in top_files(root, args.top)],
    }

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
        return 0

    print(f"logs/ 用量审计（只读）：{root}")
    print(f"合计 {total_files} 文件 / {total_bytes / 1048576:.2f} MB")
    print()
    print(f"{'类别':<20}{'文件数':>8}{'体积(MB)':>12}{'占比':>8}")
    for cat, v in sorted(cats.items(), key=lambda kv: -kv[1]["bytes"]):
        pct = v["bytes"] / total_bytes * 100 if total_bytes else 0.0
        print(f"{cat:<20}{v['files']:>8}{v['bytes'] / 1048576:>12.2f}{pct:>7.1f}%")
    print()
    if rec_dirs:
        print(f"可回收目录（建议整删，共 {reclaimable_bytes / 1048576:.2f} MB）：")
        for b, p in rec_dirs[:15]:
            print(f"  {b / 1048576:>8.2f} MB  {p}")
        print("  ⚠️ 证据类（run_*.json / report_*.md）不在此列——删除动作请显式确认后手动执行。")
    else:
        print("未发现可回收目录。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
