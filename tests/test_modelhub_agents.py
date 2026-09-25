"""角色设定与智能体注册（`pm/modelhub/agents.py`）—— 进程内、零出网。

这块之前**一条用例都没有**：`grep -rln AgentRegistry tests/` 是空的，56% 的数字全部来自
`import pm.modelhub.server` 时顺手构造了一遍注册表。也就是说这个模块写在 docstring 里的
三条承诺（角色文件不可访问要显式报错、未注册角色绝不静默回退空人设、注册幂等）
在流水线里没人看过一眼 —— 而它们是"任意智能体统一接入"这道门的实际语义。

这里盯的是**失败方式**：这类注册表坏了不会崩，只会静默把角色换成空的、
把同名注册变成两条，然后台账里多出一些说不清来源的记录。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from pm.modelhub import agents as AG
from pm.modelhub.pool import ConfigError


def _roles_file(tmp_path: Path, roles: dict[str, Any]) -> Path:
    p = tmp_path / "roles.json"
    p.write_text(json.dumps({"roles": roles}, ensure_ascii=False), encoding="utf-8")
    return p


@pytest.fixture(autouse=True)
def roles_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Any:  # 返回一个 lambda：写一份角色文件并返回它
    """把 `PMH_ROLES` 钉在 tmp 上：默认值指向仓库里那份真配置，测试不该碰它。"""

    def write(roles: dict[str, Any]) -> Path:
        p = _roles_file(tmp_path, roles)
        monkeypatch.setenv("PMH_ROLES", str(p))
        return p

    return write


def test_unreadable_roles_file_is_a_config_error_not_an_empty_registry(
    tmp_path: Path,
) -> None:
    """文件不存在/不可读时必须报错。回退成"零角色但能跑"会让每条请求都变成未知角色。"""
    missing = tmp_path / "nope" / "roles.json"
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("PMH_ROLES", str(missing))
        with pytest.raises(ConfigError) as e:
            AG.AgentRegistry()
    assert "不可访问" in str(e.value) and str(missing) in str(e.value)


@pytest.mark.parametrize(
    "payload",
    [
        {},  # 整个文件没 roles 键
        {"roles": {}},  # 有键但是空的
        {"roles": []},  # 形状不对
        {"roles": None},
    ],
)
def test_missing_or_empty_roles_object_is_refused(payload: dict[str, Any], tmp_path: Path) -> None:
    p = tmp_path / "roles.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("PMH_ROLES", str(p))
        with pytest.raises(ConfigError) as e:
            AG.AgentRegistry()
    assert "非空的 roles 对象" in str(e.value)


def test_role_without_a_prompt_names_the_offender(roles_env: Any) -> None:
    """空白 system_prompt 也算缺：一个只有名字的人设比没有更坏（看起来配好了）。"""
    roles_env({"writer": {"system_prompt": "写"}, "ghost": {"system_prompt": "   "}})
    with pytest.raises(ConfigError) as e:
        AG.AgentRegistry()
    assert "ghost" in str(e.value) and "system_prompt" in str(e.value)


def test_unknown_role_error_lists_what_actually_exists(roles_env: Any) -> None:
    """打错角色名时把可用清单给出来 —— 这是"绝不静默回退空人设"的第二半。"""
    roles_env({"writer": {"system_prompt": "写"}, "judge": {"system_prompt": "评"}})
    reg = AG.AgentRegistry()
    assert reg.role_names() == ["judge", "writer"]  # 排序稳定，报错文案可预期
    with pytest.raises(ConfigError) as e:
        reg.get_role("writre")
    msg = str(e.value)
    assert "writer" in msg and "judge" in msg


def test_roles_hot_reload_when_the_file_changes(roles_env: Any) -> None:
    """运行期只读的是"内容不被程序改"，不是"改文件必须重启"：mtime 变了就要看到。"""
    p = roles_env({"writer": {"system_prompt": "写"}})
    reg = AG.AgentRegistry()
    assert reg.role_names() == ["writer"]
    p.write_text(
        json.dumps(
            {"roles": {"writer": {"system_prompt": "写"}, "arbiter": {"system_prompt": "裁"}}}
        ),
        encoding="utf-8",
    )
    os.utime(p, (p.stat().st_atime, p.stat().st_mtime + 2))  # 文件系统时间戳可能同秒
    assert reg.role_names() == ["arbiter", "writer"]


def test_reload_roles_is_the_explicit_escape_hatch(roles_env: Any) -> None:
    """mtime 没动（同一时间戳内改完 / 时钟回拨 / 网络盘不刷新）时，自动重载会瞎；
    `reload_roles()` 是运维手里的显式那条路，必须真的强制重读而不是走同一个判断。"""
    p = roles_env({"writer": {"system_prompt": "写"}})
    reg = AG.AgentRegistry()
    before = (p.stat().st_atime, p.stat().st_mtime)
    p.write_text(
        json.dumps({"roles": {"writer": {"system_prompt": "写"}, "ops": {"system_prompt": "维"}}}),
        encoding="utf-8",
    )
    os.utime(p, before)  # 把时间戳按回去：模拟"改了但 mtime 没变"
    assert reg.role_names() == ["writer"], "mtime 未变时不该重读（这是设计，不是漏）"
    reg.reload_roles()
    assert reg.role_names() == ["ops", "writer"]


def test_render_keeps_unknown_placeholders_literal(roles_env: Any) -> None:
    """白名单式替换：没给的变量留着原样，而不是留下一个洞或被 format 炸掉。"""
    roles_env({"writer": {"system_prompt": "城市 {{city}}｜未传的 {{other}} 与 {brace}"}})
    reg = AG.AgentRegistry()
    out = reg.render_system_prompt("writer", {"city": "上海"})
    assert "上海" in out and "{{other}}" in out and "{brace}" in out
    assert reg.render_system_prompt("writer") == "城市 {{city}}｜未传的 {{other}} 与 {brace}"


def test_register_agent_refuses_blank_name_and_unknown_role(roles_env: Any) -> None:
    roles_env({"writer": {"system_prompt": "写"}})
    reg = AG.AgentRegistry()
    for bad in ("", "   "):
        with pytest.raises(ConfigError) as e:
            reg.register_agent(bad)
        assert "不能为空" in str(e.value)
    with pytest.raises(ConfigError) as e:
        reg.register_agent("bot", default_role="ghost")
    assert "未知角色" in str(e.value)
    assert reg.get_agent("bot") is None, "绑定不存在的角色时不该留下半成品注册项"


def test_registration_is_idempotent_and_hands_out_copies(roles_env: Any) -> None:
    """同名覆盖是"注册幂等"的实现方式；返回副本是"运行期只读"的实现方式。"""
    roles_env({"writer": {"system_prompt": "写"}, "judge": {"system_prompt": "评"}})
    reg = AG.AgentRegistry()
    reg.register_agent("bot", framework="langchain", default_role="writer")
    again = reg.register_agent("bot", framework="native-http", default_role="judge")
    assert len(reg.list_agents()) == 1
    assert again["framework"] == "native-http" and again["default_role"] == "judge"
    assert reg.get_agent("bot")["framework"] == "native-http"

    # 从**取值接口**拿到的副本才能证明"只读"：改 `register_agent` 的返回值不算
    got = reg.get_agent("bot")
    assert got is not reg.get_agent("bot"), "get_agent 把活对象直接交出去了"
    got["framework"] = "mutated-through-return-value"
    assert reg.get_agent("bot")["framework"] == "native-http"
    reg.list_agents()[0]["name"] = "hijacked"
    assert [a["name"] for a in reg.list_agents()] == ["bot"]
    role = reg.get_role("writer")
    role["system_prompt"] = "tampered"
    assert reg.get_role("writer")["system_prompt"] == "写"
    assert reg.render_system_prompt("writer") == "写"


def test_unregister_reports_whether_anything_was_removed(roles_env: Any) -> None:
    roles_env({"writer": {"system_prompt": "写"}})
    reg = AG.AgentRegistry()
    reg.register_agent("bot")
    assert reg.unregister_agent("bot") is True
    assert reg.unregister_agent("bot") is False


def test_get_registry_is_a_process_singleton(
    roles_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """服务端每次请求都调 `get_registry()`：每请求重读配置文件的话，
    角色文件坏一次就会把整条链路打成 500。这里只锁"同一个对象"这一条性质。"""
    roles_env({"writer": {"system_prompt": "写"}})
    monkeypatch.setattr(AG, "_REGISTRY", None)
    assert AG.get_registry() is AG.get_registry()
    assert AG.get_registry().role_names() == ["writer"]
