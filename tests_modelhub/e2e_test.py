#!/usr/bin/env python3
"""端到端验收测试：ModelHub 网关（v1.1 验收矩阵）。

场景：
  A. 正常对话：openai SDK 接入（agent=openai-sdk-demo）+ 固定角色 assistant
  B. 正常对话：LangChain ChatOpenAI 接入（agent=langchain-demo，第二种框架）
  C. 原生 HTTP 接入（agent=raw-http-demo；指定 model= 不在池内的名字 → 回落主备链）
  D. 指定弱可用模型：优先目标模型，不可用自动切换/上游自愈直接成功均算过（熔断语义有单测覆盖）
  E. 角色固定：role=analyst 下分别用两个不同模型对话，验证系统提示词固定注入
  F. 异常注入-未知角色：role=不存在的角色 → 422 显式报错（不静默）
  G. 流式：stream=true → SSE 正式能力（v1.1 起；上游不可流式时降级合成并标注）
  H. 台账核验：按 agent/model/success 检索全部调用与切换事件
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

BASE = "http://127.0.0.1:8687/v1"
results: list[dict] = []


def record(step: str, ok: bool, detail: str) -> None:
    results.append({"step": step, "ok": ok, "detail": detail})
    print(("PASS " if ok else "FAIL ") + step + "  " + detail[:150])


# ---------- A. openai SDK ----------
def case_a() -> None:
    try:
        from openai import OpenAI

        client = OpenAI(base_url=BASE, api_key="***", timeout=90)
        t0 = time.time()
        r = client.chat.completions.create(
            model="deepseek-v4-flash",
            messages=[{"role": "user", "content": "用一句话回答：模型池网关是什么？"}],
            extra_body={"agent": "openai-sdk-demo", "role": "assistant", "max_tokens": 2048},
        )
        content = r.choices[0].message.content or ""
        mh = getattr(r, "modelhub", None) or {}
        record(
            "A_openai_sdk",
            bool(content.strip()),
            f"model={r.model} latency={int((time.time()-t0)*1000)}ms chars={len(content)} request_id={mh.get('request_id')}",
        )
    except Exception as e:  # noqa: BLE001
        record("A_openai_sdk", False, f"{type(e).__name__}: {e}")


# ---------- B. LangChain ----------
def case_b() -> None:
    try:
        from langchain_openai import ChatOpenAI

        llm = ChatOpenAI(
            base_url=BASE,
            api_key="***",
            model="glm-5.2",
            temperature=0.3,
            max_tokens=2048,
            timeout=90,
        )
        t0 = time.time()
        resp = llm.invoke(
            "用一句话回答：LangChain 如何接入统一模型池？",
            config={"configurable": {}},
            extra_body={"agent": "langchain-demo", "role": "assistant"},
        )
        content = resp.content if isinstance(resp.content, str) else str(resp.content)
        record(
            "B_langchain",
            bool(content.strip()),
            f"model={resp.response_metadata.get('model')} latency={int((time.time()-t0)*1000)}ms chars={len(content)}",
        )
    except Exception as e:  # noqa: BLE001
        record("B_langchain", False, f"{type(e).__name__}: {e}")


# ---------- C. 原生 HTTP + 未知模型名回落 ----------
def case_c() -> None:
    body = {
        "model": "totally-unknown-model",
        "messages": [{"role": "user", "content": "只回复两个字：收到"}],
        "agent": "raw-http-demo",
        "max_tokens": 1024,
    }
    req = urllib.request.Request(
        BASE + "/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Authorization": "***", "Content-Type": "application/json"},
        method="POST",
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            d = json.loads(r.read().decode())
        mh = d.get("modelhub") or {}
        ok = bool((d.get("choices") or [{}])[0].get("message", {}).get("content", "").strip())
        record(
            "C_raw_http_unknown_model",
            ok,
            f"HTTP{r.status} served={d.get('model')} failovers={mh.get('failovers')} latency={int((time.time()-t0)*1000)}ms",
        )
    except urllib.error.HTTPError as e:
        record("C_raw_http_unknown_model", False, f"HTTP{e.code}: {e.read().decode(errors='replace')[:120]}")
    except Exception as e:  # noqa: BLE001
        record("C_raw_http_unknown_model", False, f"{type(e).__name__}: {e}")


# ---------- D. 指定弱可用模型（切换或自愈均合格） ----------
def case_d() -> None:
    body = {
        "model": "amd-glm-5.3-flash",
        "messages": [{"role": "user", "content": "只回复两个字：收到"}],
        "agent": "failover-probe",
        "max_tokens": 1024,
    }
    req = urllib.request.Request(
        BASE + "/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Authorization": "***", "Content-Type": "application/json"},
        method="POST",
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=240) as r:
            d = json.loads(r.read().decode())
        mh = d.get("modelhub") or {}
        served = d.get("model")
        ok = bool((d.get("choices") or [{}])[0].get("message", {}).get("content", "").strip())
        tag = "自动切换" if served != "amd-glm-5.3-flash" else "上游自愈直接成功"
        record(
            "D_forced_failover",
            ok,
            f"requested=amd-glm-5.3-flash → served={served}（{tag}） failovers={mh.get('failovers')} latency={int((time.time()-t0)*1000)}ms request_id={mh.get('request_id')}",
        )
    except urllib.error.HTTPError as e:
        record("D_forced_failover", False, f"HTTP{e.code}: {e.read().decode(errors='replace')[:140]}")
    except Exception as e:  # noqa: BLE001
        record("D_forced_failover", False, f"{type(e).__name__}: {e}")


# ---------- E. 角色固定不漂移 ----------
def case_e() -> None:
    try:
        from openai import OpenAI

        client = OpenAI(base_url=BASE, api_key="***", timeout=120)
        outs = []
        for m in ("deepseek-v4-flash", "glm-5.2"):
            r = client.chat.completions.create(
                model=m,
                messages=[{"role": "user", "content": "你的角色设定是什么？一句话。"}],
                extra_body={"agent": "role-fixed-probe", "role": "analyst", "max_tokens": 2048},
            )
            outs.append((m, (r.choices[0].message.content or "").strip()))
        keywords = ("结论", "依据", "分析", "数据")
        hit = [m for m, c in outs if any(k in c for k in keywords)]
        record(
            "E_role_fixed",
            len(hit) == 2,
            f"两模型均注入固定 analyst 人设：命中角色特征 {hit}；回答1={outs[0][1][:40]} / 回答2={outs[1][1][:40]}",
        )
    except Exception as e:  # noqa: BLE001
        record("E_role_fixed", False, f"{type(e).__name__}: {e}")


# ---------- F. 未知角色 → 422 ----------
def case_f() -> None:
    body = {
        "messages": [{"role": "user", "content": "hi"}],
        "role": "no-such-role",
        "agent": "error-path-probe",
    }
    req = urllib.request.Request(
        BASE + "/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Authorization": "***", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            record("F_unknown_role_422", False, f"期望 422 实际 HTTP{r.status}（静默失败风险！）")
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        record("F_unknown_role_422", e.code == 422, f"HTTP{e.code}: {detail[:120]}")
    except Exception as e:  # noqa: BLE001
        record("F_unknown_role_422", False, f"{type(e).__name__}: {e}")


# ---------- G. stream=true → v1.1 起支持流式（SSE 正式能力） ----------
def case_g() -> None:
    body = {
        "messages": [{"role": "user", "content": "只回复两个字：收到"}],
        "stream": True,
        "agent": "stream-probe",
        "max_tokens": 1024,
    }
    req = urllib.request.Request(
        BASE + "/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Authorization": "***", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            ctype = r.headers.get("Content-Type", "")
            raw = r.read().decode("utf-8", errors="replace")
        is_sse = "text/event-stream" in ctype
        has_done = "[DONE]" in raw
        has_text = "收到" in raw
        degraded = r.headers.get("X-Modelhub-Degraded", "") == "true"
        record(
            "G_stream_sse_supported",
            r.status == 200 and is_sse and has_done and has_text,
            f"HTTP{r.status} ctype={ctype} DONE={has_done} 含正文={has_text} 降级合成={degraded}"
            "（v1.1 起流式为正式能力；上游不可流式时自动降级并在 X-Modelhub-Degraded 标注）",
        )
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        record("G_stream_sse_supported", False, f"HTTP{e.code}: {detail[:120]}")
    except Exception as e:  # noqa: BLE001
        record("G_stream_sse_supported", False, f"{type(e).__name__}: {e}")


# ---------- H. 台账核验 ----------
def case_h() -> None:
    time.sleep(1.0)
    try:
        with urllib.request.urlopen(BASE + "/ledger?limit=500", timeout=20) as r:
            d = json.loads(r.read().decode())
        rows = d.get("rows", [])
        calls = [x for x in rows if x.get("type") == "call"]
        switches = [x for x in rows if x.get("type") == "switch"]
        # 只有真实触达上游的 agent 才会有 call 事件；error-path-probe（F）
        # 在网关层被 422 拒绝，不产生调用 —— 这正是异常路径的正确语义。
        agents = {x.get("agent") for x in calls if x.get("agent")}
        need = {"openai-sdk-demo", "langchain-demo", "raw-http-demo", "failover-probe", "role-fixed-probe", "stream-probe"}
        missing = need - agents
        with_switch = [x for x in calls if x.get("failovers", 0) > 0]
        record(
            "H_ledger_traceable",
            not missing and len(switches) > 0 and len(with_switch) > 0,
            f"calls={len(calls)} switches={len(switches)} agents_covered={len(need)-len(missing)}/{len(need)} missing={missing or '无'} 切换留痕={len(with_switch)}条",
        )
    except Exception as e:  # noqa: BLE001
        record("H_ledger_traceable", False, f"{type(e).__name__}: {e}")


if __name__ == "__main__":
    print("=== ModelHub v1.1 端到端验收 ===")
    case_a()
    case_b()
    case_c()
    case_d()
    case_e()
    case_f()
    case_g()
    case_h()
    passed = sum(1 for r in results if r["ok"])
    print(f"\nSUMMARY: {passed}/{len(results)} PASS")
    Path(__file__).with_name("e2e_results.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    sys.exit(0 if passed == len(results) else 1)
