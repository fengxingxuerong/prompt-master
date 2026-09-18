"""
本地结果缓存（P1 工程项）。

解决什么问题：
- 断点续跑（SQLite checkpoint 恢复）时，相同 prompt 版本 + 相同测试集会被
  全量重新调用 LLM —— 评估结果与目标模型输出其实没有变化，纯属浪费 API 预算。
- 缓存把「prompt 版本 → 目标输出 → 评估结果」的完整链路结果按内容哈希落盘，
  恢复后直接命中，跳过 LLM 调用。

缓存边界（重要）：
- 评估结果缓存依赖 test_output，因此只有当「prompt + 测试输入 + 目标输出」
  三者完全一致时才命中 —— 目标模型非确定性输出会让缓存自然失效，这是期望行为
  （缓存的是「已发生的事实」，而不是「预测」）。
- target 输出缓存依赖「prompt + 测试输入 + 目标模型」：命中意味着本轮直接复用
  上一轮的目标输出。对「同一 prompt 版本重新跑」的断点续跑场景这正是期望行为；
  但要注意它会让目标模型的非确定性被冻结 —— 需要真实变化时清空缓存即可。

存储：默认 JSON 文件（logs/eval_cache.json + logs/target_cache.json），线程安全，
容量上限 + 插入序淘汰。多 worker 部署（--workers > 1）时设
`PM_CACHE_BACKEND=sqlite` 换用 SQLite 共享缓存（logs/*_cache.db）：
多个 worker 进程读写同一份缓存文件，命中率不再随 worker 数下降，
也不会出现"各进程各持一份、互相覆盖"的重复烧钱问题。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any

from . import backend

logger = logging.getLogger("pm.cache")

# 默认缓存目录（可通过 PM_CACHE_DIR 覆盖）
DEFAULT_CACHE_DIR = Path(__file__).resolve().parent.parent / "logs"


def env_cache_dir() -> Path:
    return Path(os.getenv("PM_CACHE_DIR", str(DEFAULT_CACHE_DIR)))


def env_cache_backend() -> str:
    """缓存存储后端：json（默认）| sqlite（多 worker 共享）。非法值回退 json。"""
    raw = os.getenv("PM_CACHE_BACKEND", "json").strip().lower()
    if raw not in ("json", "sqlite"):
        logger.warning("PM_CACHE_BACKEND=%r 无效（可选 json/sqlite），回退默认 json", raw)
        return "json"
    return raw


def env_cache_enabled(which: str) -> bool:
    """缓存开关：PM_EVAL_CACHE / PM_TARGET_CACHE，默认开启。

    另外：处于注入的假后端上下文（自测 / 演示模式）时一律禁用，
    避免污染 `logs/*_cache.json` —— 旧版靠改 `os.environ` 实现，那个副作用永不还原（C4）。
    """
    if backend.cache_disabled():
        return False
    return os.getenv(f"PM_{which.upper()}_CACHE", "1").strip() in ("1", "true", "yes")


def _hash(*parts: str) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(p.encode("utf-8", errors="replace"))
        h.update(b"\x00")
    return h.hexdigest()[:40]


def key_for_eval(
    prompt: str, test_input: str, test_output: str, judge_spec: str, extra: str = ""
) -> str:
    """评估结果缓存键：被测输出变了就自然失效。

    `extra` 必须带上原始需求 / 上下文 / 质量警告（旧版没带：不同 task 只要
    prompt+输入+输出相同就会串用同一份评估结果）。
    """
    return _hash("eval", prompt, test_input, test_output, judge_spec, extra)


def key_for_target(prompt: str, test_input: str, target_model: str, extra: str = "") -> str:
    """目标模型输出缓存键。

    光靠 `target_model` 字符串不够（那只是写进 prompt 的标签）：`extra` 应传入
    实际生效的配置指纹（模型 / 端点 / 采样参数），否则换模型、改温度后旧缓存仍命中（H1）。
    """
    return _hash("target", prompt, test_input, target_model, extra)


class JsonCache:
    """简单 JSON 文件缓存：线程安全 + 容量上限 + 插入序淘汰。"""

    def __init__(self, path: Path, max_entries: int = 200):
        self.path = Path(path)
        self.max_entries = max(1, int(max_entries))
        self._lock = threading.Lock()
        self._data: dict[str, Any] = {}
        self._load()

    def _load(self) -> None:
        try:
            if self.path.exists():
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                self._data = raw if isinstance(raw, dict) else {}
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("缓存文件 %s 读取失败，按空缓存继续：%s", self.path, e)
            self._data = {}

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # 临时名带 pid：多进程（uvicorn --workers）共用同一缓存文件时，固定 .tmp 会互相覆盖
            tmp = Path(f"{self.path}.{os.getpid()}.tmp")
            tmp.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
            tmp.replace(self.path)
        except OSError as e:
            logger.warning("缓存写入失败（不影响主流程）：%s", e)

    def get(self, key: str) -> Any | None:
        with self._lock:
            return self._data.get(key)

    def put(self, key: str, value: Any) -> None:
        with self._lock:
            if key in self._data:
                self._data[key] = value
                # 更新也要落盘：旧版这里直接 return，导致“只更新内存、重启后回到旧值”
                self._save()
                return
            if len(self._data) >= self.max_entries:
                # 淘汰最旧的 1/4，避免无限膨胀（dict 保持插入序）
                for old in list(self._data.keys())[: max(1, self.max_entries // 4)]:
                    self._data.pop(old, None)
            self._data[key] = value
            self._save()

    def clear(self) -> None:
        with self._lock:
            self._data = {}
            try:
                if self.path.exists():
                    self.path.unlink()
            except OSError as e:
                logger.warning("清除缓存文件失败：%s", e)

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)


class SqliteCache:
    """SQLite 共享缓存：多 worker 部署时多个进程读写同一份缓存文件。

    为什么需要它（README「已知限制」的债）：JsonCache 是"每进程一份内存态 +
    落盘覆盖"，`--workers > 1` 时各 worker 各持一份 `logs/*_cache.json`，
    命中率下降、且互相覆盖丢失条目 —— 记录层早已共享（PM_TASK_DB → SQLite），
    缓存层却还各管各的。本类把缓存也收敛到同一份 SQLite 文件。

    与 JsonCache 的差异（接口完全一致：get/put/clear/__len__）：
    - 存储走 SQLite 表，WAL 模式 + 进程内锁 + check_same_thread=False
      （与 pm/store.py 的 SqliteStore 同款模式，多进程读写不互斥）；
    - `seq` 自增主键当插入序：更新已有键不动 seq（淘汰顺序不被刷新打乱），
      淘汰最旧的 1/4（与 JsonCache 的容量语义一致）；
    - 值经 JSON 序列化落 TEXT（default=str 降级，与 JsonCache 落盘口径一致）。

    注意：SQLite 是文件级锁，同一文件被多进程访问时由 SQLite 自己保证事务
    原子性；进程内多线程由 self._lock 串行化。
    """

    def __init__(self, path: Path, max_entries: int = 200):
        self.path = Path(path)
        self.max_entries = max(1, int(max_entries))
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS cache_entries (
                    seq   INTEGER PRIMARY KEY AUTOINCREMENT,
                    key   TEXT NOT NULL UNIQUE,
                    value TEXT NOT NULL
                )
                """
            )
            self._conn.commit()

    def get(self, key: str) -> Any | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM cache_entries WHERE key=?", (key,)
            ).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row[0])
        except (json.JSONDecodeError, TypeError) as e:
            logger.warning("缓存条目 %s 解析失败，按未命中处理：%s", key, e)
            return None

    def put(self, key: str, value: Any) -> None:
        payload = json.dumps(value, ensure_ascii=False, default=str)
        with self._lock:
            try:
                # 更新已有键：只改值不动 seq（保持插入序，淘汰语义与 JsonCache 一致）
                cur = self._conn.execute(
                    "UPDATE cache_entries SET value=? WHERE key=?", (payload, key)
                )
                if cur.rowcount == 0:
                    self._evict_locked()
                    self._conn.execute(
                        "INSERT INTO cache_entries (key, value) VALUES (?,?)", (key, payload)
                    )
                self._conn.commit()
            except sqlite3.Error as e:
                logger.warning("缓存写入失败（不影响主流程）：%s", e)

    def _evict_locked(self) -> None:
        """容量检查 + 淘汰最旧的 1/4（调用方需已持有锁）。"""
        row = self._conn.execute("SELECT COUNT(*) FROM cache_entries").fetchone()
        count = int(row[0]) if row else 0
        if count < self.max_entries:
            return
        drop = max(1, self.max_entries // 4)
        self._conn.execute(
            "DELETE FROM cache_entries WHERE seq IN "
            "(SELECT seq FROM cache_entries ORDER BY seq ASC LIMIT ?)",
            (drop,),
        )

    def clear(self) -> None:
        with self._lock:
            try:
                self._conn.execute("DELETE FROM cache_entries")
                self._conn.commit()
            except sqlite3.Error as e:
                logger.warning("清除缓存失败（不影响主流程）：%s", e)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __len__(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) FROM cache_entries").fetchone()
        return int(row[0]) if row else 0


# --------------------------------------------------------------------------
# 全局单例（进程内共享；路径与后端由环境变量决定）
# --------------------------------------------------------------------------
_eval_cache: Any | None = None
_target_cache: Any | None = None
_cache_lock = threading.Lock()


def _build_cache(which: str) -> Any:
    """按 PM_CACHE_BACKEND 构造缓存实例：json（默认）| sqlite（多 worker 共享）。"""
    if env_cache_backend() == "sqlite":
        return SqliteCache(env_cache_dir() / f"{which}_cache.db")
    return JsonCache(env_cache_dir() / f"{which}_cache.json")


def eval_cache() -> Any | None:
    """评估结果缓存单例；PM_EVAL_CACHE=0 时返回 None（完全禁用）。"""
    global _eval_cache
    if not env_cache_enabled("eval"):
        return None
    with _cache_lock:
        if _eval_cache is None:
            _eval_cache = _build_cache("eval")
        return _eval_cache


def target_cache() -> Any | None:
    """目标模型输出缓存单例；PM_TARGET_CACHE=0 时返回 None（完全禁用）。"""
    global _target_cache
    if not env_cache_enabled("target"):
        return None
    with _cache_lock:
        if _target_cache is None:
            _target_cache = _build_cache("target")
        return _target_cache
