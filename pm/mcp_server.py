"""PromptMaster 的 MCP server：把提示词优化闭环暴露为标准 MCP 工具。

任何 MCP 客户端（OpenClaw / Claude Code / Cursor / Loomy…）都能挂载：

    python -m pm.mcp_server          # stdio 传输（MCP 客户端标准接入方式）
    # 客户端配置示例：
    # {"command": "python", "args": ["-m", "pm.mcp_server"], "cwd": "<仓库根>"}

架构与边界（读我再用）：
- **计算类工具**（estimate/history/library/calibrate）：subprocess 调 `run.py --json`。
  run.py 是唯一事实来源——CLI 行为怎么变，MCP 工具就跟怎么变，不会出现两套口径。
- **长任务工具**（submit/status/wait/report）：转发 HTTP 到常驻 server。
  MCP over stdio 的进程随客户端会话生死，10~30 分钟的优化任务必须外置到
  常驻 server（`python run_server.py`）——这是架构约束，不是实现偷懒。
  server 未启动时工具会返回带启动指引的错误。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("prompt-master")

SERVER_URL = os.getenv("PM_SERVER_URL", "http://127.0.0.1:8080").rstrip("/")
ROOT = Path(__file__).resolve().parent.parent
_TERMINAL = {"passed", "max_iterations", "failed", "early_stopped", "needs_clarification"}


def _run_cli_json(args: list[str], timeout: float = 60.0) -> Any:
    """subprocess 调 `run.py ... --json`，返回 stdout 解析后的 JSON。"""
    proc = subprocess.run(
        [sys.executable, str(ROOT / "run.py"), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        cwd=str(ROOT),
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    if proc.returncode not in (0, 1, 2, 3):
        raise RuntimeError(f"run.py 退出码异常 {proc.returncode}：{proc.stderr[-300:]}")
    return json.loads(proc.stdout)


def _http_json(method: str, url: str, payload: dict | None = None, timeout: float = 30.0) -> Any:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
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
            f"无法连接 server（{url}）：{e.reason}。请先常驻启动：python run_server.py"
        ) from e


# --------------------------------------------------------------------------
# 计算类工具（subprocess → run.py --json，与 CLI 单一口径）
# --------------------------------------------------------------------------


@mcp.tool()
def estimate_cost(task: str, cases: int = 3, max_iter: int = 3, fast: bool = False) -> str:
    """预估一次优化任务的 LLM 调用次数区间。不联网、不需要 Key，提交前的预算闸门。

    Args:
        task: 原始需求描述（用于确定用例集形态，内容不参与估算）
        cases: 测试用例数量（1-8）
        max_iter: 最大修订轮次（0-10）
        fast: 快速档（关基线/盲评、单采样、1 轮修订）
    """
    args = ["--dry-run", "--task", task, "--cases", str(cases), "--max-iter", str(max_iter)]
    if fast:
        args.append("--fast")
    result = _run_cli_json(args)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
def run_history(last: int = 20) -> str:
    """运行历史聚合 + 同任务跨 run 的 Δ 显著性判定（均值 ± 95% CI，不含 0 才算变好）。

    Args:
        last: 只看最近 N 条真实运行（演示/测试产物自动排除）
    """
    result = _run_cli_json(["history", "--last", str(last), "--json"])
    return json.dumps(result, ensure_ascii=False, default=str)


@mcp.tool()
def library_recommend(task_text: str) -> str:
    """记忆层：按新任务文本推荐历史相似的高分提示词资产（top-3）。

    高分相似资产可作 few-shot 参考塞进新任务的 context，或经 optimize_export 后
    作为修订起点——避免每个新需求都从零开始。
    """
    result = _run_cli_json(["library", "--recommend", "--task-text", task_text, "--json"])
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
def library_export(run_id: str) -> str:
    """导出指定运行的最佳提示词成品（返回导出路径与头信息）。"""
    result = _run_cli_json(["library", "--export", run_id])
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
def calibrate_judge(judge: str = "evaluator") -> str:
    """评委校准：锚点样本上对比评委分与人工专家分，并给出与上次的漂移告警。

    真实调用（每条锚点 1 次评估），端点不可用时返回失败详情。结果自动入漂移账本。
    """
    result = _run_cli_json(["calibrate", "--judge", judge, "--json"], timeout=300.0)
    return json.dumps(result, ensure_ascii=False)


# --------------------------------------------------------------------------
# 长任务工具（转发常驻 server）
# --------------------------------------------------------------------------


@mcp.tool()
def optimize_submit(
    task: str,
    target_model: str = "未指定",
    n_test_cases: int = 3,
    max_iterations: int = 3,
    cases_json: str = "",
) -> str:
    """提交提示词优化任务（异步：秒回 run_id，用 optimize_wait 等终态）。

    Args:
        task: 原始需求描述（4-8000 字）
        target_model: 提示词最终运行的目标模型（影响优化策略）
        n_test_cases: 测试用例数量（1-8；提供 cases_json 时以用例数为准）
        max_iterations: 最大修订轮次（0-10）
        cases_json: 可选，JSON 数组字符串：
            [{"input": "...", "expected": "...", "mode": "contains|rule",
              "scenario": "injection", "hijack_marker": "..."}]
            提供后跳过用例生成，expected 参与事实断言（一票否决）
    """
    payload: dict[str, Any] = {
        "task": task,
        "target_model": target_model,
        "n_test_cases": n_test_cases,
        "max_iterations": max_iterations,
    }
    if cases_json.strip():
        cases = json.loads(cases_json)
        payload["test_cases"] = cases
        payload["n_test_cases"] = len(cases)
    resp = _http_json("POST", f"{SERVER_URL}/api/optimize", payload)
    return json.dumps(resp, ensure_ascii=False)


@mcp.tool()
def optimize_status(run_id: str) -> str:
    """查询任务状态：status / iteration / aggregate（分数）/ llm_calls / 错误。"""
    resp = _http_json("GET", f"{SERVER_URL}/api/status/{run_id}", timeout=15.0)
    return json.dumps(resp, ensure_ascii=False, default=str)


@mcp.tool()
def optimize_report(run_id: str) -> str:
    """获取任务交付报告全文（Markdown：最终提示词 + 评分 + 基线对比 + 遗留问题）。"""
    resp = _http_json("GET", f"{SERVER_URL}/api/report/{run_id}", timeout=30.0)
    return json.dumps({"run_id": run_id, "report": resp.get("report", "")}, ensure_ascii=False)


@mcp.tool()
def optimize_wait(run_id: str, timeout_seconds: int = 2400, interval_seconds: int = 15) -> str:
    """阻塞等待任务到终态，返回终态结果（含报告全文）。10~30 分钟的任务请把
    timeout_seconds 设够（默认 2400）；客户端超时短于任务时长时，改用 status 轮询。

    终态：passed（达标）/ max_iterations / early_stopped（未达标但已交付）/
    failed（运行失败）/ needs_clarification（需求澄清被挂起）。
    """
    deadline = time.monotonic() + max(30, timeout_seconds)
    status_data: dict[str, Any] = {}
    while True:
        status_data = _http_json("GET", f"{SERVER_URL}/api/status/{run_id}", timeout=15.0)
        if status_data.get("status") in _TERMINAL:
            break
        if time.monotonic() > deadline:
            return json.dumps(
                {"run_id": run_id, "status": status_data.get("status"), "error": "等待超时"},
                ensure_ascii=False,
            )
        time.sleep(max(5, interval_seconds))
    final_status = str(status_data.get("status"))
    report = ""
    if final_status in ("passed", "max_iterations", "early_stopped"):
        report = str(
            _http_json("GET", f"{SERVER_URL}/api/report/{run_id}", timeout=30.0).get("report", "")
        )
    result = {
        "run_id": run_id,
        "status": final_status,
        "aggregate": status_data.get("aggregate"),
        "iterations": status_data.get("iteration"),
        "llm_calls": status_data.get("llm_calls"),
        "error": status_data.get("error"),
        "report": report,
    }
    return json.dumps(result, ensure_ascii=False, default=str)


def main() -> None:
    mcp.run()  # stdio 传输；MCP 客户端负责进程生命周期


if __name__ == "__main__":
    main()
