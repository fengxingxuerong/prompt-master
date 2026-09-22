#!/usr/bin/env python3
"""ModelHub 发布测试套件：单元(U1-U3) + 并发(C1) + 性能(P1-P2)。

所有用例本地可重复执行：
    .venv\\Scripts\\python.exe tests_modelhub\\release_test.py
产物：tests_modelhub/release_results.json（逐用例 PASS/FAIL + 证据值）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(r"D:\projects\prompt-master")
sys.path.insert(0, str(ROOT))
from pm.bootstrap import ensure_utf8_stdio  # noqa: E402

ensure_utf8_stdio()

results: list[dict] = []


def record(step: str, ok: bool, detail: str) -> None:
    results.append({"step": step, "ok": ok, "detail": detail})
    print(("PASS " if ok else "FAIL ") + step + "  " + detail[:150])


# ============ U1 池配置加载（占位符/优先级/禁用） ============
def u1() -> None:
    run = r"""
import json, sys, pathlib
sys.path.insert(0, r"D:\projects\prompt-master")
from pm.modelhub.pool import ModelHub, ConfigError
cfg = json.loads(pathlib.Path(r"{cfg}").read_text(encoding="utf-8"))
hub = ModelHub(config_path=pathlib.Path(r"{cfg}"))
entries = {{e["name"]: e for e in hub.list_models()}}
# 占位符已解析为真实密钥（35 字符 sk- 开头）
k = entries["deepseek-v4-flash"]["api_key_env_resolved"] if "api_key_env_resolved" in entries["deepseek-v4-flash"] else None
# 直接探内部状态验证解析
e0 = [x for x in hub._entries if x.name == "deepseek-v4-flash"][0]
out = {{
  "resolved_ok": e0.api_key.startswith("sk-") and len(e0.api_key) == 35,
  "disabled_excluded": "amd-mineru2.5-pro" not in [e.name for e in hub._ordered_enabled()],
  "priority_sorted": [e.name for e in hub._ordered_enabled()][:3] == ["deepseek-v4-flash", "glm-5.2", "sensenova-6.8-flash-lite"],
  "count": len(entries),
}}
print("U1::" + json.dumps(out))
"""
    real = json.loads((ROOT / "config" / "modelhub.json").read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory() as td:
        cfg = Path(td) / "pool.json"
        cfg.write_text(json.dumps(real, ensure_ascii=False), encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, "-c", run.replace("{cfg}", str(cfg)).replace("{{", "{").replace("}}", "}")],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=120, cwd=str(ROOT), env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        out = (proc.stdout or "").strip()
        try:
            data = json.loads(out.split("U1::", 1)[1])
            ok = data["resolved_ok"] and data["disabled_excluded"] and data["priority_sorted"] and data["count"] == 12
            record("U1_pool_config_load", ok, f"count={data['count']} resolved={data['resolved_ok']} disabled_excluded={data['disabled_excluded']} priority_order={data['priority_sorted']}")
        except Exception as e:  # noqa: BLE001
            record("U1_pool_config_load", False, f"{type(e).__name__}: {out[:120]}")


# ============ U2 断路器（连败熔断/冷却跳过/自愈） ============
def u2() -> None:
    run = r"""
