"""五维基线体检：性能 / SEO / 可访问性 / 安全 / 代码质量（同口径，复测复用本脚本）。

用法：python five_dim_audit.py baseline|retest
输出：releases/suite/five_dim_<label>.json
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
import json, os, re, time, urllib.request, urllib.error
from pathlib import Path

ROOT = Path(r"D:\projects\prompt-master")
OUT = ROOT / "releases" / "suite"
label = sys.argv[1] if len(sys.argv) > 1 else "baseline"

SERVICES = {
    "modelhub": ("http://127.0.0.1:8687", [
        ("/", "html"), ("/v1/models", "json"), ("/console", "html"), ("/v1/usage", "json"), ("/v1/metrics", "json")]),
    "lobster": ("http://127.0.0.1:8791", [("/", "html"), ("/ledger", "html"), ("/poster", "html")]),
    "taskboard": ("http://127.0.0.1:8792", [("/", "html"), ("/rules", "md"), ("/exceptions", "md"), ("/handoff", "md")]),
}

def fetch(url, timeout=15):
    t0 = time.time()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, r.read(), int((time.time()-t0)*1000), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), int((time.time()-t0)*1000), dict(e.headers or {})
    except Exception as e:
        return None, str(e).encode(), int((time.time()-t0)*1000), {}

def perf_and_a11y():
    perf, seo, a11y = [], [], []
    for name, (base, pages) in SERVICES.items():
        for path, kind in pages:
            st, body, ms, hdrs = fetch(base + path)
            ok = st == 200
            perf.append({"svc": name, "path": path, "status": st, "ms": ms, "size": len(body)})
            if kind != "html" or not ok:
                continue
            text = body.decode("utf-8", errors="replace")
            title = re.search(r"<title>(.*?)</title>", text, re.S)
            desc = re.search(r'<meta name="description" content="(.*?)"', text)
            viewport = 'name="viewport"' in text
            lang = re.search(r"<html[^>]*\blang=", text)
            h1s = len(re.findall(r"<h1[ >]", text))
            h2s = len(re.findall(r"<h2[ >]", text))
            imgs = re.findall(r"<img\b[^>]*>", text)
            img_noalt = [i for i in imgs if "alt=" not in i]
            seo.append({"svc": name, "path": path, "title": bool(title and title.group(1).strip()),
                        "title_len": len(title.group(1).strip()) if title else 0,
                        "meta_desc": bool(desc), "viewport": viewport, "lang": bool(lang),
                        "h1": h1s, "h2": h2s})
            a11y.append({"svc": name, "path": path, "img_total": len(imgs), "img_noalt": len(img_noalt),
                         "h1_count": h1s, "lang_attr": bool(lang), "viewport": viewport})
    return perf, seo, a11y

def security():
    checks = []
    # 1) 鉴权现状（未鉴权 PATCH 实测——只读探测用 OPTIONS/GET，写探测放 optimize 后复测）
    for name, port in (("taskboard", 8792), ("lobster", 8791), ("modelhub", 8687)):
        st, body, ms, hdrs = fetch(f"http://127.0.0.1:{port}/api/health" if name != "modelhub" else "http://127.0.0.1:8687/api/health")
        auth_header = hdrs.get("WWW-Authenticate") or hdrs.get("www-authenticate")
        checks.append({"name": f"{name}_auth_challenge", "value": bool(auth_header),
                       "note": "无 WWW-Authenticate = 写接口无强制鉴权" if not auth_header else "有挑战头"})
    # 2) PII 明文（orders.json）
    orders = ROOT / "releases" / "lobster" / "data" / "orders.json"
    if orders.exists():
        raw = orders.read_text(encoding="utf-8")
        pii = len(re.findall(r'"phone": "1[3-9]\d{9}"', raw))
        checks.append({"name": "lobster_pii_plain_phones", "value": pii,
                       "note": ">0 即明文手机号落盘" })
    # 3) CORS
    st, body, ms, hdrs = fetch("http://127.0.0.1:8687/api/health")
    acao = hdrs.get("Access-Control-Allow-Origin") or hdrs.get("access-control-allow-origin")
    checks.append({"name": "cors_acao_present", "value": bool(acao), "note": "未配置=同源兜底（可接受）"})
    # 4) 依赖锁定文件存在
    checks.append({"name": "modelhub_requirements_lock", "value": (ROOT / "releases" / "requirements.lock").exists()})
    checks.append({"name": "lobster_requirements_txt", "value": (ROOT / "releases" / "lobster" / "requirements.txt").exists()})
    # 5) 敏感文件暴露（本地文件系统口径）
    checks.append({"name": "desktop_plain_key_file", "value": Path(r"D:\Desktop\新建 Text Document.txt").exists(),
                   "note": "桌面明文密钥（高）"})
    return checks

def code_quality():
    import subprocess
    files = [r"D:\projects\prompt-master\pm\modelhub\server.py",
             r"D:\projects\prompt-master\pm\modelhub\pool.py",
             r"D:\projects\prompt-master\releases\taskboard\app.py",
             r"D:\projects\prompt-master\releases\lobster\app.py"]
    checks = []
    for f in files:
        proc = subprocess.run([sys.executable, "-m", "py_compile", f], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=60)
        checks.append({"file": Path(f).name, "compile": proc.returncode == 0,
                       "err": (proc.stderr or "")[:120]})
    return checks

if __name__ == "__main__":
    perf, seo, a11y = perf_and_a11y()
    sec = security()
    qual = code_quality()
    result = {"label": label, "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
              "measurement": "urllib 同口径计时 + 正则静态检查 + py_compile",
              "performance": perf, "seo": seo, "accessibility": a11y,
              "security": sec, "code_quality": qual}
    perf_ok = sum(1 for p in perf if p["status"] == 200)
    seo_pass = sum(1 for s in seo if s["title"] and s["viewport"])
    a11y_issues = sum(1 for a in a11y if a["img_noalt"] or not a["lang_attr"])
    sec_issues = [c for c in sec if c["name"] in ("lobster_pii_plain_phones", "desktop_plain_key_file") and c["value"]] \
                 + [c for c in sec if c["name"] == "taskboard_auth_challenge" and not c["value"]]
    result["summary"] = {"perf_ok": f"{perf_ok}/{len(perf)}", "seo_pass": f"{seo_pass}/{len(seo)}",
                         "a11y_issues": a11y_issues, "sec_issues": len(sec_issues),
                         "compile_ok": sum(1 for c in qual if c["compile"])}
    OUT.mkdir(parents=True, exist_ok=True)
    out = OUT / f"five_dim_{label}.json"
    out.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    print("FIVE_DIM", label, json.dumps(result["summary"], ensure_ascii=False))
