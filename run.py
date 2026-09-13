#!/usr/bin/env python3
"""PromptMaster CLI —— 提示词自动优化闭环。

用法示例：
    # 1) 无 API Key 也能跑：验证图拓扑与控制流（不验证效果）
    python run.py --selftest

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
import json
import logging
import os
import re
import sys
from pathlib import Path

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

    with usage_scope() as ledger:
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
def main() -> int:
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
            seed_cases.append(
                {
                    "input": str(item["input"]),
                    "expected": str(item.get("expected") or ""),
                    # 每条可自带 mode（"mode" 或 "assert_mode"），没写就用全局 --assert-mode：
                    # 一份用例集里混着写字面片段与需求规则才是常态
                    "mode": _resolve_case_mode(item, args.assert_mode, i, p),
                }
            )
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

    print(f"run_id: {init['run_id']}  目标模型: {args.target_model}")
    final = run_pipeline(args, init)
    log_path, report_path = save_artifacts(final, args.out)

    print("\n" + "=" * 60)
    print(final.get("final_report", "_未生成报告_"))
    print("=" * 60)
    print(f"\n完整运行日志：{log_path}")
    print(f"报告已保存：{report_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
