"""main()：主参数解析、--fast/--samples 落 env、护栏、dry-run 预算与同步运行分发。（自 run.py 拆出，逻辑逐字保留）"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

from pm import __version__
from pm.casesaudit import DEFAULT_MODE
from pm.keycheck import KEY_ROLES as _KEY_ROLES
from pm.keycheck import missing_key_roles as _missing_key_roles
from pm.state import initial_state
from pm.textio import TextDecodeError, read_text_auto, warn_if_fallback

from .agent_mode import agent_subcommand
from .calibrate import _calibrate_command
from .check import check_command
from .diffcmd import diff_command
from .doctorcmd import doctor_command
from .gatecmd import gate_command
from .history import _history_command
from .library import _library_command
from .live_lock import LiveRunBusy
from .live_lock import enabled as _live_lock_enabled
from .live_lock import hold as _live_lock_hold
from .live_lock import lock_path as _live_lock_path
from .live_lock import wait_seconds as _live_lock_wait
from .support import (
    CASES_FILE_HINT,
    EXIT_CONFIG,
    EXIT_FAILED,
    MAX_SEED_CASES,
    _result_exit_code,
    assert_mode_arg,
    case_entry_fields,
    emit_json_result,
    read_cases_file,
    read_seed_prompt,
    resolve_case_mode,
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
  check       零调用静态体检：量你已有的那一版提示词，或数一份用例集有几票能否决（--prompt-file / --cases-file / --json）
  diff        零调用差分：两版提示词（或两个 run）之间改了什么、修好几条、又新引入几条
              —— 退出码 1 = 有新引入的规则问题，可直接当 CI 回归门禁
  gate        提示词改动的 CI 回归门禁：自动从 git 取基线，只拦本次「新引入」的规则问题
              —— 退出码 1 = 有新引入（CI 该红）；2 = 门禁自己没跑起来；默认对象 pm/prompts.py
  doctor      环境诊断（零 LLM 调用）：Python / .env / Key / 角色配置 / 日志目录 / 端口 /
              常驻服务 / MCP 一次说清 —— 退出码 0 无问题 / 1 只有警告 / 2 有错误
  panel       本地插件面板（浏览器小窗口）：体检 / 考卷有几票 / 两版差分三张卡零调用可用，
              ④优化闭环那张要 Key —— 没 Key 就置灰并写明原因
              —— 缺省只绑 127.0.0.1:8765（与常驻服务的 8080 分开），--no-browser 只起服务
  desktop     原生桌面窗口（Tkinter，标准库）：给小白的一条路 —— 选项目文件夹 + 写目标
              （可贴已有提示词）→ 只读扫描代码与 Git 进度 → 一次模型分析 → 可复制的提示词
              —— 无 Key 也能“只读取项目”；不开浏览器、不起服务；不修改用户项目
              —— 考卷/差分/账本等集成方面只在 CLI / REST / MCP

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
    "doctor",
    "panel",
    "desktop",
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


def _dispatch_agent(cmd: str, argv: list[str]) -> int:
    """submit / status / report / wait：失败的 RuntimeError 按退出码协议翻成 JSON。"""
    try:
        return agent_subcommand(cmd, argv)
    except RuntimeError as e:
        # 子命令共用退出码协议：4xx/连不上=2，5xx=3；输出仍是单个 JSON
        print(json.dumps({"status": "failed", "error": str(e)}, ensure_ascii=False))
        return EXIT_FAILED if "HTTP 5" in str(e) else EXIT_CONFIG


def _dispatch_lazy(cmd: str, argv: list[str]) -> int:
    """面板/桌面要延迟 import（uvicorn / Tkinter mainloop）；其余（含未知）落到 library。

    保持"调用时才 import"：`--help` 与所有零调用子命令都不该为这两个变重。
    """
    if cmd == "panel":
        from .panelcmd import panel_command

        return panel_command(argv)
    if cmd == "desktop":
        # 同样延迟：Tkinter 是标准库，但窗口一旦建起来就该由 mainloop 独占主线程
        from pm.desktop import app_main

        return app_main(argv)
    return _library_command(argv)


def _dispatch_subcommand(cmd: str, argv: list[str]) -> int:
    """子命令分发（保持原先"argparse 之前裸字符串匹配"的结构，只是收成一个函数）。

    拆成三个函数是复杂度棘轮逼出来的（2026-10-23 加 doctor 时 CC 顶到 11）：
    分发器每加一个子命令就 +1 分支，而面板/桌面那两支还要延迟 import。
    名字在**调用时**解析（不是提前塞进表里），所以 `test_dispatch_routes_every_subcommand`
    的 monkeypatch 语义不变。
    """
    if cmd in {"submit", "status", "report", "wait"}:
        return _dispatch_agent(cmd, argv)
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
    if cmd == "doctor":
        return doctor_command(argv)
    return _dispatch_lazy(cmd, argv)


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
    _validate_out_path(p, args.out)

    if args.dry_run:
        return _dry_run_exit(args, seed_cases)

    if not _run_preconditions(p, args, seed_cases):
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
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
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
        "提供后不再让模型生成用例，expected 参与事实断言（ground-truth 一票否决）"
        + CASES_FILE_HINT,
    )
    p.add_argument(
        "--assert-mode",
        default=DEFAULT_MODE,
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
    p.add_argument(
        "--out",
        default=None,
        help="报告输出文件路径（不是目录；父目录不存在会自动创建，路径错误在开跑前退 2）",
    )
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
    p.add_argument(
        "--yes",
        action="store_true",
        help="跳过真实运行前的成本确认（交互终端下默认先报价并要求 y/N 确认；"
        "--json / 非交互 / 假后端演示模式本来就不问，无需此开关）",
    )
    p.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="详细日志：DEBUG 级（缺省是 INFO，且已静默 httpx2/httpcore2 这类"
        "每次调用打一行的传输层噪声）",
    )
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
        if Path(args.task_file).is_dir():
            p.error(f"--task-file 要的是文件，不是目录：{args.task_file}")
        try:
            decoded = read_text_auto(args.task_file)
        except OSError as e:
            p.error(f"--task-file 读取失败：{e}")
        except TextDecodeError as e:
            p.error(f"--task-file 解码失败：{e}")
        warn_if_fallback(decoded, "--task-file")
        task = decoded.text.strip()
    if not task:
        p.error("需求描述为空")
    # 与 API 的 OptimizeRequest 同口径（4-8000）：CLI 是另一个烧钱入口，
    # 没有长度护栏时一次粘贴几万字就能直冲 LLM，成本护栏形同虚设（2026-09-13 测试发现）
    if len(task) < 4 or len(task) > 8000:
        p.error(f"需求描述长度需在 4-8000 字之间（当前 {len(task)} 字）")
    return task


def _validate_out_path(p: argparse.ArgumentParser, raw: str | None) -> None:
    """`--out` 必须是可写的**文件**路径；目录/只读目标要在开跑前报错。

    2026-10-22 实测：`--out C:\\tmp`（目录）会先完整跑完一轮，最后在
    `save_artifacts` 里抛 PermissionError 裸栈、退出码 1 —— 钱已经花了，
    报告却没落盘。这种纯参数错误必须在调用任何模型之前退 2。
    """
    if not raw:
        return
    text = str(raw)
    out = Path(text)
    if text.endswith(("/", "\\")) and not out.exists():
        p.error(f"--out 需要文件路径，不是目录：{out}\n请给完整文件名，例如 {out / 'report.md'}")
    if out.exists() and out.is_dir():
        p.error(f"--out 需要文件路径，不是目录：{out}\n请给完整文件名，例如 {out / 'report.md'}")
    if out.exists() and not os.access(out, os.W_OK):
        p.error(f"--out 指向的文件不可写：{out}")
    parent = out.parent if str(out.parent) else Path(".")
    if parent.exists() and not parent.is_dir():
        p.error(f"--out 的父路径不是目录：{parent}")
    if parent.exists() and not os.access(parent, os.W_OK):
        p.error(f"--out 的父目录不可写：{parent}")


def _load_seed_cases(p: argparse.ArgumentParser, args: argparse.Namespace) -> list[dict[str, Any]]:
    """--cases-file：读入 → 逐条成案 → 再预检一次 expected 的写法。没给就是空表。

    取值走 `support.case_entry_fields`（与 `check --cases-file` 同一份），
    校验顺序保持原样：空 input → 条数上限 → mode 合法性 → 全文 rule-like。
    """
    if not args.cases_file:
        return []
    raw = read_cases_file(p, args.cases_file)
    seed_cases: list[dict[str, Any]] = []
    for i, item in enumerate(raw):
        fields = case_entry_fields(item)
        if not fields["input"].strip():
            p.error(f"--cases-file 第 {i + 1} 条缺少非空 input")
        if len(seed_cases) >= MAX_SEED_CASES:
            # 不能默默丢掉多余用例：n_test_cases 按声明条数做完整性校验（C1），
            # 静默截断会让本轮永远无法判达标，且原因藏在报告角落里
            p.error(f"--cases-file 最多 {MAX_SEED_CASES} 条用例，当前 {len(raw)} 条；请筛选后再跑")
        case: dict[str, Any] = {
            "input": fields["input"],
            "expected": fields["expected"],
            # 每条可自带 mode（"mode" 或 "assert_mode"），没写就用全局 --assert-mode：
            # 一份用例集里混着写字面片段与需求规则才是常态
            "mode": resolve_case_mode(fields["mode_raw"], args.assert_mode, i, p),
        }
        # 注入存活专项（确定性校验，不经评委）的可选字段：seed 携带时透传给
        # mock_node → case_scenarios / hijack_markers，与 mockgen 路径同口径
        for _opt in ("scenario", "hijack_marker"):
            if fields[_opt]:
                case[_opt] = fields[_opt]
        seed_cases.append(case)
    # 用例数以用户提供为准（断言按序号与 expected 对齐）
    args.cases = len(seed_cases)

    _reject_rule_like_expected(p, seed_cases)
    return seed_cases


def _reject_rule_like_expected(
    p: argparse.ArgumentParser, seed_cases: list[dict[str, Any]]
) -> None:
    """expected 写成需求规则、mode 却是 contains/exact 时当场拦住。

    预检：contains/exact 比的是字面片段，而人写的 expected 常常是需求规则。
    那种写法永远命不中，只会把基线与优化版一起打死 —— 与其跑完 20 次计费调用
    再看报告，不如现在就拦住（真实跑踩过一次，见 qa_report 第十二节）。

    判据与话术已收口到 `pm.assertions.rule_like_expected_detail`（2026-10-23）：
    REST /api/optimize 此前没有这道闸，同一份用例从 API 进来会被静默降级成
    "仅提醒"，一票否决变成装饰 —— 两个入口必须同一把尺。
    """
    from pm.assertions import rule_like_expected_detail

    detail = rule_like_expected_detail(seed_cases, origin="--cases-file")
    if detail:
        p.error(detail)


# ---------------------------------------------------------------------------
# --dry-run：不联网的调用数与金额预估
# ---------------------------------------------------------------------------


def _estimate_calls(args: argparse.Namespace, seed_cases: list[dict[str, Any]]) -> dict[str, Any]:
    """真实运行前的一次调用数与金额预估（不联网、不调用）。

    **唯一事实源**：`--dry-run` 的 JSON 与交互成本闸门都读这一个函数 ——
    两处各算一份的话，报价迟早跟实际参数漂开，而报价正是用户唯一能拿来自查的东西。
    """
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
    return payload


def _dry_run_exit(args: argparse.Namespace, seed_cases: list[dict[str, Any]]) -> int:
    print(json.dumps(_estimate_calls(args, seed_cases), ensure_ascii=False, indent=2))
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


# Key 覆盖判定收在 pm/keycheck.py（CLI 闸门与 doctor 共用一份；`_KEY_ROLES` /
# `_missing_key_roles` 这两个别名保留给既有测试与调用方）。


def _print_no_key_hint(p: argparse.ArgumentParser, missing: list[str]) -> None:
    if len(missing) == len(_KEY_ROLES):
        print(
            "错误：未检测到 PM_API_KEY（也没有 PM_API_KEYS 多 Key 池）。\n"
            "请复制 .env.example 为 .env 并填写，或直接 export PM_API_KEY=...",
            file=sys.stderr,
        )
    else:
        print(
            "错误：缺少这些角色的 API Key：" + "、".join(missing) + "\n"
            "全局 PM_API_KEY / PM_API_KEYS 未配置，而角色级 Key 只覆盖了一部分；\n"
            "请补全局 Key，或为每个角色设置 PM_<角色>_API_KEY。",
            file=sys.stderr,
        )
    print(
        "无 Key 时的两条本地路径：\n"
        f"  {p.prog} --selftest"
        "                            只验证图拓扑与控制流（秒级）\n"
        f'  PM_FAKE_BACKEND=progress {p.prog} --task "..."     假后端跑完整流程'
        "（只验证链路，不证明优化效果）",
        file=sys.stderr,
    )


def _api_key_gate(p: argparse.ArgumentParser) -> bool:
    """True = 可以继续（有 Key，或假后端演示模式）；False = 已打印指引，退出码 2。"""
    missing = _missing_key_roles()
    if not missing:
        return True
    # 假后端场景下整轮不出网，闸门不该拦 —— 这正是 run.py 文档承诺的无 Key 入门路径。
    # 口径与 pipeline 的钩子装配同源（active_scenario），非法场景名两边都不算演示。
    from pm.testing import active_scenario  # 延迟导入：--help 轻路径不拉起 schemas

    if not active_scenario():
        _print_no_key_hint(p, missing)
        return False
    print(
        "演示模式：PM_FAKE_BACKEND 假后端，本次不调用任何真实端点（只验证链路，不证明优化效果）",
        file=sys.stderr,
    )
    return True


def _confirm_expensive_run(args: argparse.Namespace, seed_cases: list[dict[str, Any]]) -> bool:
    """交互式真实运行前的**成本闸门**：先报价，再要一句显式确认。

    True = 可以继续；False = 用户取消（已打印说明，调用方统一退 2）。

    为什么只拦"能问人"的场景：Agent / CI / 管道里没有人能回答 `y/N`，拦下来等于把
    自动化卡死 —— 所以 `--json`、非 TTY、假后端演示模式一律直接放行。
    与桌面端"提交前把预估区间写进确认框"是同一意图，这里补上此前只有桌面有的那份
    （真实事故形状：`.env` 里有 Key 的人敲下 `--task` 就直接开跑几十次计费调用）。
    """
    if args.yes or args.json or not (sys.stdin.isatty() and sys.stdout.isatty()):
        return True
    from pm.testing import active_scenario

    if active_scenario():  # 假后端整轮不出网、零成本，没什么可确认的
        return True

    est = _estimate_calls(args, seed_cases)
    calls = est["estimated_llm_calls"]
    cost = est.get("estimated_cost") or {}
    amount = ""
    if cost.get("estimated_total_max") is not None:
        amount = (
            f"，预估金额 {cost['estimated_total_min']} ~ {cost['estimated_total_max']}"
            "（单位随 PM_PRICE_*_PER_M）"
        )
    print(
        f"\n这一轮是**真实运行**：预计 {calls['min']}~{calls['max']} 次 LLM 调用{amount}。",
        file=sys.stderr,
    )
    print(
        "  只看报价、先不跑：  run.py --dry-run …（零调用）\n"
        "  先确认端点活着：    run.py --preflight\n"
        "  长任务别占终端：    python run_server.py & 然后 run.py submit …\n"
        "  确认过就不再问：    run.py --yes …",
        file=sys.stderr,
    )
    try:
        answer = input("确认开始？（y/N）> ").strip().lower()
    except EOFError:
        answer = ""
    if answer in ("y", "yes"):
        return True
    print("已取消：本次没有发生任何调用。", file=sys.stderr)
    return False


def _run_preconditions(
    p: argparse.ArgumentParser, args: argparse.Namespace, seed_cases: list[dict[str, Any]]
) -> bool:
    """真实运行的两道前置闸：① 有 Key；② 交互场景下用户确认过成本。

    任一不过都已在 stderr 给了人话，调用方统一退 2。合并成单一调用点，是为了让
    `main()` 的判定顺序仍是"一行一剂量"（原先那里只挂了 Key 闸门）。
    """
    if not _api_key_gate(p):
        return False
    return _confirm_expensive_run(args, seed_cases)


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
