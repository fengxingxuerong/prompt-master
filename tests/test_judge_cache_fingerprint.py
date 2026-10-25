"""评估缓存键必须随**实际发请求的评委配置**变化，而不只是模型名。

为什么钉这条（2026-10-31 审计实测）：`_judge_spec` 旧版只拼 `build_config(j).model`，
于是下面三种"换了评委"都不会让缓存失效 —— 报告会用旧评委打的分冒充新读数：

| 改动 | 旧键 | 后果 |
|---|---|---|
| `PM_EVALUATOR_BASE_URL` 换服务商（同名模型） | 不变 | 分数出自旧端点，报告只写"缓存命中" |
| `PM_EVALUATOR_TEMPERATURE` 0.1 → 0.9 | 不变 | 采样分布变了，用的是旧采样下的判定 |
| `PM_EVALUATOR_MAX_TOKENS` 调小致截断 | 不变 | 截断前后的判定混用 |

`pm/llm/config.py::_decorrelate_judge_b` 的 docstring 早已断言"温度变化会进入
call_fingerprint，旧评估缓存会一次性失效"，但评估缓存根本没走那个指纹 ——
这条用例把那句话变成可执行的判据。
"""

from __future__ import annotations

import pytest
from pm.cache import key_for_eval
from pm.nodes import judge as J


def _spec(monkeypatch: pytest.MonkeyPatch, **env: str) -> str:
    """在干净的角色配置上设几个 env，返回评委指纹。"""
    for k in (
        "PM_EVALUATOR_MODEL",
        "PM_EVALUATOR_BASE_URL",
        "PM_EVALUATOR_TEMPERATURE",
        "PM_EVALUATOR_MAX_TOKENS",
    ):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PM_MODEL", "judge-base")
    monkeypatch.setenv("PM_BASE_URL", "https://a.example/v1")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return J._judge_spec(["evaluator"])


def test_same_config_is_stable(monkeypatch: pytest.MonkeyPatch) -> None:
    """同一配置两次取值必须逐字相同，否则缓存永远命不中。"""
    assert _spec(monkeypatch) == _spec(monkeypatch)


def test_base_url_change_invalidates(monkeypatch: pytest.MonkeyPatch) -> None:
    """换端点（同名模型）必须换键。"""
    a = _spec(monkeypatch, PM_EVALUATOR_BASE_URL="https://a.example/v1")
    b = _spec(monkeypatch, PM_EVALUATOR_BASE_URL="https://b.example/v1")
    assert a != b, "换评委端点后评估缓存键没变：续跑会复读旧端点的分数"


def test_temperature_change_invalidates(monkeypatch: pytest.MonkeyPatch) -> None:
    a = _spec(monkeypatch, PM_EVALUATOR_TEMPERATURE="0.1")
    b = _spec(monkeypatch, PM_EVALUATOR_TEMPERATURE="0.9")
    assert a != b, "改评委温度后评估缓存键没变：采样分布变了却复用旧判定"


def test_max_tokens_change_invalidates(monkeypatch: pytest.MonkeyPatch) -> None:
    a = _spec(monkeypatch, PM_EVALUATOR_MAX_TOKENS="2000")
    b = _spec(monkeypatch, PM_EVALUATOR_MAX_TOKENS="200")
    assert a != b, "改评委 max_tokens 后评估缓存键没变：截断前后的判定会混用"


def test_model_still_readable_in_spec(monkeypatch: pytest.MonkeyPatch) -> None:
    """指纹是哈希，但可读的模型名要留着 —— 报告与排障要看得懂这个键是谁。"""
    spec = _spec(monkeypatch, PM_EVALUATOR_MODEL="judge-x")
    assert "evaluator:judge-x@" in spec


def test_spec_reaches_the_eval_cache_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """端到端：指纹不同 ⇒ key_for_eval 不同（不是只在 _judge_spec 里变了）。"""
    ka = key_for_eval(
        "p", "i", "o", _spec(monkeypatch, PM_EVALUATOR_TEMPERATURE="0.1"), extra="task"
    )
    kb = key_for_eval(
        "p", "i", "o", _spec(monkeypatch, PM_EVALUATOR_TEMPERATURE="0.9"), extra="task"
    )
    assert ka != kb
