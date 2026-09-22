#!/usr/bin/env python3
"""异常路径注入测试：每类异常都要"显式报错 + 明确指引"，绝不静默失败。

X1 配置文件缺失        → ConfigError（提示可从 *.example 复制）
X2 配置文件损坏        → ConfigError（指明行号与原因）
X3 环境变量密钥缺失    → ConfigError（指明缺失的变量名）
X4 密钥无效            → 全池 auth 失败 → ModelPoolExhaustedError（附最后错误）
X5 全池不可达          → ModelPoolExhaustedError（附尝试顺序）
X6 空池（全 disabled） → ConfigError
每项测试用独立子进程跑，PMH_CONFIG 指向临时文件，不影响正式配置。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(r"D:\projects\prompt-master")
sys.path.insert(0, str(ROOT))
from pm.bootstrap import ensure_utf8_stdio  # noqa: E402

ensure_utf8_stdio()
results: list[dict] = []


def record(step: str, ok: bool, detail: str) -> None:
    results.append({"step": step, "ok": ok, "detail": detail})
    print(("PASS " if ok else "FAIL ") + step + "  " + detail[:160])


RUNNER = r"""
import json, sys
sys.path.insert(0, r"D:\projects\prompt-master")
from pm.modelhub.pool import ModelHub, ConfigError, ModelPoolExhaustedError
mode = sys.argv[1]
cfg = sys.argv[2]
try:
    hub = ModelHub(config_path=__import__("pathlib").Path(cfg))
    if mode == "load_only":
        print("LOAD_OK"); sys.exit(0)
    hub.chat([{"role": "user", "content": "hi"}], agent="exception-probe", max_tokens=256)
    print("CHAT_OK"); sys.exit(0)
except ConfigError as e:
    print("CONFIG_ERROR::" + str(e)); sys.exit(3)
except ModelPoolExhaustedError as e:
    print("POOL_EXHAUSTED::" + str(e)); sys.exit(4)
except Exception as e:
    print("OTHER::" + type(e).__name__ + "::" + str(e)); sys.exit(5)
"""


def run_case(mode: str, cfg_path: str) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, "-c", RUNNER, mode, cfg_path],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
        cwd=str(ROOT),
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    return proc.returncode, (proc.stdout or proc.stderr).strip()


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="modelhub_exc_"))
    real = json.loads((ROOT / "config" / "modelhub.json").read_text(encoding="utf-8"))

    # X1 缺失
    rc, out = run_case("load_only", str(tmp / "no_such.json"))
    record("X1_missing_config", rc == 3 and "配置文件不存在" in out, out[:150])

    # X2 损坏
    bad = tmp / "broken.json"
    bad.write_text('{"pool": [ {"name": "x", "base_url": ', encoding="utf-8")
    rc, out = run_case("load_only", str(bad))
    record("X2_broken_json", rc == 3 and "损坏" in out and "行" in out, out[:150])

    # X3 密钥环境变量缺失
    miss = tmp / "missing_env.json"
    spec = dict(real["pool"][0])
    spec["api_key"] = "${PMH_NO_SUCH_KEY_VAR}"
    (tmp / "missing_env.json").write_text(
        json.dumps({"version": 1, "pool": [spec]}, ensure_ascii=False), encoding="utf-8"
    )
    rc, out = run_case("load_only", str(tmp / "missing_env.json"))
    record("X3_missing_env_key", rc == 3 and "PMH_NO_SUCH_KEY_VAR" in out, out[:150])

    # X4 密钥无效（真实端点 + 假密钥）
    wrong = tmp / "wrong_key.json"
    spec = dict(real["pool"][0])
    spec["api_key"] = "sk-invalid-key-for-exception-test-000"
    spec["timeout_seconds"] = 20
    wrong.write_text(
        json.dumps({"version": 1, "pool": [spec]}, ensure_ascii=False), encoding="utf-8"
    )
    rc, out = run_case("chat", str(wrong))
    record(
        "X4_invalid_key",
        rc == 4 and "POOL_EXHAUSTED" in out and ("401" in out or "403" in out or "Unauthorized" in out),
        out[:180],
    )

    # X5 全池不可达（不存在的域名，短超时）
    dead = tmp / "dead_pool.json"
    spec2 = dict(spec)
    spec2["base_url"] = "https://127.0.0.1:9"
    dead.write_text(
        json.dumps({"version": 1, "pool": [spec2]}, ensure_ascii=False), encoding="utf-8"
    )
    rc, out = run_case("chat", str(dead))
    record("X5_all_unreachable", rc == 4 and "全部失败" in out, out[:180])

    # X6 空池
    empty = tmp / "empty.json"
    entries = [dict(e, enabled=False) for e in real["pool"]]
    empty.write_text(
        json.dumps({"version": 1, "pool": entries}, ensure_ascii=False), encoding="utf-8"
    )
    rc, out = run_case("load_only", str(empty))
    record("X6_all_disabled", rc == 3 and "没有任何 enabled=true" in out, out[:150])

    passed = sum(1 for r in results if r["ok"])
    print(f"\nSUMMARY: {passed}/{len(results)} PASS")
    (tmp / "exception_results.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print("RESULTS_FILE: " + str(tmp / "exception_results.json"))
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()
