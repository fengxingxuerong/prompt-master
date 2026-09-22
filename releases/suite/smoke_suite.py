"""套件级冒烟测试：三服务统一口径（基线/复测共用同一脚本，保证数据口径一致）。"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
import json, time, urllib.request, urllib.error

SERVICES = {
    "modelhub": {"base": "http://127.0.0.1:8687", "pages": [("/", "json"), ("/v1/models", "json"), ("/console", "html")],
                 "api": ("/v1/models", lambda d: len(d.get("data", [])), "models_count")},
    "lobster": {"base": "http://127.0.0.1:8791", "pages": [("/", "html"), ("/ledger", "html"), ("/poster", "html")],
                "api": ("/api/orders", lambda d: d.get("count", 0), "orders_count")},
    "taskboard": {"base": "http://127.0.0.1:8792", "pages": [("/", "html"), ("/rules", "md"), ("/exceptions", "md"), ("/handoff", "md")],
                  "api": ("/api/tasks", lambda d: d.get("count", 0), "tasks_count")},
}

def timed_get(url, timeout=20):
    t0 = time.time()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            body = r.read()
            return r.status, body, int((time.time() - t0) * 1000)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), int((time.time() - t0) * 1000)
    except Exception as e:
        return None, str(e).encode(), int((time.time() - t0) * 1000)

def run_suite(label):
    out = {"label": label, "ts": time.strftime("%Y-%m-%d %H:%M:%S"), "services": {}}
    for name, cfg in SERVICES.items():
        svc = {"health": None, "pages": {}, "api": {}}
        st, body, ms = timed_get(cfg["base"] + "/api/health")
        try:
            d = json.loads(body.decode("utf-8"))
            svc["health"] = {"status": st, "ok": d.get("status") == "ok", "ms": ms}
        except Exception:
            svc["health"] = {"status": st, "ok": False, "ms": ms}
        for path, kind in cfg["pages"]:
            st, body, ms = timed_get(cfg["base"] + path)
            svc["pages"][path] = {"status": st, "size": len(body), "ms": ms}
        path, counter, key = cfg["api"]
        st, body, ms = timed_get(cfg["base"] + path)
        try:
            svc["api"] = {"status": st, key: counter(json.loads(body.decode("utf-8"))), "ms": ms}
        except Exception:
            svc["api"] = {"status": st, key: None, "ms": ms}
        out["services"][name] = svc
    # 汇总
    ok_pages = sum(1 for s in out["services"].values() for p in s["pages"].values() if p["status"] == 200)
    total_pages = sum(len(s["pages"]) for s in out["services"].values())
    ok_health = sum(1 for s in out["services"].values() if s["health"]["ok"])
    out["summary"] = {"health_ok": f"{ok_health}/3", "pages_ok": f"{ok_pages}/{total_pages}"}
    return out

if __name__ == "__main__":
    label = sys.argv[1] if len(sys.argv) > 1 else "run"
    result = run_suite(label)
    out_path = r"D:\projects\prompt-master\releases\suite\smoke_" + label + ".json"
    import os
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
    print("SUITE", label, "| health", result["summary"]["health_ok"], "| pages", result["summary"]["pages_ok"])
    for name, svc in result["services"].items():
        print(f"  {name}: health={svc['health']['ok']}({svc['health']['ms']}ms) api={svc['api']}")
        for p, v in svc["pages"].items():
            print(f"    {p} {v['status']} {v['ms']}ms {v['size']}B")
