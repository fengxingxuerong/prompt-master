"""`pm/cache.py` 的落盘/淘汰/清理异常分支（覆盖率洼地补齐）。

背景：真实故障高发但平时不跑的路径——写盘失败（磁盘满/权限）、
更新已有键（旧版只改内存不落盘，重启回退）、容量淘汰、清理时的 unlink 异常。
单例开关（PM_EVAL_CACHE / PM_TARGET_CACHE / 演示后端禁用）也在这里钉住。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pm import backend
from pm import cache as C
from pm.testing import scope


@pytest.fixture()
def isolated_cache(tmp_path: Path) -> C.JsonCache:
    return C.JsonCache(tmp_path / "sub" / "cache.json", max_entries=4)


def test_put_and_get_round_trip(isolated_cache: C.JsonCache):
    isolated_cache.put("k", {"v": 1})
    assert isolated_cache.get("k") == {"v": 1}
    assert len(isolated_cache) == 1
    assert isolated_cache.get("missing") is None


def test_update_existing_key_is_persisted(isolated_cache: C.JsonCache):
    """旧版更新直接 return，只改内存不落盘 → 重启回到旧值（本次补的分支）。"""
    isolated_cache.put("k", 1)
    isolated_cache.put("k", 2)
    assert isolated_cache.get("k") == 2
    on_disk = json.loads(isolated_cache.path.read_text(encoding="utf-8"))
    assert on_disk["k"] == 2


def test_eviction_when_capacity_reached(isolated_cache: C.JsonCache):
    """超过容量淘汰最旧的 1/4，避免缓存文件无限膨胀。"""
    for i in range(4):
        isolated_cache.put(f"k{i}", i)
    assert len(isolated_cache) == 4
    isolated_cache.put("k4", 4)  # 触发淘汰
    assert len(isolated_cache) <= 4
    assert isolated_cache.get("k4") == 4  # 新键必须在
    assert isolated_cache.get("k0") is None  # 最旧的被淘汰


def test_save_failure_does_not_raise(tmp_path: Path, monkeypatch):
    """写盘失败只告警：缓存是优化不是依赖，绝不能把主流程带崩。"""
    cache = C.JsonCache(tmp_path / "cache.json")

    def boom(*_a, **_kw):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "write_text", boom)
    cache.put("k", 1)  # 不应抛异常
    assert cache.get("k") == 1  # 内存里仍然生效


def test_corrupted_cache_file_is_ignored(tmp_path: Path):
    p = tmp_path / "cache.json"
    p.write_text("{ not json", encoding="utf-8")
    cache = C.JsonCache(p)
    assert len(cache) == 0
    cache.put("k", 1)  # 仍可正常写入
    assert cache.get("k") == 1


def test_non_dict_cache_payload_is_ignored(tmp_path: Path):
    p = tmp_path / "cache.json"
    p.write_text("[1, 2, 3]", encoding="utf-8")
    assert len(C.JsonCache(p)) == 0


def test_clear_removes_file_and_survives_unlink_error(tmp_path: Path, monkeypatch):
    cache = C.JsonCache(tmp_path / "cache.json")
    cache.put("k", 1)
    cache.clear()
    assert len(cache) == 0
    assert not cache.path.exists()

    cache.put("k", 2)

    def boom(*_a, **_kw):
        raise OSError("locked")

    monkeypatch.setattr(Path, "unlink", boom)
    cache.clear()  # 不应抛异常
    assert len(cache) == 0


def test_singletons_respect_env_switches(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("PM_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("PM_EVAL_CACHE", "0")
    monkeypatch.setenv("PM_TARGET_CACHE", "0")
    monkeypatch.setattr(C, "_eval_cache", None)
    monkeypatch.setattr(C, "_target_cache", None)
    assert C.eval_cache() is None
    assert C.target_cache() is None

    monkeypatch.setenv("PM_EVAL_CACHE", "1")
    monkeypatch.setenv("PM_TARGET_CACHE", "1")
    assert C.eval_cache() is not None
    assert C.target_cache() is not None
    # 单例：第二次调用返回同一对象（而不是每次新建）
    assert C.eval_cache() is C.eval_cache()


def test_cache_disabled_under_demo_backend(tmp_path: Path, monkeypatch):
    """演示/自测上下文里一律禁用缓存，避免污染 logs/*_cache.json（C4）。"""
    monkeypatch.setenv("PM_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("PM_EVAL_CACHE", "1")
    monkeypatch.setattr(C, "_eval_cache", None)
    with scope("progress"):
        assert C.eval_cache() is None
    assert backend.cache_disabled() is False  # 作用域退出后恢复正常


def test_cache_dir_override(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("PM_CACHE_DIR", str(tmp_path))
    assert C.env_cache_dir() == tmp_path


# --------------------------------------------------------------------------
# SqliteCache（多 worker 共享后端，2026-09-18 新增）
# --------------------------------------------------------------------------
def test_sqlite_cache_round_trip(tmp_path: Path):
    """SqliteCache 与 JsonCache 同接口：get/put/len/clear 语义一致。"""
    cache = C.SqliteCache(tmp_path / "eval_cache.db", max_entries=4)
    cache.put("k", {"v": 1})
    assert cache.get("k") == {"v": 1}
    assert len(cache) == 1
    assert cache.get("missing") is None
    # 更新已有键：值覆盖、seq 不动
    cache.put("k", {"v": 2})
    assert cache.get("k") == {"v": 2}
    assert len(cache) == 1  # 更新不新增条目
    cache.clear()
    assert len(cache) == 0
    assert cache.get("k") is None
    cache.close()


def test_sqlite_cache_eviction_by_insertion_order(tmp_path: Path):
    """容量淘汰与 JsonCache 同语义：超过上限淘汰最旧的 1/4。"""
    cache = C.SqliteCache(tmp_path / "eval_cache.db", max_entries=4)
    for i in range(4):
        cache.put(f"k{i}", i)
    assert len(cache) == 4
    cache.put("k4", 4)  # 触发淘汰
    assert len(cache) <= 4
    assert cache.get("k4") == 4  # 新键必须在
    assert cache.get("k0") is None  # 最旧的被淘汰
    # 更新已有键不改变插入序：k1 被更新后仍在淘汰候选里
    cache.put("k1", 99)
    for i in range(5, 8):
        cache.put(f"k{i}", i)
    assert cache.get("k1") is None  # 更新未刷新 seq → 仍被淘汰
    cache.close()


def test_sqlite_cache_shared_across_instances(tmp_path: Path):
    """同一 db 文件的多个实例（模拟多 worker）读写同一份缓存。"""
    p = tmp_path / "eval_cache.db"
    a = C.SqliteCache(p, max_entries=8)
    b = C.SqliteCache(p, max_entries=8)
    a.put("shared", {"from": "worker-a"})
    assert b.get("shared") == {"from": "worker-a"}  # B 进程能看到 A 写入
    b.put("second", 2)
    assert a.get("second") == 2  # A 也能看到 B 写入
    a.close()
    b.close()


def test_sqlite_cache_survives_bad_payload(tmp_path: Path):
    """损坏的缓存条目按未命中处理，不抛异常（与 JsonCache 损坏文件同理）。"""
    import sqlite3 as _sqlite3

    p = tmp_path / "eval_cache.db"
    cache = C.SqliteCache(p)
    cache.put("good", 1)
    cache.close()
    # 人为写入非法 JSON
    conn = _sqlite3.connect(str(p))
    conn.execute("INSERT INTO cache_entries (key, value) VALUES (?,?)", ("bad", "{not json"))
    conn.commit()
    conn.close()
    cache = C.SqliteCache(p)
    assert cache.get("bad") is None  # 解析失败 → 未命中
    assert cache.get("good") == 1  # 其余条目不受影响
    cache.close()


def test_sqlite_cache_backend_selected_by_env(monkeypatch, tmp_path: Path):
    """PM_CACHE_BACKEND=sqlite 时单例返回 SqliteCache；默认仍为 JsonCache。"""
    monkeypatch.setattr(C, "_eval_cache", None)
    monkeypatch.setenv("PM_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("PM_EVAL_CACHE", "1")

    monkeypatch.setenv("PM_CACHE_BACKEND", "sqlite")
    assert isinstance(C.eval_cache(), C.SqliteCache)
    # 默认后端不设或非法值时回退 json
    monkeypatch.setattr(C, "_eval_cache", None)
    monkeypatch.setenv("PM_CACHE_BACKEND", "json")
    assert isinstance(C.eval_cache(), C.JsonCache)
    monkeypatch.setattr(C, "_eval_cache", None)
    monkeypatch.setenv("PM_CACHE_BACKEND", "bogus")
    assert isinstance(C.eval_cache(), C.JsonCache)  # 非法值回退默认
