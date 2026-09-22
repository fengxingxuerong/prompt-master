"""角色与智能体注册：角色设定固定 + 任意智能体统一接入。

- 角色（roles）：系统提示词集中存放在 config/roles.json，**运行期只读**。
  任何模型切换都不改角色内容——"角色固定、模型可换"是本项目的核心约束。
  未注册角色引用 → ConfigError 显式报错（不静默用空人设）。
- 智能体（agents）：任何框架（LangChain / openai SDK / 原生 HTTP / Coze / Dify …）
  按统一方式注册后接入；ModelHub 转发时带 agent 标签，台账可按智能体检索。
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any

from .pool import ConfigError, _load_json, _project_root


def _roles_path() -> Path:
    return Path(os.getenv("PMH_ROLES", str(_project_root() / "config" / "roles.json")))


class AgentRegistry:
    """角色设定 + 智能体注册（线程安全，roles 文件变更自动重载）。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._roles: dict[str, dict[str, Any]] = {}
        self._roles_mtime: float = 0.0
        self._agents: dict[str, dict[str, Any]] = {}
        self._load_roles(force=True)

    # ---- 角色 ----
    def _load_roles(self, force: bool = False) -> None:
        with self._lock:
            path = _roles_path()
            try:
                mtime = path.stat().st_mtime
            except OSError as e:
                raise ConfigError(f"角色设定文件不可访问：{path}：{e}") from e
            if force or mtime != self._roles_mtime:
                data = _load_json(path)
                roles = data.get("roles")
                if not isinstance(roles, dict) or not roles:
                    raise ConfigError(f"角色设定文件缺少非空的 roles 对象：{path}")
                for name, spec in roles.items():
                    if not isinstance(spec, dict) or not str(spec.get("system_prompt", "")).strip():
                        raise ConfigError(
                            f"角色 {name} 缺少非空 system_prompt：{path}（角色设定必须固定且显式）"
                        )
                self._roles = roles
                self._roles_mtime = mtime

    def reload_roles(self) -> None:
        self._load_roles(force=True)

    def role_names(self) -> list[str]:
        with self._lock:
            self._load_roles()
            return sorted(self._roles.keys())

    def get_role(self, name: str) -> dict[str, Any]:
        """取角色设定；不存在直接 ConfigError（绝不静默回退空人设）。"""
        with self._lock:
            self._load_roles()
            role = self._roles.get(name)
            if role is None:
                raise ConfigError(
                    f"未知角色：{name}。可用角色：{', '.join(sorted(self._roles.keys()))}。"
                    f"新角色请在 config/roles.json 中固定后使用。"
                )
            return dict(role)

    def render_system_prompt(self, name: str, variables: dict[str, str] | None = None) -> str:
        """渲染角色系统提示词。变量替换仅支持 {{key}} 白名单式占位；
        角色正文（人设部分）不受变量影响——固定的部分永远固定。"""
        role = self.get_role(name)
        prompt = str(role["system_prompt"])
        for k, v in (variables or {}).items():
            prompt = prompt.replace("{{" + k + "}}", str(v))
        return prompt

    # ---- 智能体 ----
    def register_agent(
        self,
        name: str,
        *,
        framework: str = "unknown",
        default_role: str | None = None,
        default_model: str | None = None,
        description: str = "",
    ) -> dict[str, Any]:
        """注册（或更新）一个智能体。注册是幂等的：同名覆盖。

        任意框架的智能体都通过这同一动作接入；default_role 让智能体
        绑定一个固定角色人设（也可在每次请求里显式指定 role）。
        """
        if not name or not name.strip():
            raise ConfigError("智能体名称不能为空")
        name = name.strip()
        if default_role is not None:
            self.get_role(default_role)  # 不存在直接报错
        with self._lock:
            self._agents[name] = {
                "name": name,
                "framework": framework,
                "default_role": default_role,
                "default_model": default_model,
                "description": description,
                "registered_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            return dict(self._agents[name])

    def get_agent(self, name: str) -> dict[str, Any] | None:
        with self._lock:
            agent = self._agents.get(name)
            return dict(agent) if agent else None

    def list_agents(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(v) for _, v in sorted(self._agents.items())]

    def unregister_agent(self, name: str) -> bool:
        with self._lock:
            return self._agents.pop(name, None) is not None


_REGISTRY: AgentRegistry | None = None
_REGISTRY_LOCK = threading.Lock()


def get_registry() -> AgentRegistry:
    global _REGISTRY
    with _REGISTRY_LOCK:
        if _REGISTRY is None:
            _REGISTRY = AgentRegistry()
        return _REGISTRY
