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

# 跨进程锁与原子写是**通用**设施：usage_store 出过同族事故（2026-10-15），
# 所以两处共用 `filelock` 这一份实现，别在这里再抄一遍。
from .filelock import _atomic_write, _process_lock
from .pool import ConfigError, _project_root


def _data_dir() -> Path:
    override = (os.getenv("PMH_DATA_DIR") or "").strip()
    return Path(override) if override else _project_root() / "data"


def _vkeys_path() -> Path:
    return _data_dir() / "vkeys.json"


# --- 写者之间的互斥 -------------------------------------------------------
# 每个 store 实例原本各有一把 self._lock：同进程里两个实例（生产用 get_vkey_store()
# 单例，但签发方与校验方各自新建实例、测试也各建各的）互相不排斥。
# 实测把两件事分开量过，别混着说：
#   * 主因是 `_reload` 的 mtime 门（同刻写看不见）—— 去掉"写前强制重读"后，
#     两实例各发 100 把会当场丢（`test_two_store_instances_in_one_process_do_not_lose_keys`
#     对这一条 3/3 红）。
#   * 共享路径锁关掉的是"读→写"之间剩下那段窗口。只把锁换回每实例一把、
#     保留强制重读时，本机这几轮**没有**复现丢失（窗口只有约 200µs）——
#     所以它是补完，不是那条红的主证据；别把它记成"修好它的原因"。
# 跨进程那一层由 `_process_lock` 管，它有真证据：摘掉之后两个子进程各发 60 把，
# 3/3 轮盘上都不止差一把。
_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def _path_lock(path: Path) -> threading.RLock:
    # 只用纯路径运算做键：`path.resolve()` 在"文件刚被创建"前后会给出不同结果，
    # 那会让同一个文件先后落到两把锁上 —— 正好把要关的门又打开。
    key = os.path.abspath(str(path))
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = _LOCKS[key] = threading.RLock()
        return lock


def _agents_path() -> Path:
    return _data_dir() / "agents.json"


def _load_json_or(path: Path, default: dict[str, Any], what: str) -> dict[str, Any]:
    """读一份 JSON 台账。Windows 上 `os.replace` 进行中会瞬时拒绝读，所以先退避几次。

    原来一次 `PermissionError` 就判"不可读"并抛错，症状是并发时**合法请求被打死**
    （实测两个进程各发 150 把，3 轮里 2 轮有一路直接崩在这里）。
    """
    if not path.exists():
        return default
    text: str | None = None
    last: OSError | None = None
    for attempt in range(5):
        try:
            text = path.read_text(encoding="utf-8")
            break
        except PermissionError as e:  # 别人的 replace 正在飞：让一下，别把瞬时占用当损坏
            last = e
            time.sleep(0.02 * (attempt + 1))
        except OSError as e:
            raise ConfigError(f"{what}不可读：{path}：{e}") from e
    if text is None:
        assert last is not None
        raise ConfigError(f"{what}不可读（重试 5 次仍被占用）：{path}：{last}") from last
    if not text.strip():
        return default
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ConfigError(f"{what}损坏（不是合法 JSON）：{path}：第 {e.lineno} 行 {e.msg}") from e
    if not isinstance(data, dict):
        raise ConfigError(f"{what}顶层必须是 JSON 对象：{path}")
    return data


