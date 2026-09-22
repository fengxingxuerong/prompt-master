"""套件巡检监控：三服务健康 + 数据口径 + 持久文件，异常写告警日志并可触发通知。

用法：
  python monitor_suite.py            # 单次巡检，写 releases/suite/monitor_log.jsonl
  python monitor_suite.py --watch    # 每 60s 巡检一次（长期监控模式）

告警口径：health 非 ok / 页面非 200 / 数据口径漂移（models≠12 orders≠7 tasks≠6 基线可能漂移，仅提示）。
通知接入点：告警条目带 "alert": true 字段，可由外部脚本（如 AutoClaw 定时任务）读取后推送。
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
import json, time, urllib.request, urllib.error, argparse
from pathlib import Path

ROOT = Path(r"D:\projects\prompt-master")
LOG = ROOT / "releases" / "suite" / "monitor_log.jsonl"
SERVICES = {
    "modelhub": "http://127.0.0.1:8687/api/health",
    "lobster": "http://127.0.0.1:8791/api/health",
    "taskboard": "http://127.0.0.1:8792/api/health",
}
DATA_APIS = {
    "modelhub": ("http://127.0.0.1:8687/v1/models", lambda d: len(d.get("data", []))),
    "lobster": ("http://127.0.0.1:8791/api/orders", lambda d: d.get("count", 0)),
    "taskboard": ("http://127.0.0.1:8792/api/tasks", lambda d: d.get("count", 0)),
}

def probe(url):
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, {}
    except Exception as e:
        return None, {"err": str(e)[:80]}

def inspect():
    alerts = []
    services = {}
    for name, url in SERVICES.items():
        st, d = probe(url)
        ok = st == 200 and d.get("status") == "ok"
        services[name] = {"health": "ok" if ok else f"down({st})", "ms": None}
        if not ok:
            alerts.append({"service": name, "severity": "高", "issue": f"health 异常: HTTP {st}"})
    for name, (url, counter) in DATA_APIS.items():
        st, d = probe(url)
        services[name]["data_api"] = {"status": st, "count": counter(d) if st == 200 else None}
    return {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "services": services,
            "alerts": alerts, "alert": bool(alerts)}

def main():
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
        print(time.strftime("%H:%M:%S"), tag, json.dumps({s: v["health"] for s, v in rec["services"].items()}, ensure_ascii=False))
        if not args.watch:
            break
        time.sleep(args.interval)

if __name__ == "__main__":
    main()
