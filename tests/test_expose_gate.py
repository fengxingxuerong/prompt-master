"""fail-closed 暴露门（`pm.netguard.refuse_exposed_without_token`）的守卫（2026-10-23）。

背景：2026-10-22 的发布评审把「鉴权缺省失败开放 × Docker 默认 `--host 0.0.0.0`」
列为部署阻断项——旧姿态是启动时打一条 warning 然后照常把写接口交给同网段，
README 写「公网暴露前必须处理」却没有任何机制兜底，Dockerfile 的示例命令
甚至不带 PM_API_TOKEN 就映射端口。三个启动器（run_server.py / run_modelhub.py /
`run.py panel`）现在共用这一份判据，本文件把门本身与三个接线都钉住。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from pm.cli import panelcmd
from pm.netguard import is_loopback_bind, refuse_exposed_without_token

ROOT = Path(__file__).resolve().parents[1]


def test_is_loopback_bind_matrix() -> None:
    assert is_loopback_bind("127.0.0.1")
    assert is_loopback_bind("localhost")
    assert is_loopback_bind("::1")
    assert is_loopback_bind("")  # 空串 = uvicorn 缺省，仍是回环
    # `::` 与 `0.0.0.0` 是"所有网卡"，不是回环——最容易误当成"还是只有本机"
    assert not is_loopback_bind("0.0.0.0")
    assert not is_loopback_bind("::")
    assert not is_loopback_bind("192.168.1.10")


def test_refusal_message_when_exposed_without_token(monkeypatch) -> None:
    monkeypatch.delenv("PM_API_TOKEN", raising=False)
    msg = refuse_exposed_without_token("0.0.0.0")
    assert msg is not None
    assert "拒绝启动" in msg
    assert "PM_API_TOKEN" in msg
    assert "127.0.0.1" in msg, "要给出「回环」这条出路，而不是只说不行"


def test_no_refusal_when_token_set_or_loopback(monkeypatch) -> None:
    monkeypatch.setenv("PM_API_TOKEN", "tok")
    assert refuse_exposed_without_token("0.0.0.0") is None
    monkeypatch.delenv("PM_API_TOKEN", raising=False)
    assert refuse_exposed_without_token("127.0.0.1") is None


def test_modelhub_gate_uses_its_own_token_name(monkeypatch) -> None:
    """ModelHub 换 token 名与服务名，判据同一把——/v1/keys 的签发/删除/列出
    在未设 PMH_GATEWAY_TOKEN 时对任何人敞开，这不许靠「建议」兜底。"""
    monkeypatch.delenv("PMH_GATEWAY_TOKEN", raising=False)
    msg = refuse_exposed_without_token(
        "0.0.0.0", token_env="PMH_GATEWAY_TOKEN", service="ModelHub 网关"
    )
    assert msg is not None
    assert "PMH_GATEWAY_TOKEN" in msg and "ModelHub 网关" in msg
    monkeypatch.setenv("PMH_GATEWAY_TOKEN", "tok")
    assert (
        refuse_exposed_without_token(
            "0.0.0.0", token_env="PMH_GATEWAY_TOKEN", service="ModelHub 网关"
        )
        is None
    )


def _run_entry(
    script: str, *args: str, extra_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """打真实入口脚本。显式把两个 token 变量置空：仓库根可能有 .env，
    prepare_console 会加载它——置空串可挡住 dotenv 不覆盖已有变量的缺省行为，
    保证测的是「没设 token」这条路而不是「碰巧本机设了」。"""
    env = dict(os.environ)
    env["PM_API_TOKEN"] = ""
    env["PMH_GATEWAY_TOKEN"] = ""
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, str(ROOT / script), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        cwd=str(ROOT),
        timeout=120,
    )


def test_run_server_entry_refuses_exposed_without_token() -> None:
    proc = _run_entry("run_server.py", "--host", "0.0.0.0")
    assert proc.returncode == 2, "配置错误必须是退出码 2（协议：2 = 参数或配置错）"
    assert "拒绝启动" in proc.stderr
    assert "PM_API_TOKEN" in proc.stderr


def test_run_modelhub_entry_refuses_exposed_without_token() -> None:
    proc = _run_entry("run_modelhub.py", "--host", "0.0.0.0")
    assert proc.returncode == 2
    assert "PMH_GATEWAY_TOKEN" in proc.stderr


def test_panel_command_refuses_exposed_without_token(capsys, monkeypatch) -> None:
    monkeypatch.delenv("PM_API_TOKEN", raising=False)
    rc = panelcmd.panel_command(["--host", "0.0.0.0", "--no-browser"])
    assert rc == 2
    assert "拒绝启动" in capsys.readouterr().err
