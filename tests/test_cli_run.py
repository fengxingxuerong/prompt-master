"""CLI 主函数 run 主干的补充测试。

tests/test_cli_units.py 已覆盖参数护栏 / dry-run / 退出码映射，
这里补 272-314 段的真实执行分支：--json 模式下流水线异常时
输出 JSON 错误对象并按退出码协议返回 EXIT_FAILED，而不是裸抛栈。
（单独成文件：test_cli_units.py 有未提交的惰性加载 WIP，避免纠缠。）
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pm.cli.main  # noqa: F401  __init__ 的惰性导出把 main 函数遮到 pm.cli.main 属性上
import pytest

cli_main = sys.modules["pm.cli.main"]


def _run_main(monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> int:
    monkeypatch.setattr(sys, "argv", ["run.py", *argv])
    return cli_main.main()


def test_main_json_failure_emits_json_and_exit_failed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """--json 模式流水线异常：JSON 错误对象 + EXIT_FAILED=3，不裸抛栈。"""

    def boom(args: Any, init: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("端点全挂")

    monkeypatch.setenv("PM_API_KEY", "sk-test")  # key 检查在 run_pipeline 之前
    monkeypatch.setattr("pm.cli.pipeline.run_pipeline", boom)
    code = _run_main(
        monkeypatch,
        ["--json", "--task", "让 AI 分析销售数据", "--out", str(tmp_path / "r.md")],
    )
    assert code == 3  # EXIT_FAILED（support.py：passed=0/undelivered=1/config=2/failed=3）
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "failed"
    assert "RuntimeError" in payload["error"]
    assert payload["run_id"]  # run_id 先于失败确定，便于日志追踪
