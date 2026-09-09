"""
并发化 + 本地结果缓存 的回归测试。

覆盖：
- test_node 并发执行：结果按用例序号稳定排序、数量正确、计数正确
- PM_TARGET_MAX_CONCURRENCY=1 退化为串行
- 缓存键的确定性 / 对输入的敏感性
- JsonCache 的读写、淘汰
- evaluate_node 缓存命中后不再调用 LLM
"""

from __future__ import annotations

from pm import cache as cache_mod
from pm import testing
from pm.cache import JsonCache, key_for_eval, key_for_target
from pm.nodes import evaluate_node
from pm.nodes import test_node as _pm_test_node


# --------------------------------------------------------------------------
# 1. 缓存键
# --------------------------------------------------------------------------
def test_target_cache_key_deterministic():
    assert key_for_target("p", "i", "m") == key_for_target("p", "i", "m")
    assert key_for_target("p", "i", "m") != key_for_target("p", "i", "m2")
    assert key_for_target("p", "i", "m") != key_for_target("p", "i2", "m")


def test_eval_cache_key_sensitive_to_output():
    """评估缓存依赖被测输出：输出变化则缓存自然失效。"""
    assert key_for_eval("p", "i", "o1", "j") != key_for_eval("p", "i", "o2", "j")
    assert key_for_eval("p", "i", "o", "evaluator:a") != key_for_eval("p", "i", "o", "evaluator:b")


# --------------------------------------------------------------------------
# 2. JsonCache 读写与淘汰
# --------------------------------------------------------------------------
def test_json_cache_roundtrip(tmp_path):
    c = JsonCache(tmp_path / "c.json", max_entries=10)
    c.put("k1", {"v": 1})
    assert c.get("k1") == {"v": 1}
    assert len(c) == 1
    # 持久化到磁盘后可重新加载
    c2 = JsonCache(tmp_path / "c.json", max_entries=10)
    assert c2.get("k1") == {"v": 1}


def test_json_cache_eviction_fifo(tmp_path):
    c = JsonCache(tmp_path / "c.json", max_entries=3)
    for i in range(6):
        c.put(f"k{i}", i)
    assert len(c) <= 3
    assert c.get("k0") is None  # 最旧的被淘汰
    assert c.get("k5") == 5


def test_json_cache_tolerates_corrupt_file(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{ 这不是合法 JSON", encoding="utf-8")
    c = JsonCache(p, max_entries=3)
    assert len(c) == 0
    c.put("k", 1)  # 能继续写
    assert c.get("k") == 1


# --------------------------------------------------------------------------
# 3. test_node 并发
# --------------------------------------------------------------------------
def _test_state(n_cases: int = 5) -> dict:
    return {
        "run_id": "conc-test",
        "iteration": 0,
        "prompt": "[角色] 数据分析师\n[任务] 分析数据",
        "test_cases": [f"用例 {i}" for i in range(n_cases)],
        "target_model": "fake-target",
    }


def test_test_node_concurrent_ordered():
    """并发执行：所有用例都被执行，且按用例序号稳定排序。"""
    with testing.fake_backend("progress"):
        patch = _pm_test_node(_test_state(5))
    runs = patch["test_runs"]
    assert len(runs) == 5
    assert [r["test_case_index"] for r in runs] == [0, 1, 2, 3, 4]
    assert all(r["output"] for r in runs)
    assert patch["llm_calls"] == 5


def test_test_node_serial_when_concurrency_1(monkeypatch):
    monkeypatch.setenv("PM_TARGET_MAX_CONCURRENCY", "1")
    with testing.fake_backend("progress"):
        patch = _pm_test_node(_test_state(3))
    assert len(patch["test_runs"]) == 3
    assert patch["llm_calls"] == 3


def test_test_node_single_case_no_pool_overhead():
    with testing.fake_backend("progress"):
        patch = _pm_test_node(_test_state(1))
    assert len(patch["test_runs"]) == 1
    assert patch["test_runs"][0]["test_case_index"] == 0


# --------------------------------------------------------------------------
# 4. 评估缓存命中
# --------------------------------------------------------------------------
def _eval_state() -> dict:
    return {
        "run_id": "cache-test",
        "iteration": 0,
        "task": "分析销售数据",
        "context": "",
        "prompt": "[角色] 数据分析师",
        "llm_calls": 0,
        "test_runs": [
            {
                "test_case_index": 0,
                "test_input": "输入1",
                "prompt": "[角色] 数据分析师",
                "output": "输出1",
                "target_model": "fake",
                "error": None,
            },
            {
                "test_case_index": 1,
                "test_input": "输入2",
                "prompt": "[角色] 数据分析师",
                "output": "输出2",
                "target_model": "fake",
                "error": None,
            },
        ],
        "prompt_versions": [
            {
                "iteration": 0,
                "prompt": "[角色] 数据分析师",
                "avg_score": None,
                "min_score": None,
                "note": "初版",
            },
        ],
        "max_iterations": 3,
        "evaluations": [],
        "errors": [],
        "trace": [],
        "status": "running",
    }


def test_evaluate_node_cache_hit_skips_llm(tmp_path, monkeypatch):
    """相同输入输出二次评估：命中缓存，不再消耗 LLM 调用。"""
    monkeypatch.setenv("PM_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("PM_JUDGES", "1")  # 单评委，便于精确计数
    cache_mod._eval_cache = None
    cache_mod._target_cache = None

    calls = {"n": 0}

    def fake_structured(role, model_cls, system, user, max_retries=3, overrides=None):
        calls["n"] += 1
        return testing._fake_structured(role, model_cls, system, user, max_retries, overrides)

    monkeypatch.setattr("pm.nodes.structured_call", fake_structured)
    monkeypatch.setattr("pm.nodes.plain_call", testing._fake_plain)

    base = _eval_state()
    patch1 = evaluate_node(dict(base))
    n_first = calls["n"]
    assert n_first == 2  # 2 用例 × 1 评委
    assert patch1["llm_calls"] == 2
    assert all(not e["cache_hit"] for e in patch1["evaluations"])

    patch2 = evaluate_node(dict(base))
    assert calls["n"] == n_first  # 没有新增调用
    assert patch2["llm_calls"] == 0
    assert all(e["cache_hit"] for e in patch2["evaluations"])
    # 缓存结果可追溯：保留了评委与分歧信息
    assert all(e["judge"] == "evaluator" for e in patch2["evaluations"])


def test_evaluate_node_cache_misses_on_changed_output(tmp_path, monkeypatch):
    """被测输出变化 → 缓存自然失效，重新评估。"""
    monkeypatch.setenv("PM_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("PM_JUDGES", "1")
    cache_mod._eval_cache = None
    cache_mod._target_cache = None

    calls = {"n": 0}

    def fake_structured(role, model_cls, system, user, max_retries=3, overrides=None):
        calls["n"] += 1
        return testing._fake_structured(role, model_cls, system, user, max_retries, overrides)

    monkeypatch.setattr("pm.nodes.structured_call", fake_structured)
    monkeypatch.setattr("pm.nodes.plain_call", testing._fake_plain)

    base = _eval_state()
    evaluate_node(dict(base))
    n_first = calls["n"]

    changed = dict(base)
    changed["test_runs"] = [
        {**r, "output": r["output"] + "（重新生成）"} for r in base["test_runs"]
    ]
    evaluate_node(changed)
    assert calls["n"] == n_first + 2  # 输出变了 → 全部重新评估
