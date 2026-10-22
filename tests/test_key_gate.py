"""运行前的 Key 闸门：多 Key 池与角色级 Key 都是合法配置（2026-10-22）。

修复前的判据只看 `PM_API_KEY` / `PM_TARGET_API_KEY`：
- `.env.example` 推荐的 `PM_API_KEYS` 多 Key 池会被判"没有 Key"（假拦）；
- 只有 target 一个角色 Key 时反而放行，其它角色到构造时才失败（晚失败）。

这里用单元测试直接打 `_api_key_gate`，不走网络。
"""

from __future__ import annotations

import argparse

import pytest
from pm.cli.main import _KEY_ROLES, _api_key_gate, _build_parser


def _clear_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("PM_API_KEY", "PM_API_KEYS", "PM_FAKE_BACKEND"):
        monkeypatch.delenv(name, raising=False)
    for role in _KEY_ROLES:
        monkeypatch.delenv(f"PM_{role.upper()}_API_KEY", raising=False)


def _gate() -> bool:
    p: argparse.ArgumentParser = _build_parser("run.py")
    return _api_key_gate(p)


def test_global_key_passes(monkeypatch) -> None:
    _clear_keys(monkeypatch)
    monkeypatch.setenv("PM_API_KEY", "sk-global")
    assert _gate() is True


def test_multi_key_pool_alone_passes(monkeypatch) -> None:
    _clear_keys(monkeypatch)
    monkeypatch.setenv("PM_API_KEYS", " sk-a , sk-b ")
    assert _gate() is True, "PM_API_KEYS 是 .env.example 的正式配置，不许假拦"


def test_empty_pool_does_not_count(monkeypatch, capsys) -> None:
    _clear_keys(monkeypatch)
    monkeypatch.setenv("PM_API_KEYS", " , ")
    assert _gate() is False
    err = capsys.readouterr().err
    assert "未检测到 PM_API_KEY" in err and "PM_API_KEYS" in err


def test_all_roles_having_their_own_keys_passes(monkeypatch) -> None:
    _clear_keys(monkeypatch)
    for role in _KEY_ROLES:
        monkeypatch.setenv(f"PM_{role.upper()}_API_KEY", f"sk-{role}")
    assert _gate() is True


def test_partial_role_keys_get_a_specific_message(monkeypatch, capsys) -> None:
    _clear_keys(monkeypatch)
    monkeypatch.setenv("PM_EVALUATOR_API_KEY", "sk-judge")
    assert _gate() is False
    err = capsys.readouterr().err
    assert "缺少这些角色的 API Key" in err
    assert "clarifier" in err and "target" in err
    assert "evaluator、" not in err, "已配的角色不该出现在缺失清单里"


def test_only_target_key_is_no_longer_a_free_pass(monkeypatch, capsys) -> None:
    """只配 target 一个角色 Key：其它角色必然在构造时失败，闸门要提前说清楚。"""
    _clear_keys(monkeypatch)
    monkeypatch.setenv("PM_TARGET_API_KEY", "sk-target")
    assert _gate() is False
    assert "缺少这些角色的 API Key" in capsys.readouterr().err


def test_fake_backend_still_bypasses_the_gate(monkeypatch, capsys) -> None:
    _clear_keys(monkeypatch)
    monkeypatch.setenv("PM_FAKE_BACKEND", "progress")
    assert _gate() is True
    assert "演示模式" in capsys.readouterr().err
