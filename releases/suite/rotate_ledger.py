"""JSONL 台账轮转 v3：多台账、路径不写死、失败不静默。

v2 的三个问题（2026-10-31 只读审计实测）：

1. **路径写死成 `D:\\projects\\prompt-master\\logs\\modelhub_ledger.jsonl`**：换机器/换检出目录
   就指向一个不存在的地方，函数只印一句 "no ledger file" 然后**当成成功**；
2. **只轮转一份台账**：三套服务的 `releases/*/data/*.jsonl` 完全不在范围里，而它们才是
   天天在长的那些；
3. **失败被静默吞掉**：Windows 上文件被服务占用时 `Path.replace` 抛 `PermissionError`，
   watchdog 每 30 秒调一次、每次都只 print 一行就过去 —— 台账实际上**从未被轮转过**，
   而巡检日志里看不出任何异常。

v3 的口径：
- 目标是 `logs/*.jsonl` + `releases/*/data/*.jsonl`，在**调用时**按模块位置解析，
  所以换机器、换检出目录都不用改；
- 任一份失败都进 `errors`，由调用方（watchdog）落告警，不再只 print；
- `rotate()` 返回结构化报告，供巡检日志与测试消费。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

if hasattr(sys.stdout, "reconfigure"):  # 被 exec 进来时 stdout 未必是 TextIOWrapper
    sys.stdout.reconfigure(encoding="utf-8")

# 仓库根 = 本文件上两级（releases/suite/ → 仓库根）
ROOT = Path(__file__).resolve().parents[2]
# 轮转范围：主产品台账 + 三套服务的运营台账
LEDGER_GLOBS: tuple[str, ...] = ("logs/*.jsonl", "releases/*/data/*.jsonl")
MAX = 1_000_000
KEEP = 3


def targets(root: Path | None = None) -> list[Path]:
    """当前应当纳入轮转的台账清单（按 glob 现算，不缓存）。"""
    base = Path(root) if root is not None else ROOT
    found: list[Path] = []
    for pattern in LEDGER_GLOBS:
        found.extend(sorted(p for p in base.glob(pattern) if p.is_file()))
    return found


def rotate_one(path: Path, *, force: bool = False) -> dict[str, Any]:
    """轮转一份台账。返回 {path, size, rotated, error}；**不吞异常**，折进 error 字段。"""
    rec: dict[str, Any] = {"path": str(path), "size": 0, "rotated": False, "error": ""}
    try:
        if not path.exists():
            rec["error"] = "文件不存在"
            return rec
        size = path.stat().st_size
        rec["size"] = size
        if not force and size < MAX:
            return rec
        base = str(path)
        gen3, gen2, gen1 = Path(base + ".3"), Path(base + ".2"), Path(base + ".1")
        if gen3.exists():
            gen3.unlink()
        if gen2.exists():
            gen2.replace(gen3)
        if gen1.exists():
            gen1.replace(gen2)
        path.replace(gen1)
        rec["rotated"] = True
    except OSError as e:
        # 典型现场：Windows 上服务正开着这个文件 ⇒ PermissionError。
        # 这里如实记下来让调用方告警，而不是 print 一行就当过去了。
        rec["error"] = f"{type(e).__name__}: {e}"
    return rec


def rotate(force: bool = False, root: Path | None = None) -> dict[str, Any]:
    """轮转全部台账，返回汇总报告：{scanned, rotated, skipped, errors, items}。"""
    items = [rotate_one(p, force=force) for p in targets(root)]
    errors = [i for i in items if i["error"]]
    rotated = [i for i in items if i["rotated"]]
    report = {
        "scanned": len(items),
        "rotated": len(rotated),
        "skipped": len(items) - len(rotated) - len(errors),
        "errors": [f"{i['path']}: {i['error']}" for i in errors],
        "items": items,
        "max_bytes": MAX,
        "keep": KEEP,
    }
    if not items:
        print(f"no ledger file（范围内 0 份，root={root or ROOT}）")
    else:
        print(
            f"scanned={report['scanned']} rotated={report['rotated']} "
            f"skipped={report['skipped']} errors={len(report['errors'])}"
        )
        for e in report["errors"]:
            print(f"  ⚠️ 轮转失败：{e}", file=sys.stderr)
    return report


if __name__ == "__main__":
    rotate(force="--force" in sys.argv)