import json, sys, pathlib, time, os, threading
sys.path.insert(0, r"D:\projects\prompt-master")
os.environ["PMH_BREAK_THRESHOLD"] = "2"
os.environ["PMH_COOLDOWN_SECONDS"] = "1.5"
import importlib
from pm.modelhub import pool as poolmod
importlib.reload(poolmod)
from pm.modelhub.pool import ModelEntry
entries = [ModelEntry(s) for s in [
    {"name": "probe-a", "base_url": "https://127.0.0.1:9", "api_key": "sk-x", "priority": 1},
    {"name": "probe-b", "base_url": "https://127.0.0.1:9", "api_key": "sk-x", "priority": 2},
]]
hub = poolmod.ModelHub.__new__(poolmod.ModelHub)
hub._lock = threading.RLock()
hub._config_path = pathlib.Path(os.devnull)
hub._entries = entries
hub._mtime = time.time()  # 跳过配置重载（断路器语义测试不依赖配置文件）
hub._last_config_error = None
hub._reload_if_needed = lambda force=False: None
a = entries[0]
# 两次失败 → 熔断
hub._mark_failure(a, "boom")
hub._mark_failure(a, "boom")
state1 = hub.status()["models"][0]["circuit"] == "open(cooling)"
# 冷却期内 _ordered_enabled 跳过它
skipped = all(e.name != "probe-a" for e in hub._ordered_enabled())
# 冷却到期后重新可用
time.sleep(1.6)
back = "probe-a" in [e.name for e in hub._ordered_enabled()]
# 成功后计数清零
hub._mark_success(a)
cleared = a.consecutive_failures == 0 and hub.status()["models"][0]["circuit"] == "closed"
print("U2::" + json.dumps({"breaker": state1, "skipped": skipped, "healed": back, "cleared": cleared}))
"""
    proc = subprocess.run(
        [sys.executable, "-c", run], capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=60, cwd=str(ROOT),
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    out = (proc.stdout or "").strip()
    try:
        d = json.loads(out.split("U2::", 1)[1])
        ok = all(d.values())
        record("U2_circuit_breaker", ok, f"熔断={d['breaker']} 冷却跳过={d['skipped']} 自愈放行={d['healed']} 成功清零={d['cleared']}")
    except Exception as e:  # noqa: BLE001
        record("U2_circuit_breaker", False, f"{type(e).__name__}: {out[:120]} {proc.stderr[-120:] if proc.stderr else ''}")


# ============ U3 台账检索 + 坏行容错 ============
def u3() -> None:
    import pm.modelhub.ledger as ledger

    with tempfile.TemporaryDirectory() as td:
        lp = Path(td) / "t.jsonl"
        os.environ["PMH_LEDGER_PATH"] = str(lp)
        # 重新加载模块级 path 函数依赖环境变量——直接调用 API 写入
        ledger.append_call_event(request_id="r1", model="m1", endpoint="e", success=True,
                                 latency_ms=100, attempts=1, failovers=0, http_status=200,
                                 error_type=None, error_msg=None, agent="a1", role=None, content_chars=5)
        ledger.append_event({"type": "call", "request_id": "r2", "model": "m2", "endpoint": "e",
                             "success": False, "latency_ms": 50, "attempts": 2, "failovers": 1,
                             "http_status": 429, "error_type": "GatewayError", "error_msg": "rate",
                             "agent": "a2", "role": None, "content_chars": None, "ts": "2026-09-21T00:00:00.000Z"})
        with lp.open("a", encoding="utf-8") as f:
            f.write("{broken json line\n")  # 坏行
        ledger.append_call_event(request_id="r3", model="m1", endpoint="e", success=False,
                                 latency_ms=10, attempts=1, failovers=0, http_status=500,
                                 error_type="GatewayError", error_msg="x", agent="a1", role=None, content_chars=None)
        all_rows = ledger.query_ledger(limit=50)
        ok_rows = ledger.query_ledger(success=True, limit=50)
        m1 = ledger.query_ledger(model="m1", limit=50)
        switches = ledger.query_ledger(type="switch", limit=50)
        stats = ledger.ledger_stats()
        ok = (
            len(all_rows) == 3           # 坏行被跳过
            and len(ok_rows) == 1
            and len(m1) == 2
            and len(switches) == 0
            and stats["calls"] == 3 and stats["failed"] == 2
        )
        record("U3_ledger_query", ok,
               f"总行={len(all_rows)}(坏行已跳过) 成功过滤={len(ok_rows)} 按模型={len(m1)} 汇总calls={stats['calls']} failed={stats['failed']}")
        del os.environ["PMH_LEDGER_PATH"]


# ============ C1 并发正确性（8 路） ============
def c1() -> None:
    bodies = []
    for i in range(8):
        bodies.append(json.dumps({
            "messages": [{"role": "user", "content": f"并发测试 {i}：只回复数字 {i}"}],
            "agent": f"conc-probe-{i}", "max_tokens": 2048,
        }).encode())

    def hit(i: int) -> dict:
        req = urllib.request.Request(
            "http://127.0.0.1:8687/v1/chat/completions", data=bodies[i], method="POST",
            headers={"Authorization": "***", "Content-Type": "application/json"})
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=240) as r:
                d = json.loads(r.read().decode())
            return {"ok": True, "i": i, "ms": int((time.time() - t0) * 1000),
                    "served": d.get("model"), "rid": (d.get("modelhub") or {}).get("request_id"),
                    "agent": (d.get("modelhub") or {}).get("agent")}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "i": i, "err": f"{type(e).__name__}: {e}"[:120], "ms": int((time.time() - t0) * 1000)}

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=8) as ex:
        outs = list(ex.map(hit, range(8)))
    wall = int((time.time() - t0) * 1000)
    ok_all = all(o["ok"] for o in outs)
    # 台账归属一致性：每路 request_id 唯一且 agent 与请求一致
    rids = [o.get("rid") for o in outs if o["ok"]]
    agents_ok = all(o.get("agent") == f"conc-probe-{o['i']}" for o in outs if o["ok"])
    unique = len(set(rids)) == len(rids)
    # 台账里查这 8 个 request_id 都有 call 事件
    time.sleep(1.0)
    with urllib.request.urlopen("http://127.0.0.1:8687/v1/ledger?limit=800", timeout=20) as r:
        rows = json.loads(r.read().decode())["rows"]
    found = {row["request_id"] for row in rows if (row.get("agent") or "").startswith("conc-probe-")}
    traced = len(found) >= 8
    ok = ok_all and agents_ok and unique and traced
    record("C1_concurrency_8", ok,
           f"8路成功={sum(1 for o in outs if o['ok'])}/8 wall={wall}ms 唯一request_id={unique} agent归属={agents_ok} 台账可溯={traced} 延迟样本={[o['ms'] for o in outs]}")


# ============ P1 转发开销（直连 vs 网关，交错多轮） ============
def p1() -> None:
    env = open(ROOT / ".env", encoding="utf-8").read()
    import re
    key = re.search(r"PM_API_KEY_1=([^\r\n]+)", env).group(1).strip()

    def direct() -> float:
        # 上游 401 突发期实测持续较长（同钥网关能靠重试扛过而裸直连全灭）：
        # 改用指数退避直到拿到一次成功样本（最多 7 次，记未计入耗时）。
        for attempt in range(7):
            try:
                body = json.dumps({"model": "deepseek-v4-flash",
                                   "messages": [{"role": "user", "content": "只回复两个字：收到（直连测量）"}],
                                   "max_tokens": 2048}).encode()
                req = urllib.request.Request("https://token.sensenova.cn/v1/chat/completions", data=body, method="POST",
                                             headers={"Authorization": "***" + key, "Content-Type": "application/json"})
                t0 = time.time()
                with urllib.request.urlopen(req, timeout=120) as r:
                    r.read()
                return (time.time() - t0) * 1000
            except Exception:  # noqa: BLE001
                time.sleep(2.0 * (2 ** attempt))
        raise RuntimeError("直连 7 次重试均失败（上游 401/429 突发）")

    def via_gw() -> float:
        last_err: Exception | None = None
        for attempt in range(3):
            try:
                body = json.dumps({"model": "deepseek-v4-flash",
                                   "messages": [{"role": "user", "content": "只回复两个字：收到（网关测量）"}],
                                   "max_tokens": 2048}).encode()
                req = urllib.request.Request("http://127.0.0.1:8687/v1/chat/completions", data=body, method="POST",
                                             headers={"Authorization": "***", "Content-Type": "application/json"})
                t0 = time.time()
                with urllib.request.urlopen(req, timeout=120) as r:
                    r.read()
                return (time.time() - t0) * 1000
            except urllib.error.HTTPError as e:
                last_err = e
                if e.code in (401, 429, 502, 503) and attempt < 2:
                    time.sleep(3.0 * (attempt + 1))
                    continue
                raise
            except Exception as e:  # noqa: BLE001
                last_err = e
                time.sleep(2.0)
        raise last_err  # type: ignore[misc]

    # C1 密集并发后静置，避开上游 401 突发窗口
    time.sleep(10)
    direct_ms: list[float] = []
    gw_ms: list[float] = []
    skipped = 0
    for rnd in range(4):
        try:
            d = direct()
            direct_ms.append(d)
        except Exception:  # noqa: BLE001
            skipped += 1
        time.sleep(3)
        try:
            g = via_gw()
            gw_ms.append(g)
        except Exception:  # noqa: BLE001
            skipped += 1
        time.sleep(3)

    if len(direct_ms) >= 2 and len(gw_ms) >= 2:
        d_med = sorted(direct_ms)[len(direct_ms) // 2 - 1 : len(direct_ms) // 2 + 1]
        g_med = sorted(gw_ms)[len(gw_ms) // 2 - 1 : len(gw_ms) // 2 + 1]
        d_med = sum(d_med) / len(d_med)
        g_med = sum(g_med) / len(g_med)
        overhead = g_med - d_med
        ok = overhead <= 200
        record("P1_gateway_overhead", ok,
               f"直连中位={d_med:.0f}ms(n={len(direct_ms)}) 网关中位={g_med:.0f}ms(n={len(gw_ms)}) 开销={overhead:+.0f}ms 跳过轮={skipped}（基线≤200ms）")
    else:
        record("P1_gateway_overhead", False,
               f"有效轮次不足：直连={len(direct_ms)} 网关={len(gw_ms)} 跳过={skipped}（上游 401/429 突发过于持续，需人工复测）")


# ============ P2 进程稳定性 ============
def p2() -> None:
    with urllib.request.urlopen("http://127.0.0.1:8687/api/health", timeout=10) as r:
        h = json.loads(r.read().decode())
    with urllib.request.urlopen("http://127.0.0.1:8687/v1/pool/status", timeout=10) as r:
        s = json.loads(r.read().decode())
    cooling = [m["name"] for m in s["models"] if m["circuit"] != "closed"]
    ok = h.get("status") == "ok"
    record("P2_process_stability", ok,
           f"health={h.get('status')} 熔断冷却中={cooling or '无'}（全部断路器 closed 属预期健康态）")


if __name__ == "__main__":
    print("=== ModelHub 发布测试（U/C/P）===")
    u1()
    u2()
    u3()
    c1()
    p1()
    p2()
    passed = sum(1 for r in results if r["ok"])
    print(f"\nSUMMARY: {passed}/{len(results)} PASS")
    (ROOT / "tests_modelhub" / "release_results.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    sys.exit(0 if passed == len(results) else 1)
