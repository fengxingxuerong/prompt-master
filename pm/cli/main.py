"""main()：主参数解析、--fast/--samples 落 env、护栏、dry-run 预算与同步运行分发。（自 run.py 拆出，逻辑逐字保留）"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

from pm.state import initial_state

from .agent_mode import agent_subcommand
from .calibrate import _calibrate_command
from .check import check_command
from .diffcmd import diff_command
from .gatecmd import gate_command
from .history import _history_command
from .library import _library_command
from .live_lock import LiveRunBusy
from .live_lock import enabled as _live_lock_enabled
from .live_lock import hold as _live_lock_hold
from .live_lock import lock_path as _live_lock_path
from .live_lock import wait_seconds as _live_lock_wait
from .support import (
    EXIT_CONFIG,
    EXIT_FAILED,
    _resolve_case_mode,
    _result_exit_code,
    assert_mode_arg,
    emit_json_result,
    read_seed_prompt,
    save_artifacts,
    setup_logging,
)

# 串行锁的"我挂上了"这条证据走日志，不打扰 stdout 的结果契约
logger = logging.getLogger("pm.cli.main")

# 子命令清单：argparse 之前按裸字符串分发（这些参数与 --task 那一套互斥），
# 所以 --help 必须自己把它们列出来 —— 否则文档写着"学 --help 就会用"，
# 而 --help 里一个子命令都看不见（2026-09-25 实测：grep 计数 0）。
# 命令名用 {prog} 而不是写死 `run.py`：装包后的入口叫 `prompt-master`，
# 提示语指向一个不存在的命令，正是本仓库一直在修的"文档与实际分叉"那一类。
_SUBCOMMAND_HELP = """
子命令（写在最前面；各自完整参数看 `{prog} <子命令> --help`）：
  submit      提交异步任务到常驻 server（{prog_server}），秒回 run_id
              —— 一轮真实优化要 40~80 次调用 / 10~30 分钟，长任务请走这条别占终端
  status      查某个 run_id 的进度：status / iteration / aggregate / llm_calls
  report      取该任务的交付报告全文（--out 落盘，缺省打印 JSON）
  wait        阻塞轮询到终态，输出与 `--json` 同构的结果 JSON（Agent 只学一种格式）
  history     运行历史 + 相对基线的 Δ 显著性（--last N / --include-demo / --json）
  calibrate   评委校准：评委分 vs 锚点人工分，--repeat N 测评委自我复现性
  library     达标提示词资产库（--recommend --task-text 找参考 / --export 导出）
  check       零调用静态体检：拿规则闸当场量你已有的那一版提示词（--prompt-file / --json）
  diff        零调用差分：两版提示词（或两个 run）之间改了什么、修好几条、又新引入几条
              —— 退出码 1 = 有新引入的规则问题，可直接当 CI 回归门禁
  gate        提示词改动的 CI 回归门禁：自动从 git 取基线，只拦本次「新引入」的规则问题
              —— 退出码 1 = 有新引入（CI 该红）；2 = 门禁自己没跑起来；默认对象 pm/prompts.py

本地无 Key 路径：
  {prog} --selftest                               图拓扑与控制流自检（秒级）
  PM_FAKE_BACKEND=progress {prog} --task "..."   假后端跑完整流程（不证明效果）

