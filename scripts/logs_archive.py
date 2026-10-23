"""logs/ 证据归档：把旧的 run_*.json / report_*.md 移进子目录（**移动 + 留档**，绝不删除）。

为什么需要这个工具（ops 承诺今天落地，2026-10-23）：
`docs/operations.md` 的「日志会膨胀」一条早说了"定期把 run_*.json / report_*.md 归档到
子目录"，但一直**只是建议，没有工具**。logs_usage.py 是只读审计（它刻意不删任何东西，
tests/test_logs_usage.py 钉死了"零写入"）；doctor 只会在 >500MB 时给一句提示。缺的正是
"真正归档"这一步。

三条不可破的边界（与仓库既有的治理判断一脉相承）：
1. **移动不是删除**。run_*.json / report_*.md 是 `docs/evaluation.md`、`docs/iteration/`
   等按 run id 点名的**证据**。本工具只把它们从一个目录移到另一个目录，原始内容一封不动；
   归档清单写进 manifest，原路径→新路径可回查。绝不 truncate / 绝不改写字节。
2. **当前仍被文档引用的证据不进归档**。归档只挑**旧 + 未被 docs 按 run id 点名**的，
   避免"归档了,某篇文档还指着它"。
3. **默认只报不动**。`--commit` 才实际移动；否则 dry-run 只列会动什么（与 release_pypi 的
   --check 先验、绝不默许破坏性写 同一精神）。

不归什么（由其它设施负责）：
- 台账（logs/modelhub_ledger.jsonl、logs/notifications.jsonl、校准账本）——事实源，原则性不动；
- 缓存（*_cache.json、target_cache.*、断点 *_ckpt.sqlite）——功能依赖，清了丢复用；
- claimable 死重（内嵌 venv/__pycache__）——logs_usage.py 只读列出，由 owner 手动整删。
本工具只认 `run_*.json` + `report_*.md` 两类证据产物，绝不越界。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# 与 run.py 同一套引导（同 logs_usage.py 的理由：⚠️ U+26A0 在 GBK 码页会崩）
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
from pm.bootstrap import ensure_utf8_stdio  # noqa: E402

# 文档里按 run id 点名的形态：`run_<12+十六进制>`（可能带 .json 后缀）
_RUN_ID_RE = re.compile(r"run_([0-9a-f]{12,20})")
# 只认这两类证据产物（与 logs_usage.classify 的 evidence_ 对表一致）
_EVIDENCE = [
    lambda n: n.startswith("run_") and n.endswith(".json"),
    lambda n: n.startswith("report_") and n.endswith(".md"),
]


def _doc_references() -> set[str]:
    """扫 docs/ 收罗被按 run id 点名的证据集合（id 本身）。

    只收 `run_<hex>` 串里的 hex id；不带前缀的裸 id 不认（避免误伤）。
    扫的是 docs/ 下的 .md —— 那是"按 run id 点名"的出处，与 evaluation.md 逐轮引用的先例一致。
    """
    refs: set[str] = set()
    docs = _ROOT / "docs"
    if not docs.is_dir():
        return refs
    for p in docs.rglob("*.md"):
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:  # 个别权限 / 扫描中的临时文件
            continue
        refs.update(_RUN_ID_RE.findall(text))
    return refs


def _candidate_files(root: Path) -> list[Path]:
    return [p for p in root.glob("*.json") if p.is_file() and _EVIDENCE[0](p.name)] + [
        p for p in root.glob("*.md") if p.is_file() and _EVIDENCE[1](p.name)
    ]


def _run_id(name: str) -> str | None:
    # run_ 与 report_ 前缀都是同一 run 的证据；文档点名按 run_<hex> 记，这里两种都认，
    # 保证 report_<id>.md 能与它同 run 的 json 共享同一个保护集合。
    m = re.search(r"(?:run|report)_([0-9a-f]{12,20})", name)
    return m.group(1) if m else None


def _older_than_days(path: Path, days: int, now: float) -> bool:
    try:
        age_days = (now - path.stat().st_mtime) / 86400
    except OSError:
        return False  # 读不到时间戳就别归档，保守
    return age_days > days


def plan(root: Path, older_than: int, protect: set[str]) -> tuple[list[Path], dict[str, str]]:
    """返回 (待归档文件, 每个文件的 run id 映射 filename->id)。"""
    now = time.time()
    picks: list[Path] = []
    ids: dict[str, str] = {}
    for p in _candidate_files(root):
        rid = _run_id(p.name)
        if rid and rid in protect:
            continue  # 仍被文档点名 → 保留在原位
        if _older_than_days(p, older_than, now):
            picks.append(p)
            if rid:
                ids[p.name] = rid
    picks.sort(key=lambda p: p.name)
    return picks, ids


def main(argv: list[str] | None = None) -> int:
    ensure_utf8_stdio()  # 必须早于任何 print / argparse 报错
    ap = argparse.ArgumentParser(
        description="logs/ 证据归档：把旧的 run_*.json / report_*.md 移进 archive/（移动+留档，不删）"
    )
    ap.add_argument(
        "--root", default=str(_ROOT / "logs"), help="证据产物根目录（默认 <repo>/logs）"
    )
    ap.add_argument("--older-than", type=int, default=30, help="只归档超过 N 天未动的（默认 30）")
    ap.add_argument(
        "--commit",
        action="store_true",
        help="实际移动（默认 dry-run 只列）。移动 = 换目录，不改字节、不删文件",
    )
    ap.add_argument("--json", action="store_true", help="stdout 恰好一个 JSON 对象（同全仓契约）")
    args = ap.parse_args(argv)

    root = Path(args.root)
    if not root.is_dir():
        print(f"目录不存在：{root}", file=sys.stderr)
        return 2

    protect = _doc_references()
    picks, ids = plan(root, args.older_than, protect)

    result = {
        "root": str(root),
        "older_than_days": args.older_than,
        "protected_by_docs": len(protect),
        "candidates": [str(p.relative_to(root)) for p in picks],
        "moved": [],
        "commit": args.commit,
    }

    if args.commit and picks:
        archive = root / "archive" / datetime.now(timezone.utc).strftime("%Y-%m")
        archive.mkdir(parents=True, exist_ok=True)
        manifest = {"generated_at": datetime.now(timezone.utc).isoformat(), "entries": []}
        for p in picks:
            dst = archive / p.name
            if dst.exists():  # 已存在同名 → 加时间戳防覆盖证据
                dst = archive / f"{p.stem}.{int(time.time())}{p.suffix}"
            p.replace(dst)
            rid = ids.get(p.name)
            manifest["entries"].append(
                {"from": str(p.relative_to(root)), "to": str(dst.relative_to(root)), "run_id": rid}
            )
            result["moved"].append(str(dst.relative_to(root)))
        # 留档：声称归档了什么，就一定写下可回查的清单（tests 会钉）。
        (archive / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8"
        )

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=1))
        return 0

    if not picks:
        print(
            f"没有 {args.older_than} 天以上、且未被文档引用的证据产物可归档（过一遍用了 {len(protect)} 个被保护 id 对照）。"
        )
        return 0
    print(f"待归档 {len(picks)} 个证据产物（>{args.older_than} 天未动、未被 docs 点名）：")
    for rel in result["candidates"]:
        print(f"  {rel}")
    print(
        "\n（默认 dry-run，未移动）加 --commit 才实际移动；移动不改字节、不删文件，清单留档 archive/*/manifest.json。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
