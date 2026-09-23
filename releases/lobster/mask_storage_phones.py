"""一次性迁移：orders.json 存量明文手机号 → 掩码（M3.2 存储脱敏，2026-09-23）。

幂等：已掩码/非 11 位的记录跳过；无明文时零改动退出（--verify-only 只报告不写）。
安全：改写前把明文原件备份到 logs/（gitignored，不入库）并做 JSON 校验，
      落盘走 tmp + os.replace 原子写；写后复检确认零明文残留。
口径：只处理 phone 字段（note 等自由文本可能含客户自填号码，属脱敏范围外，另行治理）。

用法：
    python releases/lobster/mask_storage_phones.py               # 备份+迁移+复检
    python releases/lobster/mask_storage_phones.py --verify-only  # 只报告不改动
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ORDERS = ROOT / "data" / "orders.json"
REPO_ROOT = ROOT.parents[1]
FULL_PHONE = re.compile(r"^1[3-9]\d{9}$")

sys.path.insert(0, str(ROOT))
from app import mask_phone  # noqa: E402  单一事实来源：与 app.py 存储层同口径


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verify-only", action="store_true", help="只报告不改动")
    args = ap.parse_args()

    if not ORDERS.exists():
        print(f"[skip] {ORDERS} 不存在")
        return 0
    raw = ORDERS.read_text(encoding="utf-8")
    data = json.loads(raw)  # 坏 JSON 直接异常退出，不碰文件
    orders = data.get("orders", [])
    full_idx = [i for i, o in enumerate(orders)
                if FULL_PHONE.match(str(o.get("phone", "")))]
    print(f"订单总数 {len(orders)}，其中 11 位明文手机号 {len(full_idx)} 条")

    if args.verify_only:
        return 0
    if not full_idx:
        print("[done] 无明文残留，无需迁移（幂等重跑）")
        return 0

    # 1) 备份明文原件到 logs/（gitignored）并校验
    ts = time.strftime("%Y%m%d-%H%M%S")
    backup = REPO_ROOT / "logs" / f"lobster_orders_plaintext_backup_{ts}.json"
    backup.parent.mkdir(parents=True, exist_ok=True)
    backup.write_text(raw, encoding="utf-8")
    json.loads(backup.read_text(encoding="utf-8"))  # 备份可解析才继续
    print(f"[backup] 明文原件 → {backup}")

    # 2) 原子写掩码版
    for i in full_idx:
        orders[i]["phone"] = mask_phone(orders[i]["phone"])
    tmp = ORDERS.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, ORDERS)
    print(f"[migrate] 已掩码 {len(full_idx)} 条，原子写完成")

    # 3) 写后复检：零明文残留
    recheck = json.loads(ORDERS.read_text(encoding="utf-8")).get("orders", [])
    leftover = sum(1 for o in recheck if FULL_PHONE.match(str(o.get("phone", ""))))
    if leftover:
        print(f"[FAIL] 复检发现 {leftover} 条明文残留！请用备份恢复后排查")
        return 1
    print(f"[verify] 复检 PASS：{len(recheck)} 条订单，明文残留 0")
    return 0


if __name__ == "__main__":
    sys.exit(main())