退出码协议（--json / 子命令共用）：0=达标交付 1=未达标但已交付 2=参数或配置错误 3=运行失败
"""

_SUBCOMMANDS = (
    "submit",
    "status",
    "report",
    "wait",
    "history",
    "calibrate",
    "library",
    "check",
    "diff",
    "gate",
)


def _self_prog() -> str:
    """当前入口的名字（仓库里是 `run.py`，装包后是 `prompt-master`）。"""
    return Path(sys.argv[0]).name or "run.py"


def _subcommand_help(prog: str) -> str:
    # `run_server.py` 不是 console 脚本（包里只有 prompt-master 这一个入口），
    # 所以起服务的提示按入口分别给，别让人去敲一个不存在的命令。
    return _SUBCOMMAND_HELP.format(
        prog=prog,
        prog_server="python run_server.py" if prog == "run.py" else "python -m pm.server",
    )


def _dispatch_subcommand(cmd: str, argv: list[str]) -> int:
    """子命令分发（保持原先"argparse 之前裸字符串匹配"的结构，只是收成一个函数）。"""
    if cmd in {"submit", "status", "report", "wait"}:
        return agent_subcommand(cmd, argv)
    if cmd == "history":
        return _history_command(argv)
    if cmd == "calibrate":
        return _calibrate_command(argv)
    if cmd == "check":
        return check_command(argv)
    if cmd == "diff":
        return diff_command(argv)
    if cmd == "gate":
        return gate_command(argv)
    return _library_command(argv)


def main() -> int:
    prog = _self_prog()
    if len(sys.argv) > 1 and not sys.argv[1].startswith("-"):
        if sys.argv[1] in _SUBCOMMANDS:
            return _dispatch_subcommand(sys.argv[1], sys.argv[2:])
        # 打错子命令时别只丢一句 argparse 的 "unrecognized arguments"：把清单当场列出来
        print(f"未知子命令：{sys.argv[1]!r}\n{_subcommand_help(prog)}", file=sys.stderr)
        return EXIT_CONFIG

    p = _build_parser(prog)
    args = p.parse_args()
    _apply_measurement_flags(p, args)

    # 下面每一段的**先后顺序是对外契约**：同一组参数下"先报哪个错"决定退出码与文案。
    # 原先这里是 346 行、圈复杂度 44 的一条直线，动一段要通读全文——所以拆成了函数，
    # 但每段内部的判定顺序逐字保持（含 --cases-file 的三层校验先后）。
    if args.mermaid:
        # 图结构依赖 pm.graph（langchain 链），延迟到真正要用时再 import，
        # 让 --help / --dry-run 等轻路径保持 ~0.2s 启动
        from pm.graph import mermaid

        print(mermaid())
        return 0

    if args.json and args.interactive:
        p.error(
            "--json（智能体模式）与 --interactive（人类交互澄清）互斥；"
            "Agent 场景请直接用 auto_clarify，澄清假设会写进报告的「需求侧遗留问题」"
        )

    if args.selftest:
        from .selftest import selftest  # selftest 依赖 pm.testing（重），同样延迟

        return selftest()

    if args.preflight:
        return _preflight_exit()

    task = _require_task(p, args)
    seed_prompt = read_seed_prompt(p, args)
    if seed_prompt and not args.json:
        print(
            f"原稿改进模式：收到原稿 {len(seed_prompt)} 字符，"
            "基线臂 = 你的原稿，报告里的 Δ 读作「比你自己的版本好多少」",
            file=sys.stderr,
        )

    seed_cases = _load_seed_cases(p, args)

    if args.dry_run:
        return _dry_run_exit(args, seed_cases)

    if not _api_key_gate(p):
        return EXIT_CONFIG

    init = initial_state(
        task=task,
        context=args.context,
        target_model=args.target_model,
        n_test_cases=args.cases,
        max_iterations=args.max_iter,
        auto_clarify=not args.interactive,
        seed_cases=seed_cases or None,
        assertion_mode=args.assert_mode if seed_cases else "",
        seed_prompt=seed_prompt,
    )

    return _execute_run_serialized(args, init)


def _execute_run_serialized(args: argparse.Namespace, init: dict[str, Any]) -> int:
    """`PM_LIVE_LOCK` 开着时，整轮跑在跨进程锁里；被别人占着就退 2。

    缺省关闭（`live_lock.enabled()`），所以产品侧的并发行为与这行代码之前完全一致。
    要串起来的是"一轮 E2E"，不是"一次调用"——理由见 `pm/cli/live_lock.py` 的模块说明。
    """
    if not _live_lock_enabled():
        return _execute_run(args, init)
    try:
        with _live_lock_hold(timeout=_live_lock_wait()) as pid:
            # 挂上了就要说得出证据：2026-10-05 实测过一次"启动器没把 PM_LIVE_LOCK 传进子进程"，
            # 那一轮从头到尾是裸奔的，而日志里没有任何一行能看出没上锁。
            # 静默的自我保护等于没有保护，所以这条走 stderr（`--json` 的 stdout 契约不动）。
            logger.info(
                "串行锁已持有：%s（PID %s）；要并跑请显式取消 PM_LIVE_LOCK", _live_lock_path(), pid
            )
            return _execute_run(args, init)
    except LiveRunBusy as e:
        return _live_busy_exit(args, str(e))


def _live_busy_exit(args: argparse.Namespace, msg: str) -> int:
    """占用中的两种出口：`--json` 必须仍然只打一个 JSON 对象（README 的约定）。"""
    if args.json:
        print(json.dumps({"status": "busy", "error": msg}, ensure_ascii=False))
    else:
        print(f"实时轮次被占用：{msg}\n（锁文件：{_live_lock_path()}）", file=sys.stderr)
    return EXIT_CONFIG


def _execute_run(args: argparse.Namespace, init: dict[str, Any]) -> int:
    """真实跑一轮并落盘：失败时按 `--json` 决定"打结果 JSON"还是把异常抛出去。"""
    if not args.json:
        print(f"run_id: {init['run_id']}  目标模型: {args.target_model}")
    from .pipeline import run_pipeline  # 真实流程入口：此时 langchain 链才被拉起

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

    _print_run_outcome(args, final, log_path, report_path)
    return _result_exit_code(final.get("status"))


# ---------------------------------------------------------------------------
# 参数表与环境开关
# ---------------------------------------------------------------------------


def _build_parser(prog: str) -> argparse.ArgumentParser:
    """主入口的参数表（不含子命令：那些在 argparse 之前按裸字符串分发）。"""
    p = argparse.ArgumentParser(
        description="PromptMaster —— 提示词自动生成 / 测试 / 评估 / 迭代优化",
        epilog=_subcommand_help(prog),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--task", help="原始需求描述")
    p.add_argument("--task-file", help="从文件读取需求描述")
    p.add_argument(
        "--prompt",
        help="待改进的原稿提示词（你自己已经写好的那一版）。给定时不再从零生成："
        "改走改进分支（保留原稿术语与结构、只做最小改动），"
        "且基线臂自动换成这份原稿——报告里的 Δ 读作「比你自己的版本好多少」。"
        "需求描述仍必须给（--task）：用例只按需求命题，原稿不参与出题，否则考卷会偏袒原稿",
    )
    p.add_argument("--prompt-file", help="从文件读取待改进的原稿提示词（与 --prompt 互斥）")
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
    return p


def _apply_measurement_flags(p: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """--fast/--samples/--no-* 落到环境变量，然后做数值护栏，最后配日志。"""
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


# ---------------------------------------------------------------------------
# 单发模式与输入读取
# ---------------------------------------------------------------------------


def _preflight_exit() -> int:
    from pm.preflight import render_preflight, run_preflight

    report = run_preflight()
    print(render_preflight(report))
    return 0 if report.all_ok else 1


def _require_task(p: argparse.ArgumentParser, args: argparse.Namespace) -> str:
    if not args.task and not args.task_file:
        p.error("需要 --task / --task-file，或用 --selftest 进行自检")
    task: str = args.task
    if args.task_file:
        task = Path(args.task_file).read_text(encoding="utf-8").strip()
    if not task:
        p.error("需求描述为空")
    # 与 API 的 OptimizeRequest 同口径（4-8000）：CLI 是另一个烧钱入口，
    # 没有长度护栏时一次粘贴几万字就能直冲 LLM，成本护栏形同虚设（2026-09-13 测试发现）
    if len(task) < 4 or len(task) > 8000:
        p.error(f"需求描述长度需在 4-8000 字之间（当前 {len(task)} 字）")
    return task


def _load_seed_cases(p: argparse.ArgumentParser, args: argparse.Namespace) -> list[dict[str, Any]]:
    """--cases-file：读入 → 逐条成案 → 再预检一次 expected 的写法。没给就是空表。"""
    if not args.cases_file:
        return []
    raw = _read_cases_file(p, args.cases_file)
    seed_cases: list[dict[str, Any]] = []
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

    _reject_rule_like_expected(p, seed_cases)
    return seed_cases


def _read_cases_file(p: argparse.ArgumentParser, path: str) -> list[Any]:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        p.error(f"--cases-file 读取失败：{e}")
    if not isinstance(raw, list) or not raw:
        p.error("--cases-file 需为非空 JSON 数组")
    items: list[Any] = raw
    return items


def _reject_rule_like_expected(
    p: argparse.ArgumentParser, seed_cases: list[dict[str, Any]]
) -> None:
    """expected 写成需求规则、mode 却是 contains/exact 时当场拦住。

    预检：contains/exact 比的是字面片段，而人写的 expected 常常是需求规则。
    那种写法永远命不中，只会把基线与优化版一起打死 —— 与其跑完 20 次计费调用
    再看报告，不如现在就拦住（真实跑踩过一次，见 qa_report 第十二节）。
    """
    from pm.assertions import looks_like_rule

    rule_like = [
        i + 1
        for i, c in enumerate(seed_cases)
        if c["mode"] in {"contains", "exact"} and looks_like_rule(c.get("expected", ""))
    ]
    if not rule_like:
        return
    p.error(
        f"--cases-file 第 {', '.join(map(str, rule_like))} 条的 expected 读起来是需求规则而不是"
        "字面片段，contains/exact 永远命不中。二选一："
        '① 改成输出里真会出现的一段字（如 "未提供：订单号"）；'
        '② 给这几条加 "mode": "rule"（或整体 --assert-mode rule），'
        "交给评委逐条核验（默认只提醒不否决，需要硬约束再设 PM_RULE_VETO=1）。"
    )


# ---------------------------------------------------------------------------
# --dry-run：不联网的调用数与金额预估
# ---------------------------------------------------------------------------


def _dry_run_exit(args: argparse.Namespace, seed_cases: list[dict[str, Any]]) -> int:
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
    payload: dict[str, Any] = {
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
    }
    _attach_estimated_cost(payload, est_min, est_max)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _last_run_usage() -> tuple[dict[str, Any] | None, str]:
    """最近一次真实运行的用量台账与它的 run_id；读不到就是 (None, "")。

    外推只是增强信息，绝不允许因为它让 --dry-run 报错。原先这段和预估主体挤在
    一起、由一个覆盖整段的裸 `except Exception` 兜着；现在异常只吞在这一小段里，
    边界比原先窄。
    """
    try:
        from pm.cli.history import _load_run_history

        rows = _load_run_history(1, include_demo=False)
        if not rows:
            return None, ""
        from pm.cli.support import log_dir

        run_id = str(rows[0]["run_id"])
        f = log_dir() / f"run_{run_id}.json"
        if not f.exists():
            return None, run_id
        usage: dict[str, Any] | None = json.loads(f.read_text(encoding="utf-8")).get("llm_usage")
        return usage, run_id
    except Exception:  # noqa: BLE001 - 历史读不到就不给金额，绝不因此报错
        return None, ""


def _attach_estimated_cost(payload: dict[str, Any], est_min: int, est_max: int) -> None:
    """金额外推：只在配了单价且有历史台账时出现。

    口径是"最近一次运行实测的逐角色均价"，比任何内置价目表准
    （价目表会过期，均价来自刚跑完的那一轮）。没配单价时整段不出现——
    本系统不内置价目表，"没有数"比"看起来很合理的假数"安全。
    """
    from pm.cost import compute_cost, project_cost

    last_usage, run_id = _last_run_usage()
    if not last_usage:
        return
    cost = compute_cost(last_usage)
    if not cost:
        return
    calls = sum(int(v.get("calls", 0) or 0) for v in last_usage.values())
    if not calls:
        return
    proj = project_cost(cost["total"] / calls, {"min": est_min, "max": est_max})
    if not proj:
        return
    payload["estimated_cost"] = proj
    payload["estimated_cost"]["basis"] = f"按最近一次运行（{run_id}）的实测逐角色均价外推"


# ---------------------------------------------------------------------------
# 真实运行
# ---------------------------------------------------------------------------


def _api_key_gate(p: argparse.ArgumentParser) -> bool:
    """True = 可以继续（有 Key，或假后端演示模式）；False = 已打印指引，退出码 2。"""
    if os.getenv("PM_API_KEY") or os.getenv("PM_TARGET_API_KEY"):
        return True
    # 假后端场景下整轮不出网，闸门不该拦 —— 这正是 run.py 文档承诺的无 Key 入门路径。
    # 口径与 pipeline 的钩子装配同源（active_scenario），非法场景名两边都不算演示。
    from pm.testing import active_scenario  # 延迟导入：--help 轻路径不拉起 schemas

    if not active_scenario():
        print(
            "错误：未检测到 PM_API_KEY。\n"
            "请复制 .env.example 为 .env 并填写，或直接 export PM_API_KEY=...\n"
            "无 Key 时的两条本地路径：\n"
            f"  {p.prog} --selftest"
            "                            只验证图拓扑与控制流（秒级）\n"
            f'  PM_FAKE_BACKEND=progress {p.prog} --task "..."     假后端跑完整流程'
            "（只验证链路，不证明优化效果）",
            file=sys.stderr,
        )
        return False
    print(
        "演示模式：PM_FAKE_BACKEND 假后端，本次不调用任何真实端点（只验证链路，不证明优化效果）",
        file=sys.stderr,
    )
    return True


def _print_run_outcome(
    args: argparse.Namespace, final: dict[str, Any], log_path: Path, report_path: Path
) -> None:
    """跑完之后的两种出口：智能体模式只吐一个 JSON，人类模式打报告全文。"""
    if args.json:
        emit_json_result(final, log_path, report_path)
        return
    print("\n" + "=" * 60)
    print(final.get("final_report", "_未生成报告_"))
    print("=" * 60)
    print(f"\n完整运行日志：{log_path}")
    print(f"报告已保存：{report_path}")
