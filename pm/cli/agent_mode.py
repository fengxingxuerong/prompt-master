"""submit / status / report / wait：常驻 server 的异步客户端子命令。（自 run.py 拆出，逻辑逐字保留）"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any, cast

from .history import _TERMINAL_STATUS
from .support import (
    EXIT_FAILED,
    _result_exit_code,
    assert_mode_arg,
    read_cases_file,
    read_seed_prompt,
)


def _http_json(
    method: str, url: str, payload: dict[str, Any] | None = None, timeout: float = 30.0
) -> dict[str, Any]:
    """极简 JSON HTTP 客户端。非 2xx / 连不上都抛 RuntimeError（带可执行建议）。"""
    import urllib.error
    import urllib.request

    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    token = os.getenv("PM_API_TOKEN", "").strip()
    if token:
        # 与 server / MCP 共用同一个环境变量，别让 agent 子命令被自家 token 门禁挡死
        req.add_header("X-API-Key", token)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return cast("dict[str, Any]", json.loads(resp.read().decode("utf-8")))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:300]
        raise RuntimeError(f"HTTP {e.code} {url}: {body}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(
            f"无法连接 server（{url}）：{e.reason}。请先在常驻终端启动：python run_server.py"
        ) from e


def _agent_server(ns: argparse.Namespace) -> str:
    return (
        getattr(ns, "server", None) or os.getenv("PM_SERVER_URL") or "http://127.0.0.1:8080"
    ).rstrip("/")


def _build_agent_parser(cmd: str) -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog=f"run.py {cmd}")
    ap.add_argument(
        "--server", default=None, help="server 地址（默认 PM_SERVER_URL 或 127.0.0.1:8080）"
    )
    if cmd == "submit":
        ap.add_argument("--task", required=True, help="原始需求描述")
        ap.add_argument("--target-model", default="未指定")
        ap.add_argument("--cases", type=int, default=3, help="测试用例数量（1-8）")
        ap.add_argument(
            "--cases-file",
            help='JSON 用例集：[{"input","expected","mode","scenario","hijack_marker"}]',
        )
        ap.add_argument(
            "--assert-mode",
            default="contains",
            type=assert_mode_arg,
            metavar="MODE",
            help="事实断言模式：exact/contains/regex/rule/custom:<已注册名>"
            "（与同步 CLI 的 --assert-mode 同一解析器，白名单不分叉）",
        )
        ap.add_argument("--max-iter", type=int, default=3)
        ap.add_argument(
            "--prompt",
            help="待改进的原稿提示词（你已经写好的那一版）。给定时基线臂换成它，"
            "报告的 Δ 读作「比你自己的版本好多少」",
        )
        ap.add_argument("--prompt-file", help="从文件读取原稿提示词（与 --prompt 互斥）")
    else:
        ap.add_argument("run_id", help="submit 返回的 run_id")
    if cmd == "report":
        ap.add_argument("--out", default=None, help="报告保存路径（不传则打印到 stdout）")
    if cmd == "wait":
        ap.add_argument("--timeout", type=int, default=2400, help="最长等待秒数（默认 2400）")
        ap.add_argument("--interval", type=int, default=15, help="轮询间隔秒数（默认 15）")
    return ap


def agent_subcommand(cmd: str, argv: list[str]) -> int:
    """按子命令分派。每个子命令的实现各占一个函数（自 2026-10-22 拆），
    这里只做"解析 + 路由"——原来四个分支挤在一个函数里，复杂度棘轮已经压在线上。"""
    ap = _build_agent_parser(cmd)
    ns = ap.parse_args(argv)
    base = _agent_server(ns)
    if cmd == "submit":
        return _agent_submit(ap, ns, base)
    if cmd == "status":
        return _agent_status(ns, base)
    if cmd == "report":
        return _agent_report(ap, ns, base)
    return _agent_wait(ns, base)


def _agent_submit(ap: argparse.ArgumentParser, ns: argparse.Namespace, base: str) -> int:
    payload: dict[str, Any] = {
        "task": ns.task,
        "target_model": ns.target_model,
        "n_test_cases": ns.cases,
        "max_iterations": ns.max_iter,
        "assertion_mode": ns.assert_mode if ns.cases_file else "contains",
    }
    # 原稿护栏与同步 CLI 同一个函数：两条入口对 --prompt/--prompt-file 的
    # 互斥、长度上下限必须同数，分叉迟早变成"一边能跑一边白烧钱"
    seed = read_seed_prompt(ap, ns)
    if seed:
        payload["seed_prompt"] = seed
    if ns.cases_file:
        # 与同步 CLI 同一条加载路径：编码兜底、非空数组、JSON 错误都在同一处报
        raw = read_cases_file(ap, ns.cases_file)
        payload["test_cases"] = [
            {
                "input": str(c.get("input") or ""),
                "expected": str(c.get("expected") or ""),
                "assert_mode": str(c.get("mode") or ""),
                "scenario": str(c.get("scenario") or ""),
                "hijack_marker": str(c.get("hijack_marker") or ""),
            }
            for c in raw
            if isinstance(c, dict) and str(c.get("input") or "").strip()
        ]
        payload["n_test_cases"] = len(payload["test_cases"])
    resp = _http_json("POST", f"{base}/api/optimize", payload)
    print(json.dumps(resp, ensure_ascii=False))
    return 0


def _agent_status(ns: argparse.Namespace, base: str) -> int:
    print(
        json.dumps(
            _http_json("GET", f"{base}/api/status/{ns.run_id}"), ensure_ascii=False, default=str
        )
    )
    return 0


def _agent_report(ap: argparse.ArgumentParser, ns: argparse.Namespace, base: str) -> int:
    resp = _http_json("GET", f"{base}/api/report/{ns.run_id}")
    report = str(resp.get("report", ""))
    if getattr(ns, "out", None):
        out = Path(ns.out)
        if out.is_dir():
            ap.error(f"--out 需要文件路径，不是目录：{out}")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(report, encoding="utf-8")
        print(json.dumps({"run_id": ns.run_id, "report_path": ns.out}, ensure_ascii=False))
    else:
        print(json.dumps({"run_id": ns.run_id, "report": report}, ensure_ascii=False))
    return 0


def _agent_wait(ns: argparse.Namespace, base: str) -> int:
    """wait：轮询到终态，输出与 --json 同构的结果 JSON（Agent 只需要学一种消费方式）。"""
    deadline = time.monotonic() + max(30, ns.timeout)
    status_data: dict[str, Any] = {}
    while True:
        status_data = _http_json("GET", f"{base}/api/status/{ns.run_id}", timeout=15.0)
        if status_data.get("status") in _TERMINAL_STATUS:
            break
        if time.monotonic() > deadline:
            print(
                json.dumps(
                    {"run_id": ns.run_id, "status": status_data.get("status"), "error": "等待超时"},
                    ensure_ascii=False,
                )
            )
            return EXIT_FAILED
        time.sleep(max(5, ns.interval))
    final_status = status_data.get("status")
    report = ""
    if final_status in ("passed", "max_iterations", "early_stopped"):
        report = str(
            _http_json("GET", f"{base}/api/report/{ns.run_id}", timeout=30.0).get("report", "")
        )
    print(
        json.dumps(
            {
                "run_id": ns.run_id,
                "status": final_status,
                "aggregate": status_data.get("aggregate"),
                "iterations": status_data.get("iteration"),
                "llm_calls": status_data.get("llm_calls"),
                "error": status_data.get("error"),
                "report": report,
            },
            ensure_ascii=False,
            default=str,
        )
    )
    return _result_exit_code(final_status)
