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

存储：JSON 文件（默认 logs/eval_cache.json + logs/target_cache.json），
线程安全（target 并发化后多线程同时读写），容量上限 + 插入序淘汰。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

from . import backend

logger = logging.getLogger("pm.cache")

# 默认缓存目录（可通过 PM_CACHE_DIR 覆盖）
DEFAULT_CACHE_DIR = Path(__file__).resolve().parent.parent / "logs"


def env_cache_dir() -> Path:
    return Path(os.getenv("PM_CACHE_DIR", str(DEFAULT_CACHE_DIR)))


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


# --------------------------------------------------------------------------
# 全局单例（进程内共享；路径由环境变量决定）
# --------------------------------------------------------------------------
_eval_cache: JsonCache | None = None
_target_cache: JsonCache | None = None
_cache_lock = threading.Lock()


def eval_cache() -> JsonCache | None:
    """评估结果缓存单例；PM_EVAL_CACHE=0 时返回 None（完全禁用）。"""
    global _eval_cache
    if not env_cache_enabled("eval"):
        return None
    with _cache_lock:
        if _eval_cache is None:
            _eval_cache = JsonCache(env_cache_dir() / "eval_cache.json")
        return _eval_cache


def target_cache() -> JsonCache | None:
    """目标模型输出缓存单例；PM_TARGET_CACHE=0 时返回 None（完全禁用）。"""
    global _target_cache
    if not env_cache_enabled("target"):
        return None
    with _cache_lock:
        if _target_cache is None:
            _target_cache = JsonCache(env_cache_dir() / "target_cache.json")
        return _target_cache
