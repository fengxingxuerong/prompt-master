"""项目分析后端的回归：无 Key 不调用、只调用一次、输出可直接渲染。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from extras import project_agent as agent
from pm import backend

REPO = Path(__file__).resolve().parents[1]
SECRET = "sk-abcdefghijklmnopqrstuvwxyz"


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    root = tmp_path / "demo"
    (root / "src").mkdir(parents=True)
    (root / ".env").write_text(f"PM_API_KEY={SECRET}\n", encoding="utf-8")
    (root / "README.md").write_text("# Demo\n\n已完成登录，下一步做支付。\n", encoding="utf-8")
    (root / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    (root / "src" / "app.py").write_text("# TODO: add auth\n", encoding="utf-8")
    return root


def _fake_llm(optimized: str, captured: dict[str, str] | None = None) -> Any:
    def fake(
        role: str,
        model_cls: Any,
        system: str,
        user: str,
        max_retries: int = 3,
        overrides: dict[str, Any] | None = None,
    ) -> tuple[Any, dict[str, Any]]:
        if captured is not None:
            captured["user"] = user
        plan = model_cls(
            project_summary="项目已完成登录，支付尚未开始。",
            evidence=["src/app.py：TODO add auth", "README.md：下一步做支付"],
            prompt_gaps=["缺少输出格式", "信息不足时没有处理规则"],
            optimized_prompt=optimized,
            assumptions=["支付渠道未指定"],
        )
        return plan, {
            "role": role,
            "model": "fake",
            "channel": "fake",
            "requests": 1,
            "latency_ms": 3,
        }

    return backend.CallHook(structured=fake)


@contextmanager
def _using_fake(optimized: str, captured: dict[str, str] | None = None) -> Iterator[None]:
    with backend.use(_fake_llm(optimized, captured)):
        yield


def test_scan_only_works_without_any_key(monkeypatch: pytest.MonkeyPatch, project: Path) -> None:
    for name in ("PM_API_KEY", "PM_API_KEYS", "PM_OPTIMIZER_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    result = agent.analyze(project_dir=str(project), scan_only=True)
    assert result["mode"] == "project_scan"
    assert result["ok"] is True
    assert result["project"]["file_count"] >= 3
    assert "optimized_prompt" not in result


def test_scan_only_requires_a_project() -> None:
    with pytest.raises(agent.AgentError) as caught:
        agent.analyze(scan_only=True)
    assert caught.value.code == "invalid_input"
    assert caught.value.exit_code == 2


def test_generate_requires_a_goal_or_prompt() -> None:
    with pytest.raises(agent.AgentError) as caught:
        agent.analyze()
    assert caught.value.code == "invalid_input"


def test_overlong_prompt_is_rejected_before_any_model_call() -> None:
    with pytest.raises(agent.AgentError) as caught:
        agent.analyze(goal="继续开发", prompt="x" * (agent.MAX_PROMPT_CHARS + 1))
    assert caught.value.code == "invalid_input"
    assert "提示词太长" in str(caught.value)


def test_missing_key_is_reported_before_any_model_call(
    monkeypatch: pytest.MonkeyPatch, project: Path
) -> None:
    monkeypatch.setattr(agent, "has_llm_key", lambda: False)
    with pytest.raises(agent.AgentError) as caught:
        agent.analyze(project_dir=str(project), prompt="继续开发", goal="实现支付")
    assert caught.value.code == "no_key"
    assert caught.value.exit_code == 2
    assert "只读取项目" in str(caught.value)


def test_analyze_returns_analysis_prompt_and_quality_checks(
    monkeypatch: pytest.MonkeyPatch, project: Path
) -> None:
    monkeypatch.setattr(agent, "has_llm_key", lambda: True)
    captured: dict[str, str] = {}
    optimized = "[角色] 你是项目维护者。\n[任务] 实现支付。"
    with _using_fake(optimized, captured):
        result = agent.analyze(project_dir=str(project), prompt="继续开发", goal="实现支付")

    assert result["mode"] == "project_optimize"
    assert result["analysis"]["project_summary"].startswith("项目已完成登录")
    assert result["optimized_prompt"] == optimized
    assert result["checks"]["findings"], "过短的提示词必须被零调用规则体检命中"
    assert result["usage"]["model"] == "fake"
    user_prompt = captured["user"]
    assert "<PROJECT_EVIDENCE" in user_prompt
    assert "TODO: add auth" in user_prompt
    assert "<DETERMINISTIC_QUALITY_FINDINGS>" in user_prompt
    assert SECRET not in user_prompt, "密钥文件的内容绝不能被送进模型上下文"


def test_code_fences_are_stripped(monkeypatch: pytest.MonkeyPatch, project: Path) -> None:
    monkeypatch.setattr(agent, "has_llm_key", lambda: True)
    fenced = "```markdown\n[角色] 你是项目维护者。\n[任务] 实现支付。\n```"
    with _using_fake(fenced):
        result = agent.analyze(project_dir=str(project), goal="实现支付")
    assert result["optimized_prompt"] == "[角色] 你是项目维护者。\n[任务] 实现支付。"


def test_empty_optimized_prompt_is_an_error(monkeypatch: pytest.MonkeyPatch, project: Path) -> None:
    monkeypatch.setattr(agent, "has_llm_key", lambda: True)
    with _using_fake("   "), pytest.raises(agent.AgentError) as caught:
        agent.analyze(project_dir=str(project), goal="实现支付")
    assert caught.value.code == "empty_prompt"


def _clean_env() -> dict[str, str]:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    for name in ("PM_API_KEY", "PM_API_KEYS", "PM_OPTIMIZER_API_KEY"):
        env.pop(name, None)
    return env


def test_cli_scan_only_emits_exactly_one_json(project: Path) -> None:
    proc = subprocess.run(
        [
            sys.executable,
            str(REPO / "extras" / "project_agent.py"),
            "--project-dir",
            str(project),
            "--scan-only",
            "--json",
        ],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        env=_clean_env(),
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["mode"] == "project_scan"
    assert payload["project"]["root_name"] == "demo"


def test_cli_request_file_uses_the_same_path_as_the_desktop(project: Path, tmp_path: Path) -> None:
    request_file = tmp_path / "request.json"
    request_file.write_text(
        json.dumps({"project_dir": str(project), "scan_only": True}, ensure_ascii=False),
        encoding="utf-8",
    )
    proc = subprocess.run(
        [
            sys.executable,
            str(REPO / "extras" / "project_agent.py"),
            "--request-file",
            str(request_file),
            "--json",
        ],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        env=_clean_env(),
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["mode"] == "project_scan"


def test_cli_generate_emits_json_with_fake_llm(
    monkeypatch: pytest.MonkeyPatch, project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """覆盖桌面端真正走的出口：main() → 一次结构化调用 → 单个 JSON。"""
    monkeypatch.setattr(agent, "has_llm_key", lambda: True)
    with _using_fake("[角色] 你是项目维护者。\n[任务] 实现支付。"):
        rc = agent.main(["--project-dir", str(project), "--goal", "实现支付", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "project_optimize"
    assert payload["optimized_prompt"].startswith("[角色]")


def test_render_text_scan_lists_progress(project: Path) -> None:
    result = agent.analyze(project_dir=str(project), scan_only=True)
    text = agent.render_text(result)
    assert "读取" in text
    assert "README.md" in text
    assert "待办" in text
