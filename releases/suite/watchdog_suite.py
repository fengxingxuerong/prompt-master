"""套件看门狗：守护三服务进程，崩溃/缺失时自动拉起。

用法：
  python watchdog_suite.py              # 前台守护（Ctrl+C 停止看门狗，不影响已拉起服务）
  python watchdog_suite.py --once       # 单次巡检+拉起（手动触发）
  python watchdog_suite.py --uninstall  # 清理历史计划任务（若残留）

注意（2026-10-01 变更）：--install 已停用。本项目不再注册任何计划任务，
不使用本项目时不会有任何服务被后台自动拉起；只有上述手动命令（或 --once）
才会触发巡检与拉起。

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
import urllib.error
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


# 各服务认哪个环境变量里的令牌（与 app 读的同一个名字）
TOKEN_ENV = {
    "lobster": "LOBSTER_TOKEN",
    "taskboard": "TASKBOARD_TOKEN",
    "triage": "TRIAGE_TOKEN",
}


def _headers_for(service: str) -> dict[str, str]:
    """数据接口的请求头（2026-10-31 修）：配了令牌就带上。

    旧版探针从不带令牌，于是带 token 的部署里 `/api/tasks` 恒 401 ⇒
    "超时任务告警"这条路径静默不触发，巡检日志看不出任何异常。
    """
    token = (_ENV_FILE.get(TOKEN_ENV.get(service, "")) or os.getenv(TOKEN_ENV.get(service, ""), "")).strip()
    return {"X-API-Key": token} if token else {}


def health_issue(url: str, service: str = "") -> str | None:
    """健康判据：HTTP 200 **且** 请求体自报 ok **且** 存储没坏。

    实测缺陷（2026-10-31）：把 `orders.db` / `sessions.db` 写成垃圾字节，服务仍回
    200 + `status:"ok"`，只把 `storage` 记成 `error: DatabaseError` —— 看门狗只看
    200，于是"进程活着"被当成"服务可用"，永远不会告警。
    """
    try:
        req = urllib.request.Request(url, headers=_headers_for(service))
        with urllib.request.urlopen(req, timeout=8) as r:
            if r.status != 200:
                return f"HTTP {r.status}"
            body = json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        return f"HTTP {e.code}"
    except Exception as e:  # noqa: BLE001 - 探针永不抛
        return f"不可达：{type(e).__name__}"
    if str(body.get("status") or "") != "ok":
        return f"status={body.get('status')!r}"
    storage = str(body.get("storage") or "")
    if storage.startswith("error"):
        return f"存储不可用（{storage}）"
    return None

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
        req = _u.Request("http://127.0.0.1:8792/api/tasks?limit=200", headers=_headers_for("taskboard"))
        with _u.urlopen(req, timeout=5) as r:
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
    """G-05：巡检时顺带做台账轮转检查（超 1MB 轮转，保留 3 代）。

    2026-10-31 修：轮转脚本现在返回结构化报告，**失败必须告警**。
    旧版把异常 print 一行就算了，而 Windows 上服务正开着台账文件时 `replace` 必抛
    PermissionError ⇒ 台账实际上从未轮转过，巡检日志里却看不出异常。
    """
    try:
        rot = ROOT / "suite" / "rotate_ledger.py"
        ns: dict = {}
        exec(compile(rot.read_text(encoding="utf-8"), str(rot), "exec"), ns)
        report = ns["rotate"]()
        errors = report.get("errors") or []
        if errors:
            msg = f"台账轮转失败 {len(errors)} 份（文件被占用？）：{'; '.join(errors[:3])}"
            log({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "service": "rotate",
                 "action": "rotate_error", "detail": errors, "alert": True})
            _notify_center("watchdog", msg)
    except Exception as exc:  # noqa: BLE001 - 巡检不能因为轮转坏掉就整体崩
        print(f"[watchdog] rotate check failed: {exc}")
        _notify_center("watchdog", f"台账轮转脚本自身跑不起来：{type(exc).__name__}: {exc}")

def patrol() -> list[str]:
    actions = []
    ts = time.strftime("%Y-%m-%dT%H:%M:%S")
    child_env = {**_ENV_FILE, **os.environ}  # 系统 > .env（M3.3：TOKEN 等配置透传给服务）
    for svc in SERVICES:
        issue = health_issue(svc["url"], svc["name"])
        if issue is None:
            continue
        if not alive(svc["url"]):
            # 完全不可达 → 拉起（这是看门狗的本职）
            subprocess.Popen(svc["cmd"], cwd=svc["cwd"], env=child_env,
                             creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            actions.append(f"{svc['name']}: DOWN → 拉起 (port from {svc['url']})")
            log({"ts": ts, "service": svc["name"], "action": "restart", "alert": True})
            notify("watchdog", f"服务 {svc['name']} 掉线，已自动拉起")
            continue
        # 进程活着但自报不健康（典型：存储坏了）→ **只告警不重启**：
        # 重启修不好一个坏掉的库，只会把现场重启掉并陷入拉起循环。
        actions.append(f"{svc['name']}: 活着但不健康（{issue}）→ 仅告警")
        log({"ts": ts, "service": svc["name"], "action": "unhealthy", "detail": issue, "alert": True})
        _notify_center("watchdog", f"服务 {svc['name']} 活着但不健康：{issue}（未重启：重启修不好存储）")
    if actions:
        log({"ts": ts, "action": "patrol", "detail": actions, "alert": bool(actions)})
    _rotate_ledgers()  # G-05：巡检顺带做台账轮转检查
    _check_stale_tasks()  # M2.2：pending/待用户超 24h 任务落提醒
    return actions

def install() -> None:
    """已停用（2026-10-01，用户要求）：不再注册任何计划任务。

    背景：原实现会注册 SuiteWatchdog_OnStart（开机）与 SuiteWatchdog_5min（每 5 分钟）
    两个计划任务，导致未打开本项目时服务仍会被后台反复拉起。用户明确要求改为
    「只在本人主动做这个项目时才启动」，故保留函数签名但直接拒绝执行，
    避免任何脚本/别名/旧文档误触发重新注册。

    需要巡检时请前台显式运行：--once（单次）或默认模式（前台守护）。
    """
    print("REFUSED: 自动注册已停用，不会创建计划任务。\n"
          "  如需巡检请前台手动运行：--once（单次）或直接运行（前台守护）。\n"
          "  如需清理历史任务请运行：--uninstall")
    log({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "action": "install",
         "detail": "refused: auto-registration disabled", "alert": False})

def uninstall() -> None:
    for tn in ("SuiteWatchdog_OnStart", "SuiteWatchdog_5min"):
        subprocess.run(["schtasks", "/Delete", "/TN", tn, "/F"], capture_output=True, text=True)
    print("UNINSTALLED")
    log({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "action": "uninstall", "alert": False})

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--watch", action="store_true")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--install", action="store_true",
                    help="已停用：不会注册计划任务（防止后台自动拉起）")
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
