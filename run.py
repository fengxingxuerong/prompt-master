#!/usr/bin/env python3
"""PromptMaster CLI —— 提示词自动优化闭环。

用法示例：
    # 1) 无 API Key 也能跑：验证图拓扑与控制流（不验证效果）
    python run.py --selftest

    # 1b) 无 Key 走假后端跑完整流程（演示模式，与 server 的 PM_FAKE_BACKEND 一致）
    PM_FAKE_BACKEND=progress python run.py --task "让 AI 分析销售数据"

    # 2) 真实运行
    export PM_API_KEY=sk-xxx
    python run.py --task "让 AI 分析销售数据" --target-model "deepseek-v3"

    # 3) 交互式澄清（需求不清晰时会向你提问）
    python run.py --task "写个 prompt" --interactive

    # 4) 从文件读取需求，输出报告
    python run.py --task-file req.txt --out report.md
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, str(Path(__file__).parent))

from pm.bootstrap import ensure_utf8_stdio  # noqa: E402

# Windows 控制台/重定向编码兜底（L12）：任何打印之前先统一为 UTF-8
ensure_utf8_stdio()

from pm.graph import build_app, mermaid  # noqa: E402
from pm.llm import usage_scope  # noqa: E402
from pm.state import initial_state  # noqa: E402

LOG_DIR = Path(os.getenv("PM_LOG_DIR") or (Path(__file__).parent / "logs"))


def setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)-12s %(message)s",
        datefmt="%H:%M:%S",
    )
    if not verbose:
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        logging.getLogger("openai").setLevel(logging.WARNING)


def _resolve_case_mode(
    item: dict, default_mode: str, index: int, p: argparse.ArgumentParser
) -> str:
    """取这条用例自己的 mode；没写就用全局值。非法值直接报错而不是静默回退。

    静默回退会把一条本意是“规则”的断言变成“字面比对”，后果要到报告里才看得见。
    """
    raw = str(item.get("mode") or item.get("assert_mode") or "").strip()
    if not raw:
        return default_mode
    try:
        return assert_mode_arg(raw)
    except argparse.ArgumentTypeError as e:
        p.error(f"--cases-file 第 {index + 1} 条：{e}")
        return default_mode  # 不可达：p.error 会 SystemExit


def assert_mode_arg(value: str) -> str:
    """--assert-mode 的解析器：除内置三种外，还要能接 `custom:<name>`。

    只能用 argparse 的 choices 时，注册过的自定义断言从 API 能跑、从 CLI 跑不了
    （一个只剩半边入口的功能等于没做）。这里改成显式校验，错误提示也写清楚。
    """
    raw = (value or "").strip()
    if raw in {"exact", "contains", "regex", "rule"}:
        return raw
    if raw.startswith("custom:"):
        name = raw[len("custom:") :].strip()
        if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]*", name):
            raise argparse.ArgumentTypeError(
                f"custom: 后的断言名非法：{name!r}（仅允许字母开头的字母/数字/下划线）"
            )
        return f"custom:{name}"
    raise argparse.ArgumentTypeError(
        f"未知断言模式：{value!r}（可选 exact / contains / regex / rule / custom:<已注册名>）"
    )


def recursion_budget(max_iterations: int) -> int:
    """按迭代上限算递归预算，并给交互澄清留出余量。

    写死 100 时，`--max-iter 30` 会先烧完 30 轮真实计费调用再撞 GraphRecursionError（A5）。
    拓扑每轮固定 3 个 superstep（test/evaluate/revise），外加 clarify 链最多
    5 个（3 次 clarify 分析 + 2 次 ask_user 提问，M8 起「2 轮提问」按提问轮数计）。
    """
    per_run = 3 * (max(0, int(max_iterations)) + 1) + 10
    return max(40, 4 * per_run)


def save_artifacts(state: dict, out: Path | None) -> tuple[Path, Path]:
    LOG_DIR.mkdir(exist_ok=True)
    run_id = state.get("run_id", "unknown")
    log_path = LOG_DIR / f"run_{run_id}.json"
    log_path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )

    report = state.get("final_report") or "_未生成报告_"
    report_path = out or (LOG_DIR / f"report_{run_id}.md")
    if isinstance(report_path, str):
        report_path = Path(report_path)
    report_path.write_text(report, encoding="utf-8")
    return log_path, report_path  # type: ignore[return-value]


def emit_json_result(final: dict, log_path: Path, report_path: Path) -> None:
    """智能体模式的机器可读出口：stdout 恰好一个 JSON 对象，其余信息走文件/日志。

    字段以 Agent 的决策需求为准：状态、分数（含保守下界）、最佳提示词、
    产物路径、遗留问题。聚合缺失（如中途失败）时置 null 而不是编造结构。
    """
    versions = final.get("prompt_versions") or []
    payload = {
        "run_id": final.get("run_id"),
        "status": final.get("status"),
        "aggregate": final.get("aggregate"),
        "iterations": final.get("iteration", 0),
        "max_iterations": final.get("max_iterations"),
        "llm_calls": final.get("llm_calls", 0),
        "best_prompt": final.get("prompt"),
        "early_stop_reason": final.get("early_stop_reason", ""),
        "unresolved_questions": final.get("unresolved_questions") or [],
        "injection_survival": final.get("injection_survival"),
        "errors": final.get("errors") or [],
        "report_path": str(report_path),
        "log_path": str(log_path),
        "n_versions": len(versions),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


# 退出码协议（智能体分支决策的唯一依据，与 argparse 的 usage-error=2 天然对齐）：
#   0 = passed（达标交付）        1 = 未达标但已交付（max_iterations / early_stopped）
#   2 = 参数/配置错误             3 = 运行失败（status=failed 或未捕获异常）
_EXIT_PASSED, EXIT_UNDELIVERED, EXIT_CONFIG, EXIT_FAILED = 0, 1, 2, 3


def _result_exit_code(status: str | None) -> int:
    if status == "passed":
        return _EXIT_PASSED
    if status in ("max_iterations", "early_stopped"):
        return EXIT_UNDELIVERED
    return EXIT_FAILED


# --------------------------------------------------------------------------
# 交互模式：处理 interrupt
# --------------------------------------------------------------------------
def _pending_interrupts(app, config):
    snap = app.get_state(config)
    found = []
    for task in getattr(snap, "tasks", []) or []:
        for it in getattr(task, "interrupts", []) or []:
            found.append(it.value)
    return found


def run_pipeline(args, init: dict) -> dict:
    from langgraph.types import Command

    app = build_app(sqlite_path=args.checkpoint)
    config = {
        "configurable": {"thread_id": args.thread_id or init["run_id"]},
        "recursion_limit": recursion_budget(args.max_iter),
    }

    # 演示模式（与 server/scheduler 同一机制）：PM_FAKE_BACKEND 有效时，
    # 把假后端限定在本次运行的作用域内（ContextVar，不改动任何模块属性，C4）。
    # CLI 之前不支持该变量——无 Key 演示只能走 server；对齐后 CLI 也能跑。
    scenario = os.getenv("PM_FAKE_BACKEND", "").strip()
    hook_cm: Any = contextlib.nullcontext()
    if scenario:
        from pm import testing

        if scenario in testing.SCENARIOS:
            hook_cm = testing.scope(scenario)
        else:
            print(
                f"⚠️ PM_FAKE_BACKEND={scenario!r} 不是可用场景"
                f"（{'|'.join(testing.SCENARIOS)}），本次按真实后端执行"
            )

    with hook_cm, usage_scope() as ledger:
        app.invoke(init, config)

        if not args.interactive:
            final = app.get_state(config).values
        else:
            # 交互模式：不断响应澄清中断，直到流程走完
            rounds = 0
            while rounds < 5:
                interrupts = _pending_interrupts(app, config)
                if not interrupts:
                    break
                payload = interrupts[0]
                print("\n" + "=" * 60)
                print("需要澄清（需求不够清晰）")
                print("=" * 60)
                if isinstance(payload, dict):
                    print(f"任务理解：{payload.get('task_summary', '')}\n")
                    for i, q in enumerate(payload.get("questions", []), 1):
                        print(f"  {i}. {q}")
                print("\n请输入回答（直接回车表示按系统推断继续）：")
                try:
                    answer = input("> ").strip()
                except EOFError:
                    answer = ""
                app.invoke(Command(resume=answer or "按你的推断继续"), config)
                rounds += 1
            final = app.get_state(config).values

    # 台账快照写回 state：报告与 /api/status 直接展示（usage_scope 外已无记账）
    final["llm_usage"] = ledger.snapshot()
    return final


# --------------------------------------------------------------------------
# 自检
# --------------------------------------------------------------------------
def selftest() -> int:
    """拓扑与控制流自检。

    说明：这里用**假后端**替换 LLM 调用，只验证图能正确流转、状态能正确累加、
    评分聚合与迭代终止逻辑正确。**它不验证提示词优化的实际效果**——
    效果必须由配置真实 API Key 后的 e2e 运行来验证。
    """
    from pm import testing
    from pm.nodes import _samples_per_case

    n_samples = _samples_per_case()
    print("=" * 60)
    print("自检模式：验证图拓扑与控制流（不验证优化效果）")
    print("=" * 60)
    print(f"  采样口径：每条用例重复 {n_samples} 次（PM_SAMPLES_PER_CASE）")

    with testing.fake_backend(scenario="progress"):
        app = build_app()
        init = initial_state(
            task="让 AI 分析销售数据",
            target_model="fake-target",
            n_test_cases=3,
            max_iterations=3,
            auto_clarify=True,
        )
        config = {"configurable": {"thread_id": "selftest"}, "recursion_limit": recursion_budget(3)}
        final = app.invoke(init, config)

    checks = [
        ("图执行完成且到达 report", final.get("final_report") != ""),
        ("生成了 3 条测试用例", len(final.get("test_cases", [])) == 3),
        (
            "每条用例按采样数重复执行",
            len(final.get("test_runs", [])) == 3 * n_samples,
        ),
        ("评估产生结果", len(final.get("evaluations", [])) == 3),
        ("trace 已累加（未被覆盖）", len(final.get("trace", [])) >= 6),
        ("触发了修订迭代", final.get("iteration", 0) >= 1),
        ("迭代未超过上限", final.get("iteration", 0) <= 3),
        ("状态为终态", final.get("status") in ("passed", "max_iterations", "failed")),
        ("记录了多个提示词版本", len(final.get("prompt_versions", [])) >= 2),
    ]

    ok = True
    for name, passed in checks:
        mark = "PASS" if passed else "FAIL"
        print(f"  [{mark}] {name}")
        ok = ok and passed

    print("\n关键运行指标：")
    print(f"  iteration   = {final.get('iteration')}")
    print(f"  status      = {final.get('status')}")
    print(f"  llm_calls   = {final.get('llm_calls')}")
    print(f"  trace 条数  = {len(final.get('trace', []))}")
    agg = final.get("aggregate") or {}
    print(f"  avg/min     = {agg.get('avg_score')} / {agg.get('min_score')}")
    print(f"  版本数      = {len(final.get('prompt_versions', []))}")

    print("\n节点执行顺序：")
    for t in final.get("trace", []):
        print(f"  iter{t.get('iteration')} {t.get('node'):<10} {t.get('event')}")

    # ---- 场景二：始终不达标，验证迭代上限兜底 ----
    print("\n" + "=" * 60)
    print("场景二：始终不达标 —— 验证迭代上限后的兜底交付")
    print("=" * 60)
    with testing.fake_backend(scenario="stall"):
        app2 = build_app()
        init2 = initial_state(
            task="让 AI 分析销售数据",
            target_model="fake-target",
            n_test_cases=3,
            max_iterations=2,
            auto_clarify=True,
        )
        cfg2 = {"configurable": {"thread_id": "selftest-stall"}, "recursion_limit": 100}
        final2 = app2.invoke(init2, cfg2)

    stall_checks = [
        ("达到上限后状态为 max_iterations", final2.get("status") == "max_iterations"),
        ("迭代次数被限制在上限内", final2.get("iteration", 0) == 2),
        ("仍然生成了交付报告", bool(final2.get("final_report"))),
        ("报告中如实标注未达标", "未达标" in (final2.get("final_report") or "")),
        ("记录了全部版本", len(final2.get("prompt_versions", [])) == 3),
    ]
    for name, passed in stall_checks:
        mark = "PASS" if passed else "FAIL"
        print(f"  [{mark}] {name}")
        ok = ok and passed

    print(f"\n  iteration={final2.get('iteration')}  status={final2.get('status')}")

    print("\n" + "=" * 60)
    print("场景三：双评委分歧 —— 验证仲裁路径与如实交付")
    print("=" * 60)
    with testing.fake_backend(scenario="dispute"):
        app3 = build_app()
        init3 = initial_state(
            task="分析销售数据",
            target_model="fake-target",
            n_test_cases=2,
            max_iterations=1,
            auto_clarify=True,
        )
        cfg3 = {"configurable": {"thread_id": "selftest-dispute"}, "recursion_limit": 100}
        final3 = app3.invoke(init3, cfg3)

    dispute_evals = [e for e in final3.get("evaluations", []) if e.get("judge") == "arbiter"]
    dispute_checks = [
        (
            "状态为终态",
            final3.get("status") in ("passed", "max_iterations", "failed", "early_stopped"),
        ),
        ("双评委分歧触发了仲裁", len(dispute_evals) > 0),
        (
            "仲裁结果保留了分差信息",
            all(e.get("judge_disagreement") is not None for e in dispute_evals),
        ),
        ("报告如实标注未达标", "未达标" in (final3.get("final_report") or "")),
    ]
    for name, passed in dispute_checks:
        mark = "PASS" if passed else "FAIL"
        print(f"  [{mark}] {name}")
        ok = ok and passed
    print(
        f"\n  仲裁用例数 = {len(dispute_evals)}  分差 = "
        f"{dispute_evals[0].get('judge_disagreement') if dispute_evals else '-'}"
    )

    print("\n" + ("自检通过" if ok else "自检失败"))
    return 0 if ok else 1


# --------------------------------------------------------------------------
# --------------------------------------------------------------------------
# history 子命令：运行历史聚合 + 同任务跨 run 显著性判定
#
# 为什么需要：单轮 Δ 受评委噪声影响（实测基线 ±0.5 抖动），"看起来高了 3 分"
# 不等于"真的变好了"。把历次 run 按 task 聚合，Δ 的均值 ± 1.96×SE 置信区间
# 不含 0 时才敢说显著 —— 把结论从叙事变成统计。
# --------------------------------------------------------------------------
_TERMINAL_STATUS = {
    "passed",
    "max_iterations",
    "failed",
    "early_stopped",
    "needs_clarification",
}


def _load_run_history(last: int, include_demo: bool) -> list[dict[str, Any]]:
    """扫描 logs/run_*.json，按修改时间从新到旧收集，抽出聚合所需的最小字段集。

    全量 pytest 的 fake 跑也会往 logs/ 落 run 文件（数量远多于真实运行）：
    所以 demo 过滤必须发生在取样窗口**之内**逐个进行，而不是先截断再过滤——
    否则最近 N 个文件可能全是测试产物，真实运行反而被挤出窗口。
    """
    from datetime import datetime

    rows: list[dict[str, Any]] = []
    files = sorted(LOG_DIR.glob("run_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for f in files:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue  # 损坏的历史文件跳过，不让一颗坏牙毁掉整份体检
        agg = d.get("aggregate") or {}
        base = d.get("baseline_aggregate") or {}
        channels = {str(e.get("channel")) for e in (d.get("trace") or [])}
        is_demo = "fake" in channels
        if is_demo and not include_demo:
            continue
        opt_avg = agg.get("avg_score")
        base_avg = base.get("avg_score")
        rows.append(
            {
                "run_id": d.get("run_id") or f.stem[4:],
                "mtime": datetime.fromtimestamp(f.stat().st_mtime).strftime("%m-%d %H:%M"),
                "task": str(d.get("task") or "")[:40],
                "status": d.get("status"),
                "opt_avg": opt_avg,
                "opt_lower": agg.get("ci_lower"),
                "base_avg": base_avg,
                "delta": (
                    round(opt_avg - base_avg, 2)
                    if isinstance(opt_avg, (int, float)) and isinstance(base_avg, (int, float))
                    else None
                ),
                "llm_calls": d.get("llm_calls", 0),
                "demo": is_demo,
                "noise": agg.get("noise"),
            }
        )
        if len(rows) >= max(0, last):
            break
    return rows


def _history_command(argv: list[str]) -> int:
    import math

    ap = argparse.ArgumentParser(prog="run.py history")
    ap.add_argument("--last", type=int, default=50, help="只看最近 N 条运行（默认 50）")
    ap.add_argument("--include-demo", action="store_true", help="包含演示模式的运行（默认排除）")
    ap.add_argument("--json", action="store_true", help="输出 JSON（智能体消费）")
    ns = ap.parse_args(argv)

    rows = _load_run_history(ns.last, ns.include_demo)

    # 按 task 分组：组内 ≥2 条且都有基线 → Δ 配对统计（置信区间不含 0 = 显著）
    groups: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        if r["delta"] is not None:
            groups.setdefault(r["task"], []).append(r)

    task_stats: list[dict[str, Any]] = []
    for task, rs in groups.items():
        if len(rs) < 2:
            continue
        deltas = [float(r["delta"]) for r in rs]
        n = len(deltas)
        mean = sum(deltas) / n
        if n >= 2:
            var = sum((x - mean) ** 2 for x in deltas) / (n - 1)
            se = math.sqrt(var / n)
        else:
            se = 0.0
        lo, hi = mean - 1.96 * se, mean + 1.96 * se
        significant = n >= 2 and (lo > 0 or hi < 0)
        task_stats.append(
            {
                "task": task,
                "n_runs": n,
                "deltas": deltas,
                "delta_mean": round(mean, 2),
                "delta_ci95": [round(lo, 2), round(hi, 2)],
                "significant": significant,
                "note": "样本少，结论仅供观察" if n < 5 else None,
                "run_ids": [r["run_id"] for r in rs],
            }
        )
    task_stats.sort(key=lambda s: -s["n_runs"])

    if ns.json:
        print(
            json.dumps(
                {
                    "n_runs": len(rows),
                    "runs": rows,
                    "task_groups": task_stats,
                    "method": "同任务 Δ 的均值 ± 1.96×SE；置信区间不含 0 视为显著（小样本粗判）",
                },
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        )
        return 0

    print(f"最近 {len(rows)} 条运行（演示模式{'已包含' if ns.include_demo else '已排除'}）：\n")
    print(f"{'日期':<12} {'run_id':<14} {'状态':<16} {'基线':>5} {'优化':>5} {'Δ':>6} {'调用':>5}  任务")
    for r in rows:
        ba = f"{r['base_avg']:.2f}" if isinstance(r["base_avg"], (int, float)) else "  -"
        oa = f"{r['opt_avg']:.2f}" if isinstance(r["opt_avg"], (int, float)) else "  -"
        dl = f"{r['delta']:+.2f}" if r["delta"] is not None else "  -"
        print(
            f"{r['mtime']:<12} {r['run_id']:<14} {r['status']!s:<16} {ba:>5} {oa:>5} {dl:>6} {r['llm_calls']:>5}  {r['task']}"
        )

    if task_stats:
        print("\n同任务跨 run 显著性（Δ 均值 ± 95% CI）：")
        for s in task_stats:
            mark = "✅ 显著" if s["significant"] else "⚠️ 不显著"
            note = f"（{s['note']}）" if s["note"] else ""
            print(
                f"  {mark}  Δ均值 {s['delta_mean']:+.2f}  CI [{s['delta_ci95'][0]:+.2f}, {s['delta_ci95'][1]:+.2f}]"
                f"  n={s['n_runs']} {note}｜{s['task']}"
            )
    else:
        print("\n（暂无同任务 ≥2 次的运行——对同一需求多跑几轮，这里会给出 Δ 的显著性判定）")
    return 0


def _http_json(method: str, url: str, payload: dict | None = None, timeout: float = 30.0) -> dict:
    """极简 JSON HTTP 客户端。非 2xx / 连不上都抛 RuntimeError（带可执行建议）。"""
    import urllib.error
    import urllib.request

    data = (
        json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    )
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:300]
        raise RuntimeError(f"HTTP {e.code} {url}: {body}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(
            f"无法连接 server（{url}）：{e.reason}。请先在常驻终端启动：python run_server.py"
        ) from e


def _agent_server(ns: argparse.Namespace) -> str:
    return (
        getattr(ns, "server", None)
        or os.getenv("PM_SERVER_URL")
        or "http://127.0.0.1:8080"
    ).rstrip("/")


def _build_agent_parser(cmd: str) -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog=f"run.py {cmd}")
    ap.add_argument("--server", default=None, help="server 地址（默认 PM_SERVER_URL 或 127.0.0.1:8080）")
    if cmd == "submit":
        ap.add_argument("--task", required=True, help="原始需求描述")
        ap.add_argument("--target-model", default="未指定")
        ap.add_argument("--cases", type=int, default=3, help="测试用例数量（1-8）")
        ap.add_argument("--cases-file", help='JSON 用例集：[{"input","expected","mode","scenario","hijack_marker"}]')
        ap.add_argument("--assert-mode", default="contains", choices=["exact", "contains", "regex", "rule"])
        ap.add_argument("--max-iter", type=int, default=3)
    else:
        ap.add_argument("run_id", help="submit 返回的 run_id")
    if cmd == "report":
        ap.add_argument("--out", default=None, help="报告保存路径（不传则打印到 stdout）")
    if cmd == "wait":
        ap.add_argument("--timeout", type=int, default=2400, help="最长等待秒数（默认 2400）")
        ap.add_argument("--interval", type=int, default=15, help="轮询间隔秒数（默认 15）")
    return ap


def agent_subcommand(cmd: str, argv: list[str]) -> int:
    ns = _build_agent_parser(cmd).parse_args(argv)
    base = _agent_server(ns)

    if cmd == "submit":
        payload: dict[str, Any] = {
            "task": ns.task,
            "target_model": ns.target_model,
            "n_test_cases": ns.cases,
            "max_iterations": ns.max_iter,
            "assertion_mode": ns.assert_mode if ns.cases_file else "contains",
        }
        if ns.cases_file:
            raw = json.loads(Path(ns.cases_file).read_text(encoding="utf-8"))
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

    if cmd == "status":
        print(json.dumps(_http_json("GET", f"{base}/api/status/{ns.run_id}"), ensure_ascii=False, default=str))
        return 0

    if cmd == "report":
        resp = _http_json("GET", f"{base}/api/report/{ns.run_id}")
        report = str(resp.get("report", ""))
        if getattr(ns, "out", None):
            Path(ns.out).write_text(report, encoding="utf-8")
            print(json.dumps({"run_id": ns.run_id, "report_path": ns.out}, ensure_ascii=False))
        else:
            print(json.dumps({"run_id": ns.run_id, "report": report}, ensure_ascii=False))
        return 0

    # wait：轮询到终态，输出与 --json 同构的结果 JSON（Agent 只需要学一种消费方式）
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
        report = str(_http_json("GET", f"{base}/api/report/{ns.run_id}", timeout=30.0).get("report", ""))
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


# --------------------------------------------------------------------------
# calibrate 子命令：评委漂移监测（把 calibrate_judge 的校准能力接进主流程）
#
# 校准回答"评委与人类专家差多远"；漂移对比回答"同一个评委今天和上次比变了没有"。
# 每次校准的结果追加进 logs/judge_calibration_history.json，与上次同角色记录对比：
# bias（系统性偏松/偏严）或 mae（绝对偏差）变化超过 _CALIB_DRIFT_ALERT 即告警。
# --------------------------------------------------------------------------
_CALIB_DRIFT_ALERT = 0.5
_CALIB_HISTORY = LOG_DIR / "judge_calibration_history.json"


def _calibrate_command(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="run.py calibrate")
    ap.add_argument(
        "--judge",
        default="evaluator",
        choices=["evaluator", "evaluator_b", "arbiter"],
        help="要校准的评委角色（默认 evaluator）",
    )
    ap.add_argument(
        "--samples",
        default=str(Path(__file__).parent / "judge_calibration" / "samples.json"),
        help="锚点样本 JSON 路径（需人工核对 human_score）",
    )
    ap.add_argument("--json", action="store_true", help="输出 JSON（智能体消费）")
    ap.add_argument(
        "--no-save", action="store_true", help="本次结果不追加进漂移历史（只看不动账本）"
    )
    ns = ap.parse_args(argv)

    import calibrate_judge as calib

    try:
        samples = calib.load_samples(Path(ns.samples))
    except (OSError, ValueError, json.JSONDecodeError) as e:
        print(f"样本加载失败：{e}", file=sys.stderr)
        return EXIT_CONFIG
    if not samples:
        print(
            f"样本为空：{ns.samples}（先跑 python calibrate_judge.py --write-template 生成模板，"
            "并人工核对 human_score）",
            file=sys.stderr,
        )
        return EXIT_CONFIG

    analysis, errors = calib.calibrate(samples, ns.judge)
    for sid, err in errors:
        print(f"  [SKIP] {sid}：{err}", file=sys.stderr)
    if not analysis:
        print("全部锚点评估失败，无法校准（先跑 run.py --preflight 检查端点）", file=sys.stderr)
        return EXIT_FAILED

    # 漂移对比：与上次**同角色**记录比 bias / mae 的变化量
    history: list[dict[str, Any]] = []
    if _CALIB_HISTORY.exists():
        try:
            history = json.loads(_CALIB_HISTORY.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            history = []  # 历史损坏按空账本处理，本次照常记录
    prev = next(
        (h for h in reversed(history) if h.get("judge") == ns.judge), None
    )
    drift: dict[str, Any] | None = None
    if prev is not None:
        d_bias = round(float(analysis["bias"]) - float(prev["bias"]), 2)
        d_mae = round(float(analysis["mae"]) - float(prev["mae"]), 2)
        drifted = abs(d_bias) >= _CALIB_DRIFT_ALERT or abs(d_mae) >= _CALIB_DRIFT_ALERT
        drift = {
            "prev_ts": prev.get("ts"),
            "prev_bias": prev["bias"],
            "prev_mae": prev["mae"],
            "delta_bias": d_bias,
            "delta_mae": d_mae,
            "drifted": drifted,
            "alert_line": _CALIB_DRIFT_ALERT,
        }

    if not ns.no_save:
        history.append(
            {
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                "judge": ns.judge,
                "n": analysis["n"],
                "bias": analysis["bias"],
                "mae": analysis["mae"],
                "r": analysis["r"],
            }
        )
        _CALIB_HISTORY.parent.mkdir(parents=True, exist_ok=True)
        _CALIB_HISTORY.write_text(
            json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    if ns.json:
        print(
            json.dumps(
                {
                    "judge": ns.judge,
                    "analysis": analysis,
                    "drift": drift,
                    "history_len": len(history) + (0 if ns.no_save else 1),
                    "saved": not ns.no_save,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    print(calib.render_report(ns.judge, analysis))
    if drift:
        mark = "⚠️ 漂移告警" if drift["drifted"] else "✅ 稳定"
        print(
            f"\n[{mark}] 与上次（{drift['prev_ts']}）对比：bias {drift['prev_bias']} → "
            f"{analysis['bias']}（Δ{drift['delta_bias']:+.2f}），mae {drift['prev_mae']} → "
            f"{analysis['mae']}（Δ{drift['delta_mae']:+.2f}）；告警线 ±{_CALIB_DRIFT_ALERT}"
        )
    print(f"校准记录已{'保存' if not ns.no_save else '跳过保存'}：{_CALIB_HISTORY}")
    return 0


# --------------------------------------------------------------------------
# library 子命令：跨任务提示词资产库（记忆层·读侧）
#
# 记忆不是另建一套存储，而是把已经存在的 run 历史"组织成可检索的资产"：
# 每个终态运行的最佳提示词 + 它的分数、用例集与报告路径。检索命中后用
# --export 直接导出成品 —— 让"上次优化过类似需求"从印象变成可查询的事实。
# 终态集合沿用 history 段的 _TERMINAL_STATUS 定义。
# --------------------------------------------------------------------------


def _library_command(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="run.py library")
    ap.add_argument("--query", default=None, help="关键词：匹配任务描述或提示词正文（大小写不敏感）")
    ap.add_argument("--all", action="store_true", help="包含未达标交付的运行（默认只列 passed）")
    ap.add_argument("--json", action="store_true", help="输出 JSON（含提示词全文，智能体消费）")
    ap.add_argument("--export", default=None, metavar="RUN_ID", help="导出指定运行的最佳提示词")
    ap.add_argument("--out", default=None, help="导出路径（默认 logs/exports/<run_id>.prompt.md）")
    ns = ap.parse_args(argv)

    if ns.export:
        target: dict[str, Any] | None = None
        for f in sorted(LOG_DIR.glob("run_*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if d.get("run_id") == ns.export:
                channels = {str(e.get("channel")) for e in (d.get("trace") or [])}
                target = {
                    "run_id": d.get("run_id"),
                    "task": d.get("task"),
                    "prompt": d.get("prompt"),
                    "status": d.get("status"),
                    "avg_score": (d.get("aggregate") or {}).get("avg_score"),
                    "demo": "fake" in channels,
                }
                break
        if not target:
            print(f"找不到运行：{ns.export}", file=sys.stderr)
            return EXIT_CONFIG
        if not (target.get("prompt") or "").strip():
            print(f"运行 {ns.export} 没有可导出的提示词", file=sys.stderr)
            return EXIT_FAILED
        out = Path(ns.out) if ns.out else LOG_DIR / "exports" / f"{ns.export}.prompt.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        header = (
            f"# Prompt 资产（run_id: {target['run_id']}）\n\n"
            f"- 任务：{target.get('task', '')}\n"
            f"- 状态：{target.get('status')}  平均分：{target.get('avg_score')}\n\n"
            f"---\n\n"
        )
        out.write_text(header + str(target["prompt"]), encoding="utf-8")
        print(json.dumps({"exported": str(out), "run_id": ns.export}, ensure_ascii=False))
        return 0

    entries: list[dict[str, Any]] = []
    for f in sorted(LOG_DIR.glob("run_*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        status = d.get("status")
        prompt = str(d.get("prompt") or "")
        if status not in _TERMINAL_STATUS or not prompt.strip():
            continue
        if not ns.all and status != "passed":
            continue
        channels = {str(e.get("channel")) for e in (d.get("trace") or [])}
        agg = d.get("aggregate") or {}
        entry = {
            "run_id": d.get("run_id"),
            "task": str(d.get("task") or "")[:60],
            "status": status,
            "avg_score": agg.get("avg_score"),
            "ci_lower": agg.get("ci_lower"),
            "delta_vs_baseline": (
                round(agg["avg_score"] - (d.get("baseline_aggregate") or {}).get("avg_score"), 2)
                if isinstance(agg.get("avg_score"), (int, float))
                and isinstance((d.get("baseline_aggregate") or {}).get("avg_score"), (int, float))
                else None
            ),
            "llm_calls": d.get("llm_calls", 0),
            "prompt_chars": len(prompt),
            "prompt": prompt,
            "demo": "fake" in channels,
        }
        if ns.query:
            q = ns.query.lower()
            hay = (str(d.get("task") or "") + "\n" + prompt).lower()
            if q not in hay:
                continue
        entries.append(entry)

    if ns.json:
        print(json.dumps({"n": len(entries), "assets": entries}, ensure_ascii=False, indent=2))
        return 0

    if not entries:
        hint = f"（query={ns.query!r}）" if ns.query else ""
        print(f"资产库没有匹配的提示词{hint}。跑几个真实任务后这里会积累起来。")
        return 0

    print(f"提示词资产 {len(entries)} 条{'（含未达标交付，加 --all 才显示）' if not ns.all else ''}：\n")
    for e in entries:
        dl = f"{e['delta_vs_baseline']:+.2f}" if e["delta_vs_baseline"] is not None else "  -"
        print(
            f"  [{e['status']:<16}] {e['avg_score'] if e['avg_score'] is not None else ' -'} 分"
            f"  Δ基线 {dl:>6}  {e['llm_calls']:>3} 次调用  {e['run_id']}"
        )
        print(f"      {e['task']}")
    print("\n导出成品：python run.py library --export <run_id> [--out 路径]")
    return 0


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] in {"submit", "status", "report", "wait"}:
        return agent_subcommand(sys.argv[1], sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] == "history":
        return _history_command(sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] == "calibrate":
        return _calibrate_command(sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] == "library":
        return _library_command(sys.argv[2:])

    p = argparse.ArgumentParser(
        description="PromptMaster —— 提示词自动生成 / 测试 / 评估 / 迭代优化",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--task", help="原始需求描述")
    p.add_argument("--task-file", help="从文件读取需求描述")
    p.add_argument("--context", default="", help="补充上下文")
    p.add_argument(
        "--target-model", default="未指定", help="提示词最终运行的目标模型，影响优化策略"
    )
    p.add_argument("--cases", type=int, default=3, help="测试用例数量（默认 3）")
    p.add_argument(
        "--cases-file",
        help='JSON 测试集：[{"input": "...", "expected": "..."}]。'
        "提供后不再让模型生成用例，expected 参与事实断言（ground-truth 一票否决）",
    )
    p.add_argument(
        "--assert-mode",
        default="contains",
        type=assert_mode_arg,
        metavar="MODE",
        help="事实断言模式（配合 --cases-file 的 expected，默认 contains；"
        "rule = 把 expected 当需求规则交给评委逐条核验，不做字面比对；"
        '单条用例可用 "mode" 覆盖此默认值；'
        "可选 exact/contains/regex/rule/custom:<已注册名>）",
    )
    p.add_argument(
        "--samples",
        type=int,
        default=None,
        help="每条用例重复采样次数（默认 2；1 = 关闭重复采样，快但噪声大，上限 5）",
    )
    p.add_argument(
        "--no-baseline",
        action="store_true",
        help="不跑基线（省一半调用，但无法回答‘比不优化好多少’）",
    )
    p.add_argument("--no-pairwise", action="store_true", help="不跑成对盲评")
    p.add_argument("--max-iter", type=int, default=3, help="最大修订轮次（默认 3）")
    p.add_argument(
        "--fast",
        action="store_true",
        help="快速档：关基线、关盲评、单采样、1 轮修订（约 8-10 次调用 / 数分钟）。"
        "只回答‘这版能不能用’，不回答‘比不优化好多少’；"
        "显式传 --samples/--max-iter 时以显式值为准",
    )
    p.add_argument("--interactive", action="store_true", help="需求不清晰时向用户提问")
    p.add_argument(
        "--json",
        action="store_true",
        help="智能体模式：stdout 只输出一个结果 JSON（日志仍在 stderr），"
        "配合退出码协议消费：0=达标交付，1=未达标但已交付，"
        "2=参数/配置错误，3=运行失败。与 --interactive 互斥",
    )
    p.add_argument("--checkpoint", default=None, help="SQLite 检查点路径（可选，支持跨进程恢复）")
    p.add_argument("--thread-id", default=None, help="恢复指定线程的会话")
    p.add_argument("--out", default=None, help="报告输出路径")
    p.add_argument("--mermaid", action="store_true", help="打印图结构并退出")
    p.add_argument("--selftest", action="store_true", help="拓扑与控制流自检，不需要 API Key")
    p.add_argument(
        "--preflight",
        action="store_true",
        help="端点预检：逐角色冒烟调用 + 脱敏配置摘要（花大钱之前先确认端点活着）",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="成本预估：不联网不调用，按当前参数估算 LLM 调用次数区间（JSON 输出）。"
        "智能体可在提交前用它做预算决策",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    # 测量层开关走环境变量：与服务端的配置口径保持一致（API 无 CLI 参数）
    # 顺序很重要：先落 --fast 的默认，再让显式参数覆盖它（显式优先）
    if args.fast:
        os.environ.setdefault("PM_SAMPLES_PER_CASE", "1")
        os.environ["PM_BASELINE"] = "0"
        os.environ["PM_PAIRWISE"] = "0"
        if args.max_iter == p.get_default("max_iter"):
            args.max_iter = 1
    if args.samples is not None:
        os.environ["PM_SAMPLES_PER_CASE"] = str(args.samples)
    if args.no_baseline:
        os.environ["PM_BASELINE"] = "0"
    if args.no_pairwise:
        os.environ["PM_PAIRWISE"] = "0"

    # CLI 也要有边界（API 侧已有 ge/le，而 CLI 没有）：避免 --cases 0/负数 静默降级、
    # --max-iter 过大先烧钱再崩（A5）。
    if not 1 <= args.cases <= 8:
        p.error(f"--cases 需在 1-8 之间（当前 {args.cases}）")
    if not 0 <= args.max_iter <= 10:
        p.error(f"--max-iter 需在 0-10 之间（当前 {args.max_iter}）")

    setup_logging(args.verbose)

    if args.mermaid:
        print(mermaid())
        return 0

    if args.json and args.interactive:
        p.error("--json（智能体模式）与 --interactive（人类交互澄清）互斥；"
                "Agent 场景请直接用 auto_clarify，澄清假设会写进报告的「需求侧遗留问题」")

    if args.selftest:
        return selftest()

    if args.preflight:
        from pm.preflight import render_preflight, run_preflight

        report = run_preflight()
        print(render_preflight(report))
        return 0 if report.all_ok else 1

    if not args.task and not args.task_file:
        p.error("需要 --task / --task-file，或用 --selftest 进行自检")

    task = args.task
    if args.task_file:
        task = Path(args.task_file).read_text(encoding="utf-8").strip()
    if not task:
        p.error("需求描述为空")
    # 与 API 的 OptimizeRequest 同口径（4-8000）：CLI 是另一个烧钱入口，
    # 没有长度护栏时一次粘贴几万字就能直冲 LLM，成本护栏形同虚设（2026-09-13 测试发现）
    if len(task) < 4 or len(task) > 8000:
        p.error(f"需求描述长度需在 4-8000 字之间（当前 {len(task)} 字）")

    seed_cases: list[dict] = []
    if args.cases_file:
        try:
            raw = json.loads(Path(args.cases_file).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            p.error(f"--cases-file 读取失败：{e}")
        if not isinstance(raw, list) or not raw:
            p.error("--cases-file 需为非空 JSON 数组")
        for i, item in enumerate(raw):
            if isinstance(item, str):
                item = {"input": item}
            if not isinstance(item, dict) or not str(item.get("input") or "").strip():
                p.error(f"--cases-file 第 {i + 1} 条缺少非空 input")
            if len(seed_cases) >= 8:
                # 不能默默丢掉多余用例：n_test_cases 按声明条数做完整性校验（C1），
                # 静默截断会让本轮永远无法判达标，且原因藏在报告角落里
                p.error(f"--cases-file 最多 8 条用例，当前 {len(raw)} 条；请筛选后再跑")
            case: dict[str, Any] = {
                "input": str(item["input"]),
                "expected": str(item.get("expected") or ""),
                # 每条可自带 mode（"mode" 或 "assert_mode"），没写就用全局 --assert-mode：
                # 一份用例集里混着写字面片段与需求规则才是常态
                "mode": _resolve_case_mode(item, args.assert_mode, i, p),
            }
            # 注入存活专项（确定性校验，不经评委）的可选字段：seed 携带时透传给
            # mock_node → case_scenarios / hijack_markers，与 mockgen 路径同口径
            for _opt in ("scenario", "hijack_marker"):
                if str(item.get(_opt) or "").strip():
                    case[_opt] = str(item[_opt]).strip()
            seed_cases.append(case)
        # 用例数以用户提供为准（断言按序号与 expected 对齐）
        args.cases = len(seed_cases)

        # 预检：contains/exact 比的是字面片段，而人写的 expected 常常是需求规则。
        # 那种写法永远命不中，只会把基线与优化版一起打死 —— 与其跑完 20 次计费调用
        # 再看报告，不如现在就拦住（真实跑踩过一次，见 qa_report 第十二节）。
        from pm.assertions import looks_like_rule

        rule_like = [
            i + 1
            for i, c in enumerate(seed_cases)
            if c["mode"] in {"contains", "exact"} and looks_like_rule(c.get("expected", ""))
        ]
        if rule_like:
            p.error(
                f"--cases-file 第 {', '.join(map(str, rule_like))} 条的 expected 读起来是需求规则而不是"
                "字面片段，contains/exact 永远命不中。二选一："
                '① 改成输出里真会出现的一段字（如 "未提供：订单号"）；'
                '② 给这几条加 "mode": "rule"（或整体 --assert-mode rule），'
                "交给评委逐条核验（默认只提醒不否决，需要硬约束再设 PM_RULE_VETO=1）。"
            )

    if args.dry_run:
        from pm.nodes import _active_judges, _samples_per_case

        k = _samples_per_case()
        judges = len(_active_judges())
        n = args.cases
        baseline_on = os.getenv("PM_BASELINE") != "0"
        pairwise_on = os.getenv("PM_PAIRWISE") != "0"
        # 首轮：clarify + optimize + mockgen(无 seed 时) + target(n×k) + 评估(n×k×judges)
        base = 2 + (0 if seed_cases else 1) + n * k + n * k * judges
        # 每轮修订：revise 1 次 + 复用测试集重新 target + 重新评估
        per_iter = 1 + n * k + n * k * judges
        est_min = base + (n * k + n * k * judges + n if baseline_on else 0) + (n if pairwise_on else 0)
        est_max = est_min + args.max_iter * per_iter
        print(
            json.dumps(
                {
                    "mode": "dry_run",
                    "cases": n,
                    "samples": k,
                    "judges": judges,
                    "max_iterations": args.max_iter,
                    "baseline_enabled": baseline_on,
                    "pairwise_enabled": pairwise_on,
                    "estimated_llm_calls": {"min": est_min, "max": est_max},
                    "notes": "下界=首轮即达标；上界=跑满 max_iter 轮修订。"
                    "质量门重试与双评委仲裁会向上浮动；缓存命中会向下浮动",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    if not os.getenv("PM_API_KEY") and not os.getenv("PM_TARGET_API_KEY"):
        print(
            "错误：未检测到 PM_API_KEY。\n"
            "请复制 .env.example 为 .env 并填写，或直接 export PM_API_KEY=...\n"
            "若只想验证代码能否跑通，请用 python run.py --selftest",
            file=sys.stderr,
        )
        return 2

    init = initial_state(
        task=task,
        context=args.context,
        target_model=args.target_model,
        n_test_cases=args.cases,
        max_iterations=args.max_iter,
        auto_clarify=not args.interactive,
        seed_cases=seed_cases or None,
        assertion_mode=args.assert_mode if seed_cases else "",
    )

    if not args.json:
        print(f"run_id: {init['run_id']}  目标模型: {args.target_model}")
    try:
        final = run_pipeline(args, init)
        log_path, report_path = save_artifacts(final, args.out)
    except Exception as e:
        if args.json:
            print(
                json.dumps(
                    {
                        "run_id": init["run_id"],
                        "status": "failed",
                        "error": f"{type(e).__name__}: {e}",
                    },
                    ensure_ascii=False,
                )
            )
            return EXIT_FAILED
        raise

    status = final.get("status")
    if args.json:
        emit_json_result(final, log_path, report_path)
    else:
        print("\n" + "=" * 60)
        print(final.get("final_report", "_未生成报告_"))
        print("=" * 60)
        print(f"\n完整运行日志：{log_path}")
        print(f"报告已保存：{report_path}")
    return _result_exit_code(status)


if __name__ == "__main__":
    sys.exit(main())
