"""套件看门狗：守护三服务进程，崩溃/缺失时自动拉起。

用法：
  python watchdog_suite.py              # 前台守护（Ctrl+C 停止看门狗，不影响已拉起服务）
  python watchdog_suite.py --once       # 单次巡检+拉起（供计划任务调用）
  python watchdog_suite.py --install    # 注册 Windows 计划任务（开机自启 + 每 5 分钟巡检）
  python watchdog_suite.py --uninstall  # 移除计划任务

守护逻辑：每 30s 探测三端口；DOWN 则以独立进程拉起对应 app（detach，不随看门狗退出）。
告警写 releases/suite/watchdog_log.jsonl（含拉起动作留痕）。
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
import json, time, subprocess, urllib.request, argparse
from pathlib import Path

ROOT = Path(r"D:\projects\prompt-master\releases")
PY = r"D:\projects\prompt-master\.venv\Scripts\python.exe"
LOG = ROOT / "suite" / "watchdog_log.jsonl"
SERVICES = [
    {"name": "modelhub", "url": "http://127.0.0.1:8687/api/health", "cmd": [PY, str(ROOT / ".." / "run_modelhub.py"), "--port", "8687"], "cwd": str(ROOT / "..")},
    {"name": "lobster", "url": "http://127.0.0.1:8791/api/health", "cmd": [PY, str(ROOT / "lobster" / "app.py"), "--port", "8791"], "cwd": str(ROOT / "lobster")},
    {"name": "taskboard", "url": "http://127.0.0.1:8792/api/health", "cmd": [PY, str(ROOT / "taskboard" / "app.py"), "--port", "8792"], "cwd": str(ROOT / "taskboard")},
]

def alive(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=8) as r:
            return r.status == 200
    except Exception:
        return False

def log(rec: dict) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")

def patrol() -> list[str]:
    actions = []
    ts = time.strftime("%Y-%m-%dT%H:%M:%S")
    for svc in SERVICES:
        if alive(svc["url"]):
            continue
        subprocess.Popen(svc["cmd"], cwd=svc["cwd"],
                         creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        actions.append(f"{svc['name']}: DOWN → 拉起 (port from {svc['url']})")
        log({"ts": ts, "service": svc["name"], "action": "restart", "alert": True})
    if actions:
        log({"ts": ts, "action": "patrol", "detail": actions, "alert": bool(actions)})
    return actions

def install() -> None:
    self_exe = PY
    self_script = str(Path(__file__).resolve())
    tasks = []
    sch = subprocess.run(["schtasks", "/Query", "/TN", "SuiteWatchdog_OnStart"], capture_output=True, text=True)
    if "SuiteWatchdog_OnStart" not in (sch.stdout or ""):
        subprocess.run(["schtasks", "/Create", "/TN", "SuiteWatchdog_OnStart", "/SC", "ONSTART", "/DELAY", "0001:00",
                        "/TR", f'"{self_exe}" "{self_script}" --once', "/F", "/RL", "LIMITED"], check=True)
        tasks.append("onstart")
    sch2 = subprocess.run(["schtasks", "/Query", "/TN", "SuiteWatchdog_5min"], capture_output=True, text=True)
    if "SuiteWatchdog_5min" not in (sch2.stdout or ""):
        subprocess.run(["schtasks", "/Create", "/TN", "SuiteWatchdog_5min", "/SC", "MINUTE", "/MO", "5",
                        "/TR", f'"{self_exe}" "{self_script}" --once', "/F", "/RL", "LIMITED"], check=True)
        tasks.append("5min")
    print("INSTALLED:", tasks or ["already present"])
    log({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "action": "install", "detail": tasks, "alert": False})

def uninstall() -> None:
    for tn in ("SuiteWatchdog_OnStart", "SuiteWatchdog_5min"):
        subprocess.run(["schtasks", "/Delete", "/TN", tn, "/F"], capture_output=True, text=True)
    print("UNINSTALLED")
    log({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "action": "uninstall", "alert": False})

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--watch", action="store_true")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--install", action="store_true")
    ap.add_argument("--uninstall", action="store_true")
    a = ap.parse_args()
    if a.install:
        install()
    elif a.uninstall:
        uninstall()
    elif a.once:
        acts = patrol()
        print("PATROL:", acts or ["all alive"])
    else:
        print("watchdog running (Ctrl+C 停止看门狗本身)")
        while True:
            acts = patrol()
            print(time.strftime("%H:%M:%S"), acts or "all alive")
            time.sleep(30)
