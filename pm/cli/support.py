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

# 原 run.py 同款：仓库根/logs（拆包后 __file__ 深了两层，改用 parents[2] 回到仓库根）
_DEFAULT_LOG_DIR = Path(__file__).resolve().parents[2] / "logs"
# 兼容旧导出名（run.py / pm.cli 都转发过它）。⚠️ 运行期取路径请用 `log_dir()`，
# 不要拿这个常量再拼 —— 它在 import 时就冻结了，之后设的 `PM_LOG_DIR` 对它无效
# （2026-09-25 实测：产物落在 tmp，校准账本却写进仓库 logs，同一个变量两种命运）。
LOG_DIR = Path(os.getenv("PM_LOG_DIR") or _DEFAULT_LOG_DIR)


def log_dir() -> Path:
    """产物目录的**唯一**取法：每次调用都读一遍 `PM_LOG_DIR`。

    晚绑定是有意的：CLI 的启动引导（`load_dotenv()`）与各测试的 env 覆盖都发生在
    import 之后，import 期算出来的路径必然与运行期不一致。`pm/scheduler.py` 早就这么做了。
    """
    raw = os.getenv("PM_LOG_DIR", "").strip()
    return Path(raw) if raw else _DEFAULT_LOG_DIR


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
    item: dict[str, Any], default_mode: str, index: int, p: argparse.ArgumentParser
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
