"""虚拟密钥与智能体注册持久化（OPT-2 / OPT-5）。

- 虚拟密钥：vk- 前缀密钥，按 agent 签发，落盘 data/vkeys.json；可停用/启用/删除。
  chat 请求带 vk- 密钥 → 校验存在且启用 → 归属对应 agent（台账记 agent）。
  管理员口令（PMH_GATEWAY_TOKEN，未设则开放）用于签发/管理；对话校验只认 vk-。
- 智能体注册持久化：data/agents.json（mtime 热载），修 K-4。

文件损坏/不存在时：密钥库缺失 → 视为空库（首次使用自动创建）；
损坏 → ConfigError 显式报错（不静默清空）。
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any

from .pool import ConfigError, _project_root


def _data_dir() -> Path:
    override = (os.getenv("PMH_DATA_DIR") or "").strip()
    return Path(override) if override else _project_root() / "data"


def _vkeys_path() -> Path:
    return _data_dir() / "vkeys.json"


def _agents_path() -> Path:
    return _data_dir() / "agents.json"


def _load_json_or(path: Path, default: dict, what: str) -> dict:
    if not path.exists():
        return default
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise ConfigError(f"{what}不可读：{path}：{e}") from e
    if not text.strip():
        return default
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ConfigError(f"{what}损坏（不是合法 JSON）：{path}：第 {e.lineno} 行 {e.msg}") from e
    if not isinstance(data, dict):
        raise ConfigError(f"{what}顶层必须是 JSON 对象：{path}")
    return data


def _atomic_write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


class VirtualKeyStore:
    """虚拟密钥库（线程安全，落盘持久，mtime 热载）。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._keys: dict[str, dict[str, Any]] = {}
        self._mtime: float = -1.0
        self._reload()

    def _reload(self) -> None:
        with self._lock:
            path = _vkeys_path()
            try:
                mtime = path.stat().st_mtime
            except OSError:
                mtime = -1.0
            if mtime != self._mtime:
                data = _load_json_or(path, {"version": 1, "keys": {}}, "虚拟密钥库")
                keys = data.get("keys")
                if not isinstance(keys, dict):
                    raise ConfigError(f"虚拟密钥库 keys 必须是对象：{path}")
                self._keys = keys
                self._mtime = mtime

    def issue(self, agent: str, *, note: str = "") -> dict[str, Any]:
        if not agent or not agent.strip():
            raise ConfigError("虚拟密钥必须绑定 agent 名称")
        agent = agent.strip()
        vk = "vk-" + secrets.token_hex(16)
        rec = {
            "key": vk,
            "agent": agent,
            "enabled": True,
            "note": note,
            "issued_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        with self._lock:
            self._reload()
            self._keys[vk] = rec
            _atomic_write(_vkeys_path(), {"version": 1, "keys": self._keys})
            self._mtime = _vkeys_path().stat().st_mtime
            return dict(rec)

    def verify(self, key: str) -> dict[str, Any] | None:
        """校验虚拟密钥：存在且启用 → 返回记录；否则 None。"""
        if not key or not key.startswith("vk-"):
            return None
        with self._lock:
            self._reload()
            rec = self._keys.get(key)
            if rec is None or not rec.get("enabled", False):
                return None
            return dict(rec)

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            self._reload()
            return [dict(v) for _, v in sorted(self._keys.items())]

    def set_enabled(self, key: str, enabled: bool) -> dict[str, Any]:
        with self._lock:
            self._reload()
            rec = self._keys.get(key)
            if rec is None:
                raise ConfigError(f"虚拟密钥不存在：{key[:10]}…")
            rec["enabled"] = bool(enabled)
            _atomic_write(_vkeys_path(), {"version": 1, "keys": self._keys})
            self._mtime = _vkeys_path().stat().st_mtime
            return dict(rec)

    def delete(self, key: str) -> bool:
        with self._lock:
            self._reload()
            if key not in self._keys:
                return False
            del self._keys[key]
            _atomic_write(_vkeys_path(), {"version": 1, "keys": self._keys})
            self._mtime = _vkeys_path().stat().st_mtime
            return True


class AgentStore:
    """智能体注册持久化（mtime 热载；修 K-4）。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._agents: dict[str, dict[str, Any]] = {}
        self._mtime: float = -1.0
        self._reload()

    def _reload(self) -> None:
        with self._lock:
            path = _agents_path()
            try:
                mtime = path.stat().st_mtime
            except OSError:
                mtime = -1.0
            if mtime != self._mtime:
                data = _load_json_or(path, {"version": 1, "agents": {}}, "智能体注册表")
                agents = data.get("agents")
                if not isinstance(agents, dict):
                    raise ConfigError(f"智能体注册表 agents 必须是对象：{path}")
                self._agents = agents
                self._mtime = mtime

    def register(self, name: str, *, framework: str = "unknown", default_role: str | None = None,
                 default_model: str | None = None, description: str = "") -> dict[str, Any]:
        if not name or not name.strip():
            raise ConfigError("智能体名称不能为空")
        name = name.strip()
        with self._lock:
            self._reload()
            self._agents[name] = {
                "name": name,
                "framework": framework,
                "default_role": default_role,
                "default_model": default_model,
                "description": description,
                "registered_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            _atomic_write(_agents_path(), {"version": 1, "agents": self._agents})
            self._mtime = _agents_path().stat().st_mtime
            return dict(self._agents[name])

    def get(self, name: str) -> dict[str, Any] | None:
        with self._lock:
            self._reload()
            rec = self._agents.get(name)
            return dict(rec) if rec else None

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            self._reload()
            return [dict(v) for _, v in sorted(self._agents.items())]

    def unregister(self, name: str) -> bool:
        with self._lock:
            self._reload()
            if name not in self._agents:
                return False
            del self._agents[name]
            _atomic_write(_agents_path(), {"version": 1, "agents": self._agents})
            self._mtime = _agents_path().stat().st_mtime
            return True


_VK: VirtualKeyStore | None = None
_VK_LOCK = threading.Lock()
_AG: AgentStore | None = None
_AG_LOCK = threading.Lock()


def get_vkey_store() -> VirtualKeyStore:
    global _VK
    with _VK_LOCK:
        if _VK is None:
            _VK = VirtualKeyStore()
        return _VK


def get_agent_store() -> AgentStore:
    global _AG
    with _AG_LOCK:
        if _AG is None:
            _AG = AgentStore()
        return _AG
