"""跨进程可见性验证：任务提交给 A 进程，状态与报告从 B 进程读。

这就是 `--workers > 1` 的核心诉求 —— 两个 uvicorn worker 就是两个进程。
单元测试里用「两个 SqliteStore 实例」复现同一件事（`tests/test_task_store.py`），
这个脚本是它在真实 HTTP + 真实进程下的版本，用来证明"共享记录"不是纸面结论。

用法（两个终端各起一个服务，共用同一个 PM_TASK_DB 与 PM_LOG_DIR）：

    # 终端 1
    PM_TASK_DB=/tmp/pm.db PM_LOG_DIR=/tmp/pmlogs PM_FAKE_BACKEND=progress \\
        python run_server.py --port 8096
    # 终端 2
    PM_TASK_DB=/tmp/pm.db PM_LOG_DIR=/tmp/pmlogs PM_FAKE_BACKEND=progress \\
        python run_server.py --port 8095
    # 终端 3
    python examples/run_multiworker_check.py 8096 8095

PM_FAKE_BACKEND=progress 让全流程走假后端，**不消耗任何真实模型调用**；
要验真实调用就去掉这个环境变量并配好 .env。
"""

import json
import sys
import time
import urllib.error
import urllib.request

PORT_SUBMIT = sys.argv[1] if len(sys.argv) > 1 else "8096"
PORT_QUERY = sys.argv[2] if len(sys.argv) > 2 else "8095"
A = f"http://127.0.0.1:{PORT_SUBMIT}"  # 提交用
B = f"http://127.0.0.1:{PORT_QUERY}"  # 查询用（另一个"worker"）


def get(url: str):
    with urllib.request.urlopen(url, timeout=10) as r:
        return r.status, r.read().decode("utf-8")


def main() -> int:
    # 1) 向 A 提交任务
    body = json.dumps(
        {"task": "让 AI 分析销售数据", "n_test_cases": 1, "max_iterations": 1}
    ).encode()
    req = urllib.request.Request(
        A + "/api/optimize", data=body, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        rid = json.loads(r.read())["run_id"]
    print(f"提交到 A({PORT_SUBMIT})：run_id={rid}")

    # 2) 立刻从 B 查询：记录若还在 A 的进程内，这里必然 404
    try:
        status_a, _ = get(f"{A}/api/status/{rid}")
    except urllib.error.HTTPError as e:
        status_a = e.code
    try:
        status_b, _ = get(f"{B}/api/status/{rid}")
    except urllib.error.HTTPError as e:
        status_b = e.code
    print(f"刚提交后立刻查询：A={status_a} B={status_b}（B 若为 404 说明记录没共享）")

    # 3) 全程只向 B 轮询到终态
    terminal = {"passed", "max_iterations", "failed", "early_stopped"}
    st = {}
    codes = []
    deadline = time.time() + 90
    while time.time() < deadline:
        try:
            code, text = get(f"{B}/api/status/{rid}")
        except urllib.error.HTTPError as e:
            code, text = e.code, ""
        codes.append(code)
        if code != 200:
            print(f"❌ B 返回 {code}：多 worker 下 status 会 404")
            return 1
        st = json.loads(text)
        if st.get("status") in terminal:
            break
        time.sleep(0.4)

    print(f"B 轮询 {len(codes)} 次，状态码集合={sorted(set(codes))}，最终状态={st.get('status')}")
    if st.get("status") not in terminal:
        print("❌ 未在超时内到达终态")
        return 1

    # 4) 报告也必须能从 B 取到
    try:
        code, report = get(f"{B}/api/report/{rid}")
    except urllib.error.HTTPError as e:
        code, report = e.code, ""
    ok_report = code == 200 and rid in report
    print(f"从 B 取报告：HTTP={code} 内容含 run_id={rid in report} 长度={len(report)}")

    # 5) 赛马记录同样跨进程：A 起赛马，B 查进度
    race_body = json.dumps(
        {
            "name": "跨进程赛马",
            "tasks": [
                {"task": "写一个数据分析提示词", "n_test_cases": 1, "max_iterations": 1},
                {"task": "写一个周报总结提示词", "n_test_cases": 1, "max_iterations": 1},
            ],
        }
    ).encode()
    req = urllib.request.Request(
        A + "/api/race", data=race_body, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        race_id = json.loads(r.read())["race_id"]
    try:
        code, rtext = get(f"{B}/api/race/{race_id}")
    except urllib.error.HTTPError as e:
        code, rtext = e.code, "{}"
    rdata = json.loads(rtext) if rtext else {}
    print(
        f"赛马：A 提交 {race_id}，从 B 查到 HTTP={code} total={rdata.get('total')} "
        f"runs={len(rdata.get('runs') or {})}"
    )

    ok = status_b == 200 and ok_report and code == 200 and len(rdata.get("runs") or {}) == 2
    print("结果：" + ("✅ 跨进程共享记录生效" if ok else "❌ 仍有跨进程不可见的项"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
