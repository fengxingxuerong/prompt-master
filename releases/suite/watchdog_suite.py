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
import argparse
import json
import os
import subprocess
import time
import urllib.request
from pathlib import Path

ROOT = Path(r"D:\projects\prompt-master\releases")
PY = r"D:\projects\prompt-master\.venv\Scripts\python.exe"
LOG = ROOT / "suite" / "watchdog_log.jsonl"


def _load_dotenv_env() -> dict:
    """M3.3（2026-09-23）：加载仓库根 .env，供拉起的服务继承鉴权 TOKEN 等配置。

    背景：三服务（lobster/taskboard/triage）自身不 load_dotenv，而看门狗经
    schtasks 拉起时只有极简系统环境——没有这一步，.env 里写的
    TRIAGE_TOKEN/LOBSTER_TOKEN/TASKBOARD_TOKEN 永远到不了服务进程。
    规则：只取简单 KEY=VALUE 行；剥离一层成对引号；系统已有环境变量优先
    （setdefault 语义），即 shell 显式 export > .env 文件。
    """
    env_path = ROOT.parent / ".env"
    out: dict = {}
    try:
        for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k = k.strip()
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
                v = v[1:-1]
            if k:
                out.setdefault(k, v)
    except OSError:
        pass
    return out


# 合并口径：系统环境 > .env 文件
_ENV_FILE = _load_dotenv_env()

SERVICES = [
    {"name": "modelhub", "url": "http://127.0.0.1:8687/api/health", "cmd": [PY, str(ROOT / ".." / "run_modelhub.py"), "--port", "8687"], "cwd": str(ROOT / "..")},
    {"name": "lobster", "url": "http://127.0.0.1:8791/api/health", "cmd": [PY, str(ROOT / "lobster" / "app.py"), "--port", "8791"], "cwd": str(ROOT / "lobster")},
    {"name": "taskboard", "url": "http://127.0.0.1:8792/api/health", "cmd": [PY, str(ROOT / "taskboard" / "app.py"), "--port", "8792"], "cwd": str(ROOT / "taskboard")},
    {"name": "triage", "url": "http://127.0.0.1:8793/api/health", "cmd": [PY, str(ROOT / "triage" / "app.py"), "--port", "8793"], "cwd": str(ROOT / "triage")},
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

def notify(source: str, message: str) -> None:
    """G-06：看门狗告警统一落盘 logs/notifications.jsonl（与 usage_store 共用）。"""
    try:
        nlog = ROOT.parent / "logs" / "notifications.jsonl"
        nlog.parent.mkdir(parents=True, exist_ok=True)
        import json as _json
        with open(nlog, "a", encoding="utf-8") as f:
            f.write(_json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                 "source": source, "message": message},
                                ensure_ascii=False) + "\n")
    except Exception:
        pass
def _notify_center(source: str, message: str) -> None:
    """M2.2 修正（M3.3 顺带）：超时任务告警优先写会审台通知中心（/api/notifications 可见）。

    原实现误用了本地 notify() 不支持的 level/base_dir 参数，TypeError 被
    except 静默吞掉——告警从未真正落盘。修复后失败仍回退 G-06 本地日志。
    """
    try:
        ns: dict = {}
        nc = ROOT / "triage" / "notify_center.py"
        exec(compile(nc.read_text(encoding="utf-8"), str(nc), "exec"), ns)
        ns["notify"](source, message, level="warn", base_dir=ROOT / "triage" / "data")
    except Exception as exc:
        print(f"[watchdog] notify_center fallback: {exc}")
        notify(source, message)


def _check_stale_tasks() -> None:
    """M2.2：任务台账 pending/处理中超 24h → 落提醒通知（每次巡检最多提醒一次/任务）。"""
    try:
        import urllib.request as _u
        with _u.urlopen("http://127.0.0.1:8792/api/tasks?limit=200", timeout=5) as r:
            data = json.loads(r.read().decode("utf-8"))
        now = time.time()
        marker = ROOT / "suite" / ".stale_notified.json"
        seen = {}
        if marker.exists():
            try:
                seen = json.loads(marker.read_text(encoding="utf-8"))
            except Exception:
                seen = {}
        changed = False
        for t in data.get("tasks", []):
            st = (t.get("status") or "").lower()
            if st not in ("pending", "in_progress"):
                continue
            tid = t.get("task_id", "")
            due = t.get("due") or ""
            try:
                overdue_h = (now - time.mktime(time.strptime(due, "%Y-%m-%d"))) / 3600.0
            except Exception:
                continue
            if overdue_h >= 24 and seen.get(tid) != due:
                _notify_center("watchdog", f"任务 {tid} 已到期超 24h 未处理（due={due}）：{t.get('title', '')[:40]}")
                seen[tid] = due
                changed = True
        if changed:
            marker.write_text(json.dumps(seen, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as e:
        print(f"[watchdog] stale-task check failed: {e}")

def _rotate_ledgers() -> None:
    """G-05：巡检时顺带做台账轮转检查（超 1MB 轮转，保留 3 代）。"""
    try:
        rot = ROOT / "suite" / "rotate_ledger.py"
        ns: dict = {}
        exec(compile(rot.read_text(encoding="utf-8"), str(rot), "exec"), ns)
        ns["rotate"]()
    except Exception as exc:
        print(f"[watchdog] rotate check failed: {exc}")

def patrol() -> list[str]:
    actions = []
    ts = time.strftime("%Y-%m-%dT%H:%M:%S")
    child_env = {**_ENV_FILE, **os.environ}  # 系统 > .env（M3.3：TOKEN 等配置透传给服务）
    for svc in SERVICES:
        if alive(svc["url"]):
            continue
        subprocess.Popen(svc["cmd"], cwd=svc["cwd"], env=child_env,
                         creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        actions.append(f"{svc['name']}: DOWN → 拉起 (port from {svc['url']})")
        log({"ts": ts, "service": svc["name"], "action": "restart", "alert": True})
        notify("watchdog", f"服务 {svc['name']} 掉线，已自动拉起")
    if actions:
        log({"ts": ts, "action": "patrol", "detail": actions, "alert": bool(actions)})
    _rotate_ledgers()  # G-05：巡检顺带做台账轮转检查
    _check_stale_tasks()  # M2.2：pending/待用户超 24h 任务落提醒
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
