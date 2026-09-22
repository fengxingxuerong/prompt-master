#!/usr/bin/env python3
"""v1.1.0 新功能测试：流式 / 虚拟密钥 / 用量 / metrics / 控制台 / 持久化。

    .venv\\Scripts\\python.exe tests_modelhub\\v111_test.py
产物：tests_modelhub/v111_results.json
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

ROOT = Path(r"D:\projects\prompt-master")
sys.path.insert(0, str(ROOT))
from pm.bootstrap import ensure_utf8_stdio  # noqa: E402

ensure_utf8_stdio()

BASE = "http://127.0.0.1:8687"
results: list[dict] = []


def record(step: str, ok: bool, detail: str) -> None:
    results.append({"step": step, "ok": ok, "detail": detail})
    print(("PASS " if ok else "FAIL ") + step + "  " + detail[:150])


def post(path: str, body: dict, headers: dict | None = None, timeout: int = 120) -> tuple[int, dict | bytes]:
    h = {"Content-Type": "application/json"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(), method="POST", headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            try:
                return r.status, json.loads(raw.decode())
            except json.JSONDecodeError:
                return r.status, raw
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode(errors="replace"))
        except Exception:  # noqa: BLE001
            return e.code, {}


def get(path: str, headers: dict | None = None, timeout: int = 30) -> tuple[int, dict]:
    h = {}
    if headers:
        h.update(headers)
    req = urllib.request.Request(BASE + path, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode(errors="replace"))
        except Exception:  # noqa: BLE001
            return e.code, {}


# ---------- V1 流式 SSE ----------
def v1() -> None:
    st, body = post("/v1/chat/completions", {
        "messages": [{"role": "user", "content": "从 1 数到 5，用顿号分隔，不要其他内容"}],
        "stream": True, "max_tokens": 2048,
    })
    if st != 200 or not isinstance(body, bytes):
        record("V1_stream_sse", False, f"HTTP {st}: {str(body)[:120]}")
        return
    text = body.decode("utf-8", errors="replace")
    lines = [l for l in text.split("\n\n") if l.strip()]
    content_parts = []
    has_done = any("[DONE]" in l for l in lines)
    model = "?"
    for l in lines:
        if l.startswith("data:") and "[DONE]" not in l:
            try:
                o = json.loads(l[5:].strip())
                model = o.get("model") or model
                for ch in o.get("choices") or []:
                    piece = (ch.get("delta") or {}).get("content") or ""
                    if piece:
                        content_parts.append(piece)
            except json.JSONDecodeError:
                pass
    content = "".join(content_parts)
    ok = has_done and len(content) > 0
    record("V1_stream_sse", ok, f"model={model} chunks={len(lines)} content={content[:40]!r} DONE={has_done}")


# ---------- V2 虚拟密钥全生命周期 ----------
def v2() -> None:
    # 签发
    st, issued = post("/v1/keys", {"agent": "vk-test-agent", "note": "iteration test"})
    if st != 200 or not issued.get("ok"):
        record("V2_vkeys_lifecycle", False, f"签发失败 HTTP {st}: {str(issued)[:120]}")
        return
    key = issued["key"]["key"]
    # 用 vk- 密钥对话（归属强制为签发 agent）
    st1, chat1 = post("/v1/chat/completions", {
        "messages": [{"role": "user", "content": "只回复：OK"}], "max_tokens": 2048,
    }, headers={"X-API-Key": key})
    mh = chat1.get("modelhub") or {} if isinstance(chat1, dict) else {}
    agent_ok = mh.get("agent") == "vk-test-agent"
    # 伪造 agent 不生效（vk- 归属优先）
    st2, chat2 = post("/v1/chat/completions", {
        "messages": [{"role": "user", "content": "只回复：OK"}], "agent": "fake-agent", "max_tokens": 2048,
    }, headers={"X-API-Key": key})
    mh2 = chat2.get("modelhub") or {} if isinstance(chat2, dict) else {}
    spoof_blocked = mh2.get("agent") == "vk-test-agent"
    # 停用后 → 401
    import urllib.request as u
    req = u.Request(BASE + "/v1/keys/" + key, data=json.dumps({"enabled": False}).encode(),
                    method="PATCH", headers={"Content-Type": "application/json"})
    with u.urlopen(req, timeout=15) as r:
        patched = r.status
    st3, _ = post("/v1/chat/completions", {"messages": [{"role": "user", "content": "hi"}]},
                  headers={"X-API-Key": key})
    disabled_rejected = st3 == 401
    # 清理：删除
    req = u.Request(BASE + "/v1/keys/" + key, method="DELETE", headers={"Content-Type": "application/json"})
    with u.urlopen(req, timeout=15) as r:
        deleted = r.status == 200
    ok = st1 == 200 and agent_ok and spoof_blocked and patched == 200 and disabled_rejected and deleted
    record("V2_vkeys_lifecycle", ok,
           f"签发✓ vk对话✓(agent={mh.get('agent')}) 伪造拦截={spoof_blocked} 停用✓ 停用后401={disabled_rejected} 删除✓")


# ---------- V3 用量统计 ----------
def v3() -> None:
    st, d = get("/v1/usage")
    totals = d.get("totals") or {}
    by_agent = d.get("by_agent") or {}
    ok = st == 200 and isinstance(totals.get("calls"), int) and totals["calls"] > 0 and len(by_agent) > 0
    sample_agent = next(iter(by_agent), None)
    sample = by_agent.get(sample_agent, {}) if sample_agent else {}
    sample_model = next(iter(sample), None)
    record("V3_usage_endpoint", ok,
           f"calls={totals.get('calls')} success_rate={totals.get('success_rate')} agents={list(by_agent)[:4]} 样本[{sample_agent}/{sample_model}]={json.dumps(sample.get(sample_model, {}), ensure_ascii=False)[:120]}")


# ---------- V4 metrics ----------
def v4() -> None:
    st, d = get("/v1/metrics")
    need = ("modelhub_calls_total", "modelhub_success_rate", "modelhub_switches_total",
            "modelhub_pool_models", "modelhub_circuits")
    ok = st == 200 and all(k in d for k in need)
    record("V4_metrics_endpoint", ok,
           f"calls={d.get('modelhub_calls_total')} rate={d.get('modelhub_success_rate')} switches={d.get('modelhub_switches_total')} pool={d.get('modelhub_pool_models')} circuits_open={d.get('modelhub_pool_circuits_open')}")


# ---------- V5 控制台 ----------
def v5() -> None:
    req = urllib.request.Request(BASE + "/console")
    with urllib.request.urlopen(req, timeout=15) as r:
        html = r.read().decode("utf-8", errors="replace")
    need = ("模型池", "调用台账", "虚拟密钥", "对话测试", "/v1/pool/status", "/v1/chat/completions")
    ok = r.status == 200 and all(k in html for k in need)
    record("V5_console_page", ok, f"HTTP {r.status} size={len(html)} 含四面板入口={all(k in html for k in need)}")


# ---------- V6 注册持久化（重启不丢） ----------
def v6() -> None:
    st, reg = post("/v1/agents", {"name": "persist-probe", "framework": "test", "description": "persistence probe"})
    if st != 200:
        record("V6_agents_persist", False, f"注册失败 HTTP {st}: {str(reg)[:120]}")
        return
    persisted = reg.get("persisted") is True
    # 直接检查落盘文件
    data_file = ROOT / "data" / "agents.json"
    on_disk = data_file.exists() and "persist-probe" in data_file.read_text(encoding="utf-8")
    # 清理
    req = urllib.request.Request(BASE + "/v1/agents/persist-probe", method="DELETE")
    with urllib.request.urlopen(req, timeout=15) as r:
        removed = r.status == 200
    ok = persisted and on_disk and removed
    record("V6_agents_persist", ok, f"persisted={persisted} data/agents.json落盘={on_disk} 注销={removed}")


# ---------- V7 损坏落盘文件显式报错（D6 口径） ----------
def v7() -> None:
    run = r"""
import sys, pathlib, json, tempfile, os
sys.path.insert(0, r"D:\projects\prompt-master")
os.environ["PMH_DATA_DIR"] = sys.argv[1]
from pm.modelhub.vkeys import VirtualKeyStore
p = pathlib.Path(sys.argv[1]) / "vkeys.json"
p.write_text("{broken", encoding="utf-8")
try:
    VirtualKeyStore()
    print("NO_ERROR")
except Exception as e:
    print("ERR::" + type(e).__name__ + "::" + str(e)[:120])
"""
    import subprocess
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        proc = subprocess.run([sys.executable, "-c", run, td], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=60,
                              env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"})
        out = (proc.stdout or "").strip()
        ok = out.startswith("ERR::") and "损坏" in out and "ConfigError" in out
        record("V7_broken_vkeys_file", ok, out[:130])


if __name__ == "__main__":
    print("=== ModelHub v1.1.0 新功能测试 ===")
    v1()
    v2()
    v3()
    v4()
    v5()
    v6()
    v7()
    passed = sum(1 for r in results if r["ok"])
    print(f"\nSUMMARY: {passed}/{len(results)} PASS")
    (ROOT / "tests_modelhub" / "v111_results.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    sys.exit(0 if passed == len(results) else 1)
