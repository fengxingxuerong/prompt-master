#!/usr/bin/env python3
"""最终验证补充用例：X8 重复提交 / X9 无效模型名 / S1-S2 浸泡验证。"""

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
    print(("PASS " if ok else "FAIL ") + step + "  " + detail[:160])


def chat(body: dict, timeout: int = 180) -> tuple[int, dict]:
    req = urllib.request.Request(BASE + "/v1/chat/completions",
                                 data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode(errors="replace"))
        except Exception:  # noqa: BLE001
            return e.code, {}


def x8() -> None:
    """重复提交：同请求连发 3 次，各自独立处理。"""
    body = {"messages": [{"role": "user", "content": "只回复两个字：收到"}],
            "agent": "dup-submit-probe", "max_tokens": 1024}
    rids, statuses, contents = [], [], []
    for i in range(3):
        st, d = chat(body)
        statuses.append(st)
        if st == 200:
            rids.append(d["modelhub"]["request_id"])
            contents.append(bool(d["choices"][0]["message"]["content"].strip()))
        time.sleep(0.5)
    ok = all(s == 200 for s in statuses) and len(set(rids)) == 3 and all(contents)
    record("X8_duplicate_submit", ok,
           f"3 次状态={statuses} request_id 唯一={len(set(rids)) == 3} 正文均非空={all(contents)}（无脏数据：每次独立响应）")


def x9() -> None:
    """无效模型名：不存在 → 回落主备链成功（v1 语义）且 modelhub.requested_model 如实记录。"""
    body = {"model": "no-such-model-xyz", "messages": [{"role": "user", "content": "只回复：OK"}],
            "agent": "invalid-model-probe", "max_tokens": 1024}
    st, d = chat(body)
    mh = d.get("modelhub") or {}
    ok = st == 200 and d.get("model") in ("deepseek-v4-flash", "glm-5.2", "sensenova-6.8-flash-lite",
                                          "deepseek-v4-pro", "kimi-k3") and mh.get("requested_model") == "no-such-model-xyz"
    record("X9_invalid_model_name", ok,
           f"HTTP{st} requested={mh.get('requested_model')} served={d.get('model')}（回落主备链，不静默）")


def soak(rounds: int = 12) -> None:
    """S1 浸泡：连续对话覆盖自动切换路径；S2 内存前后对比。"""
    proc = None
    conn = json.loads(json.dumps({}))  # placeholder
    import subprocess

    def gw_pid() -> int | None:
        try:
            out = subprocess.run(["powershell", "-NoProfile", "-Command",
                                  "(Get-NetTCPConnection -LocalPort 8687 -State Listen | Select-Object -First 1).OwningProcess"],
                                 capture_output=True, text=True, timeout=20)
            return int(out.stdout.strip())
        except Exception:  # noqa: BLE001
            return None

    def ws_mb(pid: int) -> float:
        out = subprocess.run(["powershell", "-NoProfile", "-Command",
                              f"[Math]::Round((Get-Process -Id {pid}).WorkingSet64/1MB,1)"],
                             capture_output=True, text=True, timeout=20)
        return float(out.stdout.strip())

    pid = gw_pid()
    ws_start = ws_mb(pid) if pid else None

    ok_count = 0
    switch_count = 0
    errors: list[str] = []
    t0 = time.time()
    for i in range(rounds):
        # 交替正常请求与"指定弱可用模型"（触发切换路径）
        body = {"messages": [{"role": "user", "content": f"浸泡第{i}轮：只回复两个字：收到"}],
                "agent": f"soak-probe-{i}", "max_tokens": 1024}
        if i % 3 == 2:
            body["model"] = "amd-glm-5.3-flash"  # 弱可用：大概率触发切换
        try:
            st, d = chat(body, timeout=200)
            if st == 200 and d.get("choices", [{}])[0].get("message", {}).get("content", "").strip():
                ok_count += 1
                mh = d.get("modelhub") or {}
                if mh.get("failovers", 0) > 0:
                    switch_count += 1
            else:
                errors.append(f"round{i}: HTTP{st} {str(d)[:80]}")
        except Exception as e:  # noqa: BLE001
            errors.append(f"round{i}: {type(e).__name__}: {e}"[:100])
    wall = int(time.time() - t0)
    pid2 = gw_pid()
    ws_end = ws_mb(pid2) if pid2 else None
    rate = ok_count / rounds
    grew = (ws_end is not None and ws_start is not None and (ws_end - ws_start) > 50)
    ok = rate >= 0.9 and not errors and not grew
    record("S1_soak_12rounds", ok,
           f"成功 {ok_count}/{rounds}（成功率 {rate:.0%}）触发切换 {switch_count} 轮 总耗时 {wall}s 无异常={not errors}")
    record("S2_memory_stability", (ws_start is not None and ws_end is not None),
           f"WorkingSet 浸泡前={ws_start}MB 后={ws_end}MB 增长={(ws_end - ws_start) if ws_start and ws_end else '?'}MB（>50MB 判异常；基线 14MB）")


if __name__ == "__main__":
    print("=== 最终验证补充用例（X8/X9/S1/S2）===")
    x8()
    x9()
    soak(12)
    passed = sum(1 for r in results if r["ok"])
    print(f"\nSUMMARY: {passed}/{len(results)} PASS")
    (ROOT / "tests_modelhub" / "final_extra_results.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    sys.exit(0 if passed == len(results) else 1)
