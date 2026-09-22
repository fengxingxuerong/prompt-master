#!/usr/bin/env python3
"""ModelHub CLI：模型池连通性测试 / 台账查询 / 池状态 / 备份回滚。

用法：
    python modelhub_cli.py ping                  # 逐个模型真实调用，输出连通性矩阵
    python modelhub_cli.py status                # 断路器/冷却状态
    python modelhub_cli.py ledger [--model x] [--success 1|0] [--type call|switch] [--since ts] [--limit n]
    python modelhub_cli.py stats                 # 台账汇总
    python modelhub_cli.py chat "你的问题"       # 走主备链发一次真实对话
    python modelhub_cli.py backup               # 改动前备份（配置+密钥快照）
    python modelhub_cli.py rollback [备份目录]   # 一键回滚到最近/指定备份
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pm.bootstrap import ensure_utf8_stdio

ensure_utf8_stdio()

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent  # 脚本位于项目根目录
BACKUP_ROOT = ROOT / "backups"


def cmd_ping() -> int:
    from pm.modelhub.pool import ConfigError, GatewayError, ModelHub

    try:
        hub = ModelHub()
    except ConfigError as e:
        print(f"CONFIG_ERROR: {e}")
        return 2
    entries = hub._ordered_enabled()
    all_entries = hub.list_models()
    print(f"池内登记 {len(all_entries)} 个模型，本次参与调度 {len(entries)} 个（enabled 且不在冷却期）\n")
    ok_count = 0
    for e in all_entries:
        if not e["enabled"]:
            print(f"SKIP  {e['name']:<26} disabled（不参与调度）")
            continue
        hub._reload_if_needed()
        entry = next(x for x in hub._entries if x.name == e["name"])
        t0 = time.time()
        try:
            result = hub.chat(
                [{"role": "user", "content": "连通性测试：请回复“OK”两个字母。"}],
                model=e["name"],
                agent="modelhub-cli",
                role=None,
                max_tokens=4096,
            )
            ms = int((time.time() - t0) * 1000)
            served = result["model"]
            tag = "" if served == e["name"] else f"（请求模型不可用，自动切换到 {served}）"
            print(
                f"OK    {e['name']:<26} {ms:>6}ms  via={served:<24} failovers={result['failovers']}{tag}"
            )
            if served == e["name"]:
                ok_count += 1
        except GatewayError as err:
            ms = int((time.time() - t0) * 1000)
            print(f"FAIL  {e['name']:<26} {ms:>6}ms  {type(err).__name__}: {str(err)[:90]}")
        except Exception as err:  # noqa: BLE001
            ms = int((time.time() - t0) * 1000)
            print(f"FAIL  {e['name']:<26} {ms:>6}ms  {type(err).__name__}: {str(err)[:90]}")
        time.sleep(0.5)
    print(f"\n可用 {ok_count} 个。台账已写入 logs/modelhub_ledger.jsonl（python modelhub_cli.py stats 查看）")
    return 0 if ok_count > 0 else 1


def cmd_status() -> int:
    from pm.modelhub.pool import ModelHub

    print(json.dumps(ModelHub().status(), ensure_ascii=False, indent=2))
    return 0


def cmd_ledger(args: argparse.Namespace) -> int:
    from pm.modelhub.ledger import query_ledger

    succ: bool | None = None
    if args.success is not None:
        succ = args.success in ("1", "true", "True", "yes")
    rows = query_ledger(
        model=args.model,
        success=succ,
        type=args.type,
        since=args.since,
        until=args.until,
        limit=args.limit,
    )
    print(json.dumps(rows, ensure_ascii=False, indent=2))
    return 0


def cmd_stats() -> int:
    from pm.modelhub.ledger import ledger_stats

    print(json.dumps(ledger_stats(), ensure_ascii=False, indent=2))
    return 0


def cmd_chat(args: argparse.Namespace) -> int:
    from pm.modelhub.pool import ModelHub, ModelPoolExhaustedError

    try:
        result = ModelHub().chat(
            [{"role": "user", "content": args.prompt}],
            model=args.model,
            agent=args.agent or "modelhub-cli",
            role=args.role,
            max_tokens=args.max_tokens,
        )
    except ModelPoolExhaustedError as e:
        print(f"ALL_MODELS_FAILED: {e}")
        return 1
    print(f"[model={result['model']} failovers={result['failovers']} latency={result['latency_ms']}ms request_id={result['request_id']}]")
    print(result["content"])
    return 0


def cmd_backup() -> int:
    ts = time.strftime("%Y%m%d-%H%M%S")
    dest = BACKUP_ROOT / f"modelhub-{ts}"
    dest.mkdir(parents=True, exist_ok=True)
    copied = []
    for rel in ("config/modelhub.json", "config/roles.json", ".env"):
        src = ROOT / rel
        if src.exists():
            shutil.copy2(src, dest / src.name)
            copied.append(rel)
    (dest / "manifest.json").write_text(
        json.dumps(
            {"created": time.strftime("%Y-%m-%d %H:%M:%S"), "scope": "modelhub backup", "files": copied},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"BACKUP_OK {dest}（{len(copied)} 个文件）")
    return 0


def cmd_rollback(args: argparse.Namespace) -> int:
    if args.backup_dir:
        dest = Path(args.backup_dir)
    else:
        candidates = sorted(BACKUP_ROOT.glob("modelhub-*"))
        if not candidates:
            print("没有可用的 modelhub 备份（先跑 backup）")
            return 2
        dest = candidates[-1]
    if not dest.exists():
        print(f"备份目录不存在：{dest}")
        return 2
    mapping = {"modelhub.json": "config/modelhub.json", "roles.json": "config/roles.json", ".env": ".env"}
    restored = []
    for fname, rel in mapping.items():
        src = dest / fname
        if src.exists():
            target = ROOT / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, target)
            restored.append(rel)
    print(f"ROLLBACK_OK 从 {dest} 恢复：{', '.join(restored)}")
    print("网关如正在运行会自动感知配置变更（mtime 热重载）；角色文件同样自动生效。")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description="ModelHub CLI")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("ping", help="逐个模型连通性测试")
    sub.add_parser("status", help="池状态/断路器")
    sub.add_parser("stats", help="台账汇总")

    p_ledger = sub.add_parser("ledger", help="台账检索")
    p_ledger.add_argument("--model")
    p_ledger.add_argument("--success", help="1=成功 0=失败")
    p_ledger.add_argument("--type", choices=["call", "switch"])
    p_ledger.add_argument("--since")
    p_ledger.add_argument("--until")
    p_ledger.add_argument("--limit", type=int, default=200)

    p_chat = sub.add_parser("chat", help="走主备链发一次对话")
    p_chat.add_argument("prompt")
    p_chat.add_argument("--model")
    p_chat.add_argument("--agent")
    p_chat.add_argument("--role")
    p_chat.add_argument("--max-tokens", type=int, default=4096, dest="max_tokens")

    sub.add_parser("backup", help="备份配置+密钥")
    p_rb = sub.add_parser("rollback", help="回滚到备份")
    p_rb.add_argument("backup_dir", nargs="?", default=None)

    args = ap.parse_args()
    rc = {
        "ping": cmd_ping,
        "status": cmd_status,
        "stats": cmd_stats,
        "ledger": lambda: cmd_ledger(args),
        "chat": lambda: cmd_chat(args),
        "backup": cmd_backup,
        "rollback": lambda: cmd_rollback(args),
    }[args.cmd]()
    sys.exit(rc)


if __name__ == "__main__":
    main()