class VirtualKeyStore:
    """虚拟密钥库（线程安全，落盘持久，mtime 热载）。

    mtime 热载只在**读**路径上生效。写路径必须强制重读：本机实测文件系统时间戳粒度
    约 0.3~0.5ms，连续两次写有 78~84% 落在同一格里（`st_mtime` 完全不动），
    于是"看 mtime 变了才重读"会让后一个写者拿着**过期快照**整份覆盖回去——
    表现是密钥发出去了、盘上没有；两个 store 实例并发签发时还会撞同一个 `.tmp` 文件名，
    `os.replace` 直接抛 `PermissionError(13)`（见 `_atomic_write`）。
    """

    def __init__(self) -> None:
        self._keys: dict[str, dict[str, Any]] = {}
        self._mtime: float = -1.0
        self._reload()

    @staticmethod
    def _guard() -> threading.RLock:
        """同一路径上的所有写者共用一把 RLock（原因见 `_path_lock` 上方注释）。"""
        return _path_lock(_vkeys_path())

    def _reload(self, force: bool = False) -> None:
        with self._guard():
            path = _vkeys_path()
            try:
                mtime = path.stat().st_mtime
            except OSError:
                mtime = -1.0
            if force or mtime != self._mtime:
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
        with self._guard(), _process_lock(_vkeys_path()):
            self._reload(force=True)  # 写路径：别用可能过期的快照覆盖回去
            self._keys[vk] = rec
            _atomic_write(_vkeys_path(), {"version": 1, "keys": self._keys})
            self._mtime = _vkeys_path().stat().st_mtime
            return dict(rec)

    def verify(self, key: str) -> dict[str, Any] | None:
        """校验虚拟密钥：存在且启用 → 返回记录；否则 None。

        查不到时强制重读一次再判 None：mtime 粒度可能让这里"看不见别人刚写的密钥"，
        而这是鉴权路径——假阴性的代价是把合法请求 401 掉（合法 Key 被判无效最难查）。
        真不存在的 Key 只是多读一次文件，且第二次仍按同一口径判 None。
        """
        if not key or not key.startswith("vk-"):
            return None
        with self._guard():
            self._reload()
            rec = self._keys.get(key)
            if rec is None or not rec.get("enabled", False):
                self._reload(force=True)  # 假阴性只可能来自过期快照：判 None 之前再看一次
                rec = self._keys.get(key)
            if rec is None or not rec.get("enabled", False):
                return None
            return dict(rec)

    def list(self) -> list[dict[str, Any]]:
        with self._guard():
            self._reload()
            return [dict(v) for _, v in sorted(self._keys.items())]

    def set_enabled(self, key: str, enabled: bool) -> dict[str, Any]:
        with self._guard(), _process_lock(_vkeys_path()):
            self._reload(force=True)  # 写路径：同上
            rec = self._keys.get(key)
            if rec is None:
                raise ConfigError(f"虚拟密钥不存在：{key[:10]}…")
            rec["enabled"] = bool(enabled)
            _atomic_write(_vkeys_path(), {"version": 1, "keys": self._keys})
            self._mtime = _vkeys_path().stat().st_mtime
            return dict(rec)

    def delete(self, key: str) -> bool:
        with self._guard(), _process_lock(_vkeys_path()):
            self._reload(force=True)  # 写路径：同上（"找不到就返回 False"也不能建立在过期快照上）
            if key not in self._keys:
                return False
            del self._keys[key]
            _atomic_write(_vkeys_path(), {"version": 1, "keys": self._keys})
            self._mtime = _vkeys_path().stat().st_mtime
            return True


class AgentStore:
    """智能体注册持久化（mtime 热载；修 K-4）。"""

    def __init__(self) -> None:
        self._agents: dict[str, dict[str, Any]] = {}
        self._mtime: float = -1.0
        self._reload()

    @staticmethod
    def _guard() -> threading.RLock:
        """与 VirtualKeyStore 同理：锁按文件路径共享，不按实例。"""
        return _path_lock(_agents_path())

    def _reload(self, force: bool = False) -> None:
        with self._guard():
            path = _agents_path()
            try:
                mtime = path.stat().st_mtime
            except OSError:
                mtime = -1.0
            if force or mtime != self._mtime:
                data = _load_json_or(path, {"version": 1, "agents": {}}, "智能体注册表")
                agents = data.get("agents")
                if not isinstance(agents, dict):
                    raise ConfigError(f"智能体注册表 agents 必须是对象：{path}")
                self._agents = agents
                self._mtime = mtime

    def register(
        self,
        name: str,
        *,
        framework: str = "unknown",
        default_role: str | None = None,
        default_model: str | None = None,
        description: str = "",
    ) -> dict[str, Any]:
        if not name or not name.strip():
            raise ConfigError("智能体名称不能为空")
        name = name.strip()
        with self._guard(), _process_lock(_agents_path()):
            self._reload(force=True)  # 写路径：与 VirtualKeyStore 同一条理由
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
        with self._guard():
            self._reload()
            rec = self._agents.get(name)
            if rec is None:
                self._reload(force=True)  # 同 verify：判"没有"之前先看一眼是不是快照过期
                rec = self._agents.get(name)
            return dict(rec) if rec else None

    def list(self) -> list[dict[str, Any]]:
        with self._guard():
            self._reload()
            return [dict(v) for _, v in sorted(self._agents.items())]

    def unregister(self, name: str) -> bool:
        with self._guard(), _process_lock(_agents_path()):
            self._reload(force=True)  # 写路径：与 VirtualKeyStore 同一条理由
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
