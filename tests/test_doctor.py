"""`run.py doctor`：零调用环境诊断（2026-10-23）。

判据是"用户配错时能不能在花一分钱之前说清哪里不对"：
- 没有 Key / 只有角色级 Key / 多 Key 池 / 占位符，四种配置要给出不同结论；
- 诊断输出绝不回显完整 Key；
- `--json` 的 stdout 恰好一个对象，退出码 0/1/2 与结论一致。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from pm.doctor import build_report
from pm.keycheck import KEY_ROLES, is_placeholder_key, key_status, mask_key

ROOT = Path(__file__).resolve().parents[1]
RUN = [sys.executable, str(ROOT / "run.py")]

_KEY_VARS = (
    "PM_API_KEY",
    "PM_API_KEYS",
    "PM_FAKE_BACKEND",
    "PM_PROVIDER",
    "PM_SERVER_URL",
    *(f"PM_{role.upper()}_API_KEY" for role in KEY_ROLES),
)


def _clear(monkeypatch: pytest.MonkeyPatch) -> None:
    # 必须是"设为空串"而不是 delenv：doctor 会读 .env，del 掉之后仓库那份真 Key
    # 会被重新灌进来。空值在 dotenv 的 override=False 下能挡住文件值。
    for name in _KEY_VARS:
        monkeypatch.setenv(name, "")
    # 诊断不联网：外网地址会被 doctor 直接跳过
    monkeypatch.setenv("PM_SERVER_URL", "http://example.invalid")
    monkeypatch.delenv("PM_LOG_DIR", raising=False)


def test_no_keys_is_an_error(monkeypatch, tmp_path) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv("PM_LOG_DIR", str(tmp_path))
    report = build_report()
    assert report.exit_code == 2
    key_check = next(c for c in report.checks if c.name == "Key")
    assert key_check.status == "error" and "缺少角色 Key" in key_check.detail
    assert report.facts["keys"]["missing_roles"] == list(KEY_ROLES)


def test_multi_key_pool_is_accepted(monkeypatch, tmp_path) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv("PM_LOG_DIR", str(tmp_path))
    monkeypatch.setenv("PM_API_KEYS", "sk-a,sk-b")
    report = build_report()
    assert report.exit_code == 0, [c.as_dict() for c in report.checks if c.status != "ok"]
    assert report.facts["keys"]["pool_count"] == 2
    assert all("多 Key 池" in r["key_tail"] for r in report.facts["roles"] if "key_tail" in r)


def test_placeholder_key_is_an_error_not_a_pass(monkeypatch, tmp_path) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv("PM_LOG_DIR", str(tmp_path))
    monkeypatch.setenv("PM_API_KEY", "sk-xxxxxxxxxxxxxxxx")
    report = build_report()
    assert report.exit_code == 2
    key_check = next(c for c in report.checks if c.name == "Key")
    assert "占位符" in key_check.detail and "PM_API_KEY" in key_check.detail


def test_fake_backend_needs_no_key(monkeypatch, tmp_path) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv("PM_LOG_DIR", str(tmp_path))
    monkeypatch.setenv("PM_FAKE_BACKEND", "progress")
    report = build_report()
    assert report.exit_code == 0
    assert "演示模式" in next(c for c in report.checks if c.name == "Key").detail


def test_doctor_never_echoes_the_full_key(monkeypatch, tmp_path) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv("PM_LOG_DIR", str(tmp_path))
    secret = "sk-live-0123456789abcdef"
    monkeypatch.setenv("PM_API_KEY", secret)
    text = json.dumps(build_report().as_dict(), ensure_ascii=False)
    assert secret not in text
    assert "…cdef" in text


def test_external_server_url_is_not_pinged(monkeypatch, tmp_path) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv("PM_LOG_DIR", str(tmp_path))
    report = build_report()
    assert "跳过" in report.facts["server"]["detail"]


def test_keycheck_helpers() -> None:
    assert is_placeholder_key("sk-xxxxxxxx")
    assert is_placeholder_key("your-api-key")
    assert not is_placeholder_key("sk-real-value-1234")
    assert mask_key("sk-abcdef") == "…cdef"
    assert mask_key("") == "(无)"
    status = key_status()
    assert set(status) == {"global", "pool_count", "role_keys", "missing_roles", "placeholders"}


# --------------------------------------------------------------------------
# 真实入口：stdout 契约与退出码
# --------------------------------------------------------------------------
def _run(args: list[str], **env_over: str) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "PYTHONIOENCODING": "utf-8",
        "PM_SERVER_URL": "http://example.invalid",
        **dict.fromkeys(_KEY_VARS, ""),
        **env_over,
    }
    return subprocess.run(
        [*RUN, *args],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=120,
    )


def test_cli_no_key_json_is_single_object_and_exit_2(tmp_path) -> None:
    proc = _run(["doctor", "--json"], PM_LOG_DIR=str(tmp_path))
    assert proc.returncode == 2, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["mode"] == "doctor" and payload["ok"] is False
    assert payload["exit_code"] == 2


def test_cli_help_mentions_doctor() -> None:
    proc = _run(["--help"])
    assert proc.returncode == 0
    assert "doctor" in proc.stdout and "环境诊断" in proc.stdout
