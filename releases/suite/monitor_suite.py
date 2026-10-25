"""套件巡检监控：三服务健康 + 数据接口可达性 + 计数留痕，异常写告警日志。

用法：
  python monitor_suite.py            # 单次巡检，写 releases/suite/monitor_log.jsonl
  python monitor_suite.py --watch    # 每 60s 巡检一次（长期监控模式）

告警口径（2026-10-31 修，此前只有"health 非 200"这一条会告警）：
- `health.status != ok`，或 `health.storage` 以 `error` 开头 → **高**（存储坏了但进程还活着，
  旧版这一格完全不看，实测把 DB 写成垃圾字节仍报全绿）；
- 数据接口非 200（含 401：探针自己不带令牌）→ **高**；
- 数据接口读不到计数（None）→ **高**（旧版记 null 仍然全绿）。
计数基线（models/orders/tasks 各多少）**只记录不比较**——它随业务变化而变，
拿固定数字当"漂移"会天天误报；旧版本文件头写着会检测口径漂移，代码里从来没有，
这句已经在这次的修法里删掉了。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[2]
LOG = Path(__file__).resolve().parent / "monitor_log.jsonl"

SERVICES = {
    "modelhub": "http://127.0.0.1:8687/api/health",
    "lobster": "http://127.0.0.1:8791/api/health",
    "taskboard": "http://127.0.0.1:8792/api/health",
    "triage": "http://127.0.0.1:8793/api/health",
}
DATA_APIS = {
    "modelhub": ("http://127.0.0.1:8687/v1/models", lambda d: len(d.get("data", []))),
    "lobster": ("http://127.0.0.1:8791/api/orders", lambda d: d.get("count", 0)),
    "taskboard": ("http://127.0.0.1:8792/api/tasks", lambda d: d.get("count", 0)),
    "triage": ("http://127.0.0.1:8793/api/sessions", lambda d: d.get("count", 0)),
}

# 每个服务认哪个环境变量里的令牌（与各 app 读的是同一个名字）
TOKEN_ENV = {
    "modelhub": "PMH_GATEWAY_TOKEN",
    "lobster": "LOBSTER_TOKEN",
    "taskboard": "TASKBOARD_TOKEN",
    "triage": "TRIAGE_TOKEN",
}


def headers_for(name: str) -> dict[str, str]:
    """数据接口的请求头：配了令牌就带上（2026-10-31 修）。

    旧版探针从不带令牌，于是"带 token 的部署"里三个数据接口恒 401 ——
    而 401 又被记成 status=401 + count=None，**巡检仍然全绿**。
    """
    token = (os.getenv(TOKEN_ENV.get(name, ""), "") or "").strip()
    return {"X-API-Key": token} if token else {}


def probe(url: str, name: str = "") -> tuple[int | None, dict]:
    req = urllib.request.Request(url, headers=headers_for(name))
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, {}
    except Exception as e:  # noqa: BLE001 - 探针永不抛，把原因带回给调用方
        return None, {"err": str(e)[:80]}


def _health_issue(status: int | None, body: dict) -> str | None:
    """健康判据：不只是 HTTP 200，还要看请求体自报的状态与存储。

    实测缺陷（2026-10-31）：把 `orders.db` / `sessions.db` 写成垃圾字节，
    服务仍回 200 + `status:"ok"`（只把 `storage` 记成 `error: DatabaseError`），
    于是监控与看门狗双双报绿 —— "进程活着"被当成了"服务可用"。
    """
    if status != 200:
        return f"health 异常: HTTP {status}"
    if str(body.get("status") or "") != "ok":
        return f"health.status={body.get('status')!r}"
    storage = str(body.get("storage") or "")
    if storage.startswith("error"):
        return f"存储不可用（storage={storage}）"
    return None


def inspect() -> dict:
    alerts: list[dict] = []
    services: dict[str, dict] = {}
    for name, url in SERVICES.items():
        st, d = probe(url, name)
        issue = _health_issue(st, d)
        services[name] = {"health": "ok" if issue is None else "bad", "ms": None}
        if issue:
            alerts.append({"service": name, "severity": "高", "issue": issue})
    for name, (url, counter) in DATA_APIS.items():
        st, d = probe(url, name)
        count = counter(d) if st == 200 else None
        services[name]["data_api"] = {"status": st, "count": count}
        if st != 200:
            alerts.append(
                {
                    "service": name,
                    "severity": "高",
                    "issue": f"数据接口 HTTP {st}"
                    + ("（探针未带令牌？见 " + TOKEN_ENV.get(name, "?") + "）" if st == 401 else ""),
                }
            )
        elif count is None:
            alerts.append({"service": name, "severity": "高", "issue": "数据接口读不到计数"})
    return {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "services": services,
        "alerts": alerts,
        "alert": bool(alerts),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--watch", action="store_true")
    ap.add_argument("--interval", type=int, default=60)
    args = ap.parse_args()
    LOG.parent.mkdir(parents=True, exist_ok=True)
    while True:
        rec = inspect()
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        tag = "ALERT" if rec["alert"] else "OK"
        print(time.strftime("%H:%M:%S"), tag,
              json.dumps({s: v["health"] for s, v in rec["services"].items()}, ensure_ascii=False))
        for a in rec["alerts"]:
            print(f"  ⚠️ {a['service']}: {a['issue']}")
        if not args.watch:
            break
        time.sleep(args.interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
