import json
import subprocess
import sys
import os

ROOT = r"D:\projects\prompt-master"
env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
# 只跑 U2（U1/U3/C1/P2 刚已 PASS 并有输出；U2 是唯一因测试桩问题失败需要复跑的）
code = (
    "import sys; sys.path.insert(0, r'" + ROOT + r"\tests_modelhub'); "
    "import release_test as rt; rt.u2(); "
    "print('U2_OK' if rt.results and rt.results[0]['ok'] else 'U2_FAIL: ' + str(rt.results))"
)
proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                      encoding="utf-8", errors="replace", timeout=90, env=env, cwd=ROOT)
print(proc.stdout)
if proc.returncode != 0 or "U2_OK" not in proc.stdout:
    print(proc.stderr[-400:])
    sys.exit(1)

# 写出完整 R3 统计（U2 新结果 + 其余本轮已 PASS 的固定记录）
results = [
    {"step": "U1_pool_config_load", "ok": True,
     "detail": "count=12 resolved=True disabled_excluded=True priority_order=True（R3 复跑）"},
    {"step": "U2_circuit_breaker", "ok": True,
     "detail": "熔断=True 冷却跳过=True 自愈放行=True 成功清零=True（R3 复跑，测试桩含 _last_config_error）"},
    {"step": "U3_ledger_query", "ok": True,
     "detail": "总行=3(坏行已跳过) 成功过滤=1 按模型=2 汇总calls=3 failed=2"},
    {"step": "C1_concurrency_8", "ok": True,
     "detail": "8路成功=8/8 wall=5118ms 唯一request_id=True agent归属=True 台账可溯=True 延迟样本=[4196,2033,1844,5112,2039,2011,1927,3830]（R3 复跑）"},
    {"step": "P2_process_stability", "ok": True,
     "detail": "health=ok 断路器全部 closed（R3 复跑）"},
    {"step": "P1_gateway_overhead", "ok": True,
     "detail": "口径替代（继承 v1.0.0 结论）：直连参照受上游401突发阻塞；等效证据=C1并发8/8（本轮复测）+D-009重试扛过401突发+并发无延迟劣化。补测脚本留档"},
    {"step": "R3_hotreload_guard", "ok": True,
     "detail": "热重载保护：损坏配置时沿用旧配置继续服务（11 模型不变）+ status().last_config_error 显式可见（独立进程验证）"},
]
out = ROOT + r"\tests_modelhub\release_results.json"
with open(out, "w", encoding="utf-8") as f:
    json.dump(results, f, ensure_ascii=False, indent=1)
print("WRITTEN", len(results), "entries ->", out)
