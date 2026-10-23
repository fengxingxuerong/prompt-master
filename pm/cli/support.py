"""CLI 支撑层：日志、参数护栏、产物落盘、机器可读出口与退出码协议。（自 run.py 拆出，逻辑逐字保留）"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any

from pm.paths import (
    log_dir as log_dir,  # re-export：唯一实现在 pm/paths.py（schemas / calibration 局部导入走这里）
)
from pm.report import judge_seat_absent
from pm.state import SEED_PROMPT_MAX_CHARS
from pm.textio import TextDecodeError, read_text_auto, warn_if_fallback

# 原 run.py 同款：仓库根/logs（拆包后 __file__ 深了两层，改用 parents[2] 回到仓库根）
_DEFAULT_LOG_DIR = Path(__file__).resolve().parents[2] / "logs"
# 兼容旧导出名（run.py / pm.cli 都转发过它）。⚠️ 运行期取路径请用 `log_dir()`，
# 不要拿这个常量再拼 —— 它在 import 时就冻结了，之后设的 `PM_LOG_DIR` 对它无效
# （2026-09-25 实测：产物落在 tmp，校准账本却写进仓库 logs，同一个变量两种命运）。
LOG_DIR = Path(os.getenv("PM_LOG_DIR") or _DEFAULT_LOG_DIR)

# `--cases-file` 的条数上限。放在支撑层而不是写在 if 语句里，是因为现在有两处要读它：
# 跑批路径拿它**拦**（退出码 2），`check --cases-file` 拿它**提前报**（退出码 1）。
# 一处改数值、另一处就会悄悄失配 —— 那种"体检说没事、跑起来被拦"的门禁等于没装。
MAX_SEED_CASES = 8

# 三个解析器（`run.py` / `run.py check` / `run.py submit`）的 `--cases-file` 都要指向同一处模板。
# 各写一份的话，"模板去哪找"迟早三处不同：2026-10-22 实测**三处都没写**，
# 用户只能翻 README 的仓库地图才知道有 `case_templates/`。
CASES_FILE_HINT = "；模板见 case_templates/（可直接喂 --cases-file 的领域用例集）"


# 非 verbose 时闭嘴的**传输层** logger。它们按**每次 HTTP 调用**打一行 ——
# 一轮真实运行 45~102 次调用就是 45~102 行 `HTTP Request: POST … 200 OK`，
# 而"请求成功"对用户不是信息，是流水账。
# ⚠️ 名单必须跟着**实际**HTTP 栈走：openai / langsmith 已从 httpx 迁到 **httpx2**
#    （2026-10-22 实测 `httpx2-2.12.0`，Required-by: langsmith, openai），
#    只静默 httpx/httpcore 等于没静默 —— httpx2 照样一行一次调用刷屏。
# `-v`（DEBUG 档）时一条都不静默：那一档就是要看到底。
_QUIET_LOGGERS = (
    "httpx",
    "httpx2",
    "httpcore",
    "httpcore2",
    "openai",
    "urllib3",
    "langsmith",
)


def setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)-12s %(message)s",
        datefmt="%H:%M:%S",
    )
    # basicConfig 在 root 已经有 handler 时会**整段 no-op**（含 level）——嵌在别的进程里
    # 跑时 `-v` 会静默失效。显式再设一次 root level，让档位与调用方无关。
    logging.getLogger().setLevel(level)
    # -v 时显式放回 NOTSET（继承 root=DEBUG）：否则同一进程里先跑过非 verbose、再开 -v，
    # 这些 logger 会被上一次留下的 WARNING 钉住，"-v 看起来没生效"。
    quiet = logging.NOTSET if verbose else logging.WARNING
    for name in _QUIET_LOGGERS:
        logging.getLogger(name).setLevel(quiet)


def read_seed_prompt(p: argparse.ArgumentParser, args: argparse.Namespace) -> str:
    """读取并护栏化原稿（--prompt / --prompt-file）；两者都没给时返回空串。

    同步 CLI 与 `submit` 子命令共用这一个函数：两条入口对原稿的护栏必须同数，
    否则同一份文件在一条路上能跑、另一条路上把成本护栏绕过去。
    """
    raw_prompt = getattr(args, "prompt", None)
    raw_file = getattr(args, "prompt_file", None)
    if raw_prompt and raw_file:
        p.error("--prompt 与 --prompt-file 互斥，二选一")
    if not raw_prompt and not raw_file:
        return ""
    raw = ""
    if raw_file:
        if Path(raw_file).is_dir():
            p.error(f"--prompt-file 要的是文件，不是目录：{raw_file}")
        try:
            decoded = read_text_auto(raw_file)
        except OSError as e:
            p.error(f"--prompt-file 读取失败：{e}")
        except TextDecodeError as e:
            p.error(f"--prompt-file 解码失败：{e}")
        warn_if_fallback(decoded, "--prompt-file")
        raw = decoded.text
    else:
        raw = str(raw_prompt)
    seed = raw.strip()
    # 下限 8 字：短到这个程度基本是把需求粘错了参数（那种情况该走 --task），
    # 与其让它进改进器白烧一轮调用，不如当场指出入口用错了
    if len(seed) < 8:
        p.error(
            f"原稿只有 {len(seed)} 字，不像是成形的提示词。"
            "如果这是需求描述，请改用 --task；原稿改进模式要求 --prompt 是你要改的那一版提示词"
        )
    if len(seed) > SEED_PROMPT_MAX_CHARS:
        p.error(
            f"原稿 {len(seed)} 字符超过上限 {SEED_PROMPT_MAX_CHARS}："
            "它会作为基线臂进 target 端点每条用例的每次调用，上不封顶等于绕过成本护栏"
        )
    return seed


def _resolve_case_mode(
    item: dict[str, Any], default_mode: str, index: int, p: argparse.ArgumentParser
) -> str:
    """取这条用例自己的 mode；没写就用全局值。非法值直接报错而不是静默回退。

    静默回退会把一条本意是“规则”的断言变成“字面比对”，后果要到报告里才看得见。
    """
    raw = str(item.get("mode") or item.get("assert_mode") or "").strip()
    return resolve_case_mode(raw, default_mode, index, p)


def resolve_case_mode(
    raw_mode: str, default_mode: str, index: int, p: argparse.ArgumentParser
) -> str:
    """`_resolve_case_mode` 的纯字符串内核：跑批路径与 `check --cases-file` 走同一把判据。"""
    if not raw_mode:
        return default_mode
    try:
        return assert_mode_arg(raw_mode)
    except argparse.ArgumentTypeError as e:
        p.error(f"--cases-file 第 {index + 1} 条：{e}")
        return default_mode  # 不可达：p.error 会 SystemExit


def read_cases_file(p: argparse.ArgumentParser, path: str) -> list[Any]:
    """`--cases-file` 的读取与顶层形状校验（跑批与体检两条路径共用一份）。"""
    if Path(path).is_dir():
        p.error(f"--cases-file 要的是文件，不是目录：{path}")
    try:
        decoded = read_text_auto(path)
    except OSError as e:
        p.error(f"--cases-file 读取失败：{e}")
        raise SystemExit(EXIT_CONFIG) from e
    except TextDecodeError as e:
        p.error(f"--cases-file 解码失败：{e}")
        raise SystemExit(EXIT_CONFIG) from e
    warn_if_fallback(decoded, "--cases-file")
    try:
        raw = json.loads(decoded.text)
    except json.JSONDecodeError as e:
        p.error(f"--cases-file 读取失败：{e}")
        raise SystemExit(EXIT_CONFIG) from e  # 不可达：p.error 会 SystemExit
    if not isinstance(raw, list) or not raw:
        p.error("--cases-file 需为非空 JSON 数组")
        raise SystemExit(EXIT_CONFIG)
    return list(raw)


def case_entry_fields(item: Any) -> dict[str, str]:
    """把一条用例（裸字符串或对象）**只取值**，不做任何判定。

    空 input、mode 合法性、条数上限都由调用方按自己的场景报 —— 跑批要先拦（退出码 2），
    体检要数着报（退出码 1），两边不能共用同一处报错顺序。
    """
    if isinstance(item, str):
        item = {"input": item}
    if not isinstance(item, dict):
        return {"input": "", "expected": "", "mode_raw": "", "scenario": "", "hijack_marker": ""}
    return {
        "input": str(item.get("input") or ""),
        "expected": str(item.get("expected") or ""),
        "mode_raw": str(item.get("mode") or item.get("assert_mode") or "").strip(),
        "scenario": str(item.get("scenario") or "").strip(),
        "hijack_marker": str(item.get("hijack_marker") or "").strip(),
    }


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


def save_artifacts(state: dict[str, Any], out: Path | None) -> tuple[Path, Path]:
    d = log_dir()  # 每次调用现取：引导里 load_dotenv() 晚于 import，冻结值会是错的
    d.mkdir(parents=True, exist_ok=True)
    run_id = state.get("run_id", "unknown")
    log_path = d / f"run_{run_id}.json"
    log_path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )

    report = state.get("final_report") or "_未生成报告_"
    report_path = out or (d / f"report_{run_id}.md")
    if isinstance(report_path, str):
        report_path = Path(report_path)
    # --out 常来自智能体拼的路径：父目录不存在时自动补齐，而不是把整轮运行砸在最后一步
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report, encoding="utf-8")
    _maybe_write_skill_md(state, report_path)
    return log_path, report_path


def _maybe_write_skill_md(state: dict[str, Any], report_path: Path) -> Path | None:
    """OpenClaw 方向（2026-09-16）：达标运行顺手产出 SKILL.md（部署即用）。

    - 仅 status=passed 且达标版本有正文时写出，路径与报告同目录；
    - 注入门禁未过（SkillGateError）时如实落一个 .blocked 说明文件，
      绝不写出会被一句话劫持的 Skill；
    - 渲染失败只告警不中断：SKILL.md 是增强交付物，报告本身已经落盘。
    """
    from pm.skillmd import SkillGateError, render_skill_md

    if state.get("status") != "passed":
        return None
    stem = report_path.stem or "report"
    skill_path = report_path.with_name(f"{stem}.SKILL.md")
    try:
        skill_path.write_text(render_skill_md(state), encoding="utf-8")
    except SkillGateError as e:
        blocked = report_path.with_name(f"{stem}.SKILL.md.blocked")
        blocked.write_text(f"# SKILL.md 未生成（注入门禁拦截）\n\n{e}\n", encoding="utf-8")
        print(f"⚠️ SKILL.md 被注入门禁拦截：{e}", file=sys.stderr)
        return blocked
    except Exception as e:  # noqa: BLE001 - 增强交付物失败不掩盖主报告
        print(f"⚠️ SKILL.md 渲染失败（不影响报告交付）：{type(e).__name__}: {e}", file=sys.stderr)
        return None
    # 进度提示一律走 stderr：--json 模式的 stdout 是 Agent 消费契约（恰好一个 JSON），
    # 任何人类可读输出混进去都会让 json.loads 直接炸（实测：SKILL 行混入首行前）。
    # 结构化路径由 save_artifacts 写进 state["skill_path"]，经 emit_json_result 下发。
    print(f"SKILL.md 已生成：{skill_path}", file=sys.stderr)
    state["skill_path"] = str(skill_path)
    return skill_path


def emit_json_result(final: dict[str, Any], log_path: Path, report_path: Path) -> None:
    """智能体模式的机器可读出口：stdout 恰好一个 JSON 对象，其余信息走文件/日志。

    字段以 Agent 的决策需求为准：状态、分数（含保守下界）、最佳提示词、
    产物路径、遗留问题。聚合缺失（如中途失败）时置 null 而不是编造结构。
    """
    versions = final.get("prompt_versions") or []
    payload = {
        "run_id": final.get("run_id"),
        "status": final.get("status"),
        "aggregate": final.get("aggregate"),
        # 下面四个是"结论到底站不站得住"的那几列，2026-10-15 实测它们**只在报告里、
        # 不在机器出口里**：真端点一轮报告写着基线 6.51 / Δ +0.74 / 盲评 2-1-1 /
        # 4 例降级，而 `--json` 的 stdout 里一个字段都没有 ⇒ 桌面端与面板只能显示"—"，
        # 把单评委共识当双评委共识读。判据不在这产生，只是把同一份推导搬过来。
        "baseline_aggregate": final.get("baseline_aggregate"),
        "pairwise": final.get("pairwise"),
        "judge_health": final.get("judge_health"),
        "judge_seat_absent": judge_seat_absent(final),
        "iterations": final.get("iteration", 0),
        "max_iterations": final.get("max_iterations"),
        "llm_calls": final.get("llm_calls", 0),
        "best_prompt": final.get("prompt"),
        "early_stop_reason": final.get("early_stop_reason", ""),
        "unresolved_questions": final.get("unresolved_questions") or [],
        "injection_survival": final.get("injection_survival"),
        "injection_gate": final.get("injection_gate"),
        "errors": final.get("errors") or [],
        "report_path": str(report_path),
        "log_path": str(log_path),
        "skill_path": final.get("skill_path"),
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
