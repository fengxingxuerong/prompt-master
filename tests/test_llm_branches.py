"""`pm/llm.py` 的限流退避、降级通道与记账分支（覆盖率洼地补齐）。

背景：2026-09-13 生产贴近测试发现 `pm/llm.py` 覆盖率仅 77%，缺口集中在
**真实故障高发路径**——429 退避与 Key 轮换、退避预算耗尽、通道 A→B 降级、
端点不兼容记忆、token 记账。这些分支恰恰是"平时不跑、出事时才跑"的代码，
必须用注入式假 LLM 精确打靶（不联网、不 sleep）。

注入方式：monkeypatch `pm.llm.get_llm` 返回脚本化假客户端；
`time.sleep` 一并打桩，避免测试真的等 5/10/20 秒。
"""

from __future__ import annotations

import json
import time
import types
from typing import Any

import pytest
from pm import llm as L


def _Cfg(**kw: Any) -> Any:
    """minimal stand-in for LLMConfig（预算告警只需要 max_tokens）。"""
    return types.SimpleNamespace(**kw)


# --------------------------------------------------------------------------
# 假客户端：按脚本依次返回结果或抛异常
# --------------------------------------------------------------------------


class _FakeLLM:
    def __init__(self, script: list[Any]):
        self.script = list(script)
        self.calls: list[Any] = []

    def _pop(self) -> Any:
        self.calls.append(1)
        item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(item, Exception):
            raise item
        return item

    def invoke(self, messages: Any) -> Any:
        return self._pop()

    def with_structured_output(self, model_cls: Any, **kw: Any) -> _FakeLLM:
        self.so_kwargs = kw
        self.so_cls = model_cls
        return self

    def with_config(self, **kw: Any) -> _FakeLLM:
        self.config_kwargs = kw
        return self


class _Resp:
    def __init__(self, content: str = "{}", usage: dict | None = None):
        self.content = content
        self.usage_metadata = usage or {}
        self.response_metadata: dict[str, Any] = {}


@pytest.fixture(autouse=True)
def _fast_and_credentialed(monkeypatch):
    """关掉真实等待、给个假 Key（get_llm 已被打桩，Key 只是让 build_config 有值）。"""
    monkeypatch.setattr(L.time, "sleep", lambda _s: None)
    monkeypatch.setenv("PM_API_KEY", "sk-test")
    monkeypatch.delenv("PM_API_KEYS", raising=False)
    yield


def _patch_llm(monkeypatch, fake: _FakeLLM) -> None:
    monkeypatch.setattr(L, "get_llm", lambda role, overrides=None: fake)


def _rate_limit_exc() -> Exception:
    e = Exception("Error code: 429 - rate limit exceeded")
    e.status_code = 429  # type: ignore[attr-defined]
    return e


# --------------------------------------------------------------------------
# 1. 记账：_tokens_of / _record_usage
# --------------------------------------------------------------------------
def test_tokens_of_reads_usage_metadata():
    assert L._tokens_of(_Resp(usage={"input_tokens": 12, "output_tokens": 7})) == (12, 7)


def test_tokens_of_falls_back_to_response_metadata():
    r = _Resp()
    r.usage_metadata = None  # 非 dict 才轮到 response_metadata 分支
    r.response_metadata = {"token_usage": {"prompt_tokens": 5, "completion_tokens": 3}}
    assert L._tokens_of(r) == (5, 3)


def test_tokens_of_survives_hostile_object():
    """记账绝不影响主流程：属性访问炸了也要返回 (0, 0)。"""

    class Hostile:
        @property
        def usage_metadata(self):
            raise RuntimeError("boom")

    assert L._tokens_of(Hostile()) == (0, 0)


def test_record_usage_writes_into_ledger():
    with L.usage_scope() as ledger:  # type: ignore[attr-defined]
        L._record_usage("evaluator", _Resp(usage={"input_tokens": 9, "output_tokens": 4}), 120)
    snap = ledger.snapshot()
    assert snap["evaluator"]["calls"] == 1
    assert snap["evaluator"]["input_tokens"] == 9
    assert snap["evaluator"]["output_tokens"] == 4


def test_record_usage_without_ledger_is_noop():
    L._record_usage("evaluator", _Resp(), 10)  # 不抛即通过


# --------------------------------------------------------------------------
# 2. 环境变量容错与 Key 池
# --------------------------------------------------------------------------
def test_int_env_bad_value_falls_back(monkeypatch):
    monkeypatch.setenv("PM_TEST_INT", "abc")
    assert L._int_env("PM_TEST_INT", 7) == 7
    monkeypatch.setenv("PM_TEST_INT", "12.9")  # 小数串要能解析成 12，而不是崩
    assert L._int_env("PM_TEST_INT", 7) == 12


def test_float_env_bad_value_falls_back(monkeypatch):
    monkeypatch.setenv("PM_TEST_FLOAT", "xx")
    assert L._float_env("PM_TEST_FLOAT", 1.5) == 1.5


def test_key_pool_prefers_role_key(monkeypatch):
    monkeypatch.setenv("PM_CLARIFIER_API_KEY", "role-key")
    assert L._key_pool("clarifier") == ["role-key"]


def test_key_pool_merges_global_and_pool_without_dupes(monkeypatch):
    monkeypatch.setenv("PM_API_KEY", "k1")
    monkeypatch.setenv("PM_API_KEYS", "k1, k2 ,k3")
    assert L._key_pool("optimizer") == ["k1", "k2", "k3"]


def test_next_key_rotates_and_handles_empty_pool(monkeypatch):
    # 池里只留 PM_API_KEYS：全局 Key 也进池，不清干净就断言不了轮换顺序
    monkeypatch.setenv("PM_API_KEY", "")
    monkeypatch.setenv("PM_OPTIMIZER_API_KEY", "")
    monkeypatch.setenv("PM_API_KEYS", "a,b")
    keys = [L._next_key("optimizer") for _ in range(3)]
    assert set(keys[:2]) == {"a", "b"}, keys
    assert keys[0] != keys[1]  # 连续两次必须换 Key
    assert keys[2] == keys[0]  # 轮回到第一个
    monkeypatch.setenv("PM_API_KEYS", "")
    assert L._next_key("ghost") == ""


# --------------------------------------------------------------------------
# 3. 限流判定（A4 回归：400 + 100429 token 数不能被误判成限流）
# --------------------------------------------------------------------------
def test_rate_limit_detection_variants():
    assert L._is_rate_limit_error(_rate_limit_exc()) is True
    assert L._is_rate_limit_error(Exception("RateLimitError: slow down")) is True
    assert L._is_rate_limit_error(Exception("HTTP 429 Too Many Requests")) is True
    # 关键回归：token 数里含 429 的 400 错误不是限流
    assert L._is_rate_limit_error(Exception("Error code: 400 - used 100429 tokens")) is False
    assert L._is_rate_limit_error(ValueError("plain")) is False


# --------------------------------------------------------------------------
# 4. 退避重试：成功、轮换、预算耗尽、非限流直接上抛
# --------------------------------------------------------------------------
def test_retry_succeeds_after_rate_limit_and_records_usage(monkeypatch):
    monkeypatch.setenv("PM_API_KEYS", "k1,k2")
    fake = _FakeLLM([_rate_limit_exc(), _Resp("ok", usage={"input_tokens": 1, "output_tokens": 2})])
    _patch_llm(monkeypatch, fake)
    with L.usage_scope() as ledger:  # type: ignore[attr-defined]
        resp = L._invoke_with_rate_limit_retry("optimizer", lambda client: client.invoke([]))
    assert resp.content == "ok"
    assert len(fake.calls) == 2  # 第一枪 429，第二枪成功
    assert ledger.snapshot()["optimizer"]["calls"] == 1  # 只记成功那一次


def test_retry_budget_exhausted_raises_instead_of_hanging(monkeypatch):
    """A3：退避预算用尽必须上抛，不能把流水线卡十几分钟。"""
    monkeypatch.setattr(L, "RATE_LIMIT_MAX_WAIT", 0.0)
    fake = _FakeLLM([_rate_limit_exc()])
    _patch_llm(monkeypatch, fake)
    with pytest.raises(Exception, match="rate limit"):
        L._invoke_with_rate_limit_retry("optimizer", lambda client: client.invoke([]))
    assert len(fake.calls) == 1  # 预算为 0：第一次就放弃，不做无谓重试


def test_non_rate_limit_error_is_raised_immediately(monkeypatch):
    fake = _FakeLLM([ValueError("bad request")])
    _patch_llm(monkeypatch, fake)
    with pytest.raises(ValueError):
        L._invoke_with_rate_limit_retry("optimizer", lambda client: client.invoke([]))
    assert len(fake.calls) == 1


def test_single_key_pool_doubles_backoff(monkeypatch):
    """Key 池只有 1 个 Key 时退避加倍（给上游配额恢复窗口）——只验证不炸且最终成功。"""
    monkeypatch.delenv("PM_API_KEYS", raising=False)
    fake = _FakeLLM([_rate_limit_exc(), _Resp("ok")])
    _patch_llm(monkeypatch, fake)
    resp = L._invoke_with_rate_limit_retry("optimizer", lambda client: client.invoke([]))
    assert resp.content == "ok"


# --------------------------------------------------------------------------
# 5. 配置与指纹
# --------------------------------------------------------------------------
def test_build_config_overrides_apply_and_ignore_unknown_keys(monkeypatch):
    cfg = L.build_config("optimizer", {"max_tokens": 123, "nope": 1})
    assert cfg.max_tokens == 123
    assert not hasattr(cfg, "nope")


def test_call_fingerprint_changes_with_sampling_params(monkeypatch):
    a = L.call_fingerprint("target")
    monkeypatch.setenv("PM_TARGET_MAX_TOKENS", "77")
    assert L.call_fingerprint("target") != a


def test_get_llm_requires_api_key(monkeypatch):
    monkeypatch.setenv("PM_API_KEY", "")
    monkeypatch.setenv("PM_OPTIMIZER_API_KEY", "")
    with pytest.raises(RuntimeError, match="缺少 API Key"):
        L.get_llm("optimizer")


def test_get_llm_builds_openai_client():
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("PM_API_KEY", "sk-x")
    try:
        client = L.get_llm("optimizer")
        assert client.__class__.__name__ == "ChatOpenAI"
    finally:
        monkeypatch.undo()


def test_get_llm_anthropic_without_extra_dependency(monkeypatch):
    """anthropic 走延迟导入：没装 extras 时应是 ImportError，而不是启动期就崩。"""
    monkeypatch.setenv("PM_API_KEY", "sk-x")
    monkeypatch.setenv("PM_OPTIMIZER_PROVIDER", "anthropic")
    try:
        import langchain_anthropic  # noqa: F401
    except ImportError:
        with pytest.raises(ImportError):
            L.get_llm("optimizer")
    else:  # 装了则应当能构造
        assert L.get_llm("optimizer") is not None


def test_structured_method_env_validation(monkeypatch):
    monkeypatch.setenv("PM_STRUCT_METHOD", "function_calling")
    assert L._structured_method() == "function_calling"
    monkeypatch.setenv("PM_STRUCT_METHOD", "bogus")
    assert L._structured_method() == ""  # 非法值告警并回退默认


# --------------------------------------------------------------------------
# 6. 通道 A 跳过 / 端点不兼容记忆
# --------------------------------------------------------------------------
def test_force_json_channel_skips_native(monkeypatch):
    monkeypatch.setenv("PM_FORCE_JSON_CHANNEL", "1")
    cfg = L.build_config("optimizer")
    assert "强制" in (L._skip_channel_a(cfg) or "")


def test_remember_unsupported_only_for_deterministic_errors(monkeypatch):
    cfg = L.build_config("optimizer")
    key = L._capability_key(cfg)
    # 网络抖动不能记：否则一次偶发错误永久关掉原生通道
    L._remember_channel_a_unsupported(cfg, "ReadTimeout: connection timed out")
    assert L._skip_channel_a(cfg) is None
    # 确定性的不兼容错语 + 400 才记
    L._remember_channel_a_unsupported(
        cfg, "Error code: 400 - this model does not support json_schema response format"
    )
    assert key in L._UNSUPPORTED_A
    assert "此前已报不兼容" in (L._skip_channel_a(cfg) or "")
    L._UNSUPPORTED_A.discard(key)  # 清理，避免污染其他用例


# --------------------------------------------------------------------------
# 7. structured_call：通道 A 命中 / dict 兜底 / 降级到 B / 全败
# --------------------------------------------------------------------------
def _model_cls():
    from pydantic import BaseModel

    class _M(BaseModel):
        value: int

    return _M


def test_channel_a_returns_instance(monkeypatch):
    cls = _model_cls()
    fake = _FakeLLM([cls(value=1)])
    _patch_llm(monkeypatch, fake)
    out, meta = L.structured_call("mockgen", cls, "sys", "user")
    assert out.value == 1
    assert meta["channel"] == "structured_output"


def test_channel_a_dict_is_validated(monkeypatch):
    cls = _model_cls()
    fake = _FakeLLM([{"value": 5}])
    _patch_llm(monkeypatch, fake)
    out, meta = L.structured_call("mockgen", cls, "sys", "user")
    assert out.value == 5
    assert meta["channel"] == "structured_output"


def test_channel_a_wrong_type_falls_back_to_text(monkeypatch):
    """端点"支持"结构化却回 None（思考型模型常见）时必须降级并留痕。"""
    cls = _model_cls()
    fake = _FakeLLM([None, _Resp(json.dumps({"value": 9}))])
    _patch_llm(monkeypatch, fake)
    out, meta = L.structured_call("mockgen", cls, "sys", "user")
    assert out.value == 9
    assert meta["channel"] == "json_fallback"


def test_channel_a_exception_falls_back_to_text(monkeypatch):
    cls = _model_cls()
    fake = _FakeLLM([RuntimeError("native unavailable"), _Resp('{"value": 3}')])
    _patch_llm(monkeypatch, fake)
    out, meta = L.structured_call("mockgen", cls, "sys", "user")
    assert out.value == 3
    assert meta["channel"] == "json_fallback"


def test_parse_failure_retries_with_decay_by_default(monkeypatch):
    """解析失败默认沿用**降温**。

    2026-09-14 对照实验（scripts/temp_policy_probe.py）证明升温无收益：
    温度 0.0/0.1/0.2 过 schema 均 1/4，0.3 为 0/4；且 temp=0.0 基础成功率仅 25%
    → 瓶颈在 schema/提示词本身，不在温度。故默认不升温（PM_TEMP_BOOST_ON_PARSE=0）。
    """
    cls = _model_cls()
    temps: list[float] = []

    class _T(_FakeLLM):
        def with_config(self, **kw: Any) -> _T:
            temps.append(kw.get("temperature"))
            return self

    fake = _T([RuntimeError("native down"), _Resp("not json"), _Resp('{"value": 4}')])
    _patch_llm(monkeypatch, fake)
    out, meta = L.structured_call("mockgen", cls, "sys", "user", max_retries=3)
    assert out.value == 4
    assert meta["attempts"] == 2
    # 默认降温（不是升温）
    assert len(temps) >= 2 and temps[1] <= temps[0]


def test_parse_failure_boost_is_opt_in(monkeypatch):
    """PM_TEMP_BOOST_ON_PARSE>0 时才升温（保留开关，便于将来用更大样本复核）。"""
    cls = _model_cls()
    temps: list[float] = []
    monkeypatch.setattr(L, "TEMPERATURE_BOOST_ON_PARSE", 0.2)

    class _T(_FakeLLM):
        def with_config(self, **kw: Any) -> _T:
            temps.append(kw.get("temperature"))
            return self

    fake = _T([RuntimeError("native down"), _Resp("not json"), _Resp('{"value": 5}')])
    _patch_llm(monkeypatch, fake)
    out, _meta = L.structured_call("mockgen", cls, "sys", "user", max_retries=3)
    assert out.value == 5
    # 0.2(boost) - 0.15(decay) = 净 +0.05 → 略升
    assert len(temps) >= 2 and temps[1] > temps[0]


def test_all_channels_fail_raises_runtime_error(monkeypatch):
    cls = _model_cls()
    fake = _FakeLLM([RuntimeError("native down"), _Resp("still not json")])
    _patch_llm(monkeypatch, fake)
    with pytest.raises(RuntimeError, match="结构化输出失败"):
        L.structured_call("mockgen", cls, "sys", "user", max_retries=2)


# --------------------------------------------------------------------------
# 8. JSON 抽取与 plain_call
# --------------------------------------------------------------------------
def test_extract_json_object_variants():
    assert L.extract_json_object("") is None
    assert L.extract_json_object("no json here") is None
    assert L.extract_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert L.extract_json_object('前缀 {"a": {"b": [1, 2]}} 后缀') == {"a": {"b": [1, 2]}}
    # 字符串里含花括号与转义引号，括号配平扫描不能被带偏
    assert L.extract_json_object('{"s": "}{ \\" }"}') == {"s": '}{ " }'}
    # 第一个候选非法时继续往后找
    assert L.extract_json_object('{bad} then {"ok": 1}') == {"ok": 1}
    # 空/纯空白内容必须判为"无 JSON"——这是第 9 轮的首要失败模式（len=0），
    # 必须触发重试，绝不能静默通过
    assert L.extract_json_object("   \n\t  ") is None


def test_empty_response_triggers_retry_not_silent_pass(monkeypatch):
    """空返回（len=0）是思考型模型把 token 预算耗在 reasoning 上的典型症状。

    第 9 轮实测 3/6 空返回是评委的首要失败模式（scripts/nojson_probe.py）。
    这里锁住行为：空返回 → 走重试，且重试时温度按默认降温。
    """
    cls = _model_cls()
    temps: list[float] = []

    class _T(_FakeLLM):
        def with_config(self, **kw: Any) -> _T:
            temps.append(kw.get("temperature"))
            return self

    fake = _T([RuntimeError("native down"), _Resp(""), _Resp('{"value": 8}')])
    _patch_llm(monkeypatch, fake)
    out, meta = L.structured_call("mockgen", cls, "sys", "user", max_retries=3)
    assert out.value == 8
    assert meta["attempts"] == 2  # 空返回被当作失败重试，而非直接通过
    assert len(temps) >= 2 and temps[1] <= temps[0]


def test_plain_call_returns_text_and_meta(monkeypatch):
    fake = _FakeLLM([_Resp("hello", usage={"input_tokens": 3, "output_tokens": 1})])
    _patch_llm(monkeypatch, fake)
    text, meta = L.plain_call("target", "sys", "user")
    assert text == "hello"
    assert meta["role"] == "target"
    assert meta["channel"] == "plain"


# --------------------------------------------------------------------------
# 9. 补边界：坏围栏 / 状态码提示缺失 / 强制文本通道 / 通道 B 泛型异常 / anthropic 分支
# --------------------------------------------------------------------------
def test_fenced_json_invalid_falls_through_to_brace_scan():
    """围栏里的 JSON 非法时不能直接放弃，要继续做括号配平扫描。"""
    text = '```json\n{bad json}\n```\n{"ok": 1}'
    assert L.extract_json_object(text) == {"ok": 1}


def test_remember_unsupported_requires_status_hint():
    """只有"不支持"措辞、没有 400/bad request 状态提示的，不记（可能是网络层伪造措辞）。"""
    cfg = L.build_config("optimizer")
    key = L._capability_key(cfg)
    L._UNSUPPORTED_A.discard(key)
    L._remember_channel_a_unsupported(cfg, "this endpoint does not support json_schema")
    assert key not in L._UNSUPPORTED_A


def test_forced_text_channel_goes_through_skip_branch(monkeypatch):
    """PM_FORCE_JSON_CHANNEL=1 时应走 _ChannelASkipped 分支，直接进文本通道。"""
    cls = _model_cls()
    monkeypatch.setenv("PM_FORCE_JSON_CHANNEL", "1")
    fake = _FakeLLM([_Resp('{"value": 2}')])
    _patch_llm(monkeypatch, fake)
    out, meta = L.structured_call("mockgen", cls, "sys", "user")
    assert out.value == 2
    assert meta["channel"] == "json_fallback"


def test_text_channel_generic_exception_retries_with_cooldown(monkeypatch):
    """通道 B 抛非解析类异常（端点 5xx / 超时）也要降温重试，而不是立刻放弃。"""
    cls = _model_cls()
    fake = _FakeLLM(
        [RuntimeError("native down"), RuntimeError("upstream exploded"), _Resp('{"value": 8}')]
    )
    _patch_llm(monkeypatch, fake)
    out, meta = L.structured_call("mockgen", cls, "sys", "user", max_retries=3)
    assert out.value == 8
    assert meta["attempts"] == 2


def test_get_llm_anthropic_branch_with_stub_module(monkeypatch):
    """未装 langchain_anthropic 时用桩模块覆盖 anthropic 构造分支（不联网、不装依赖）。"""
    import sys
    import types

    stub = types.ModuleType("langchain_anthropic")

    class _FakeChatAnthropic:
        def __init__(self, **kw):
            self.kw = kw

    stub.ChatAnthropic = _FakeChatAnthropic  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "langchain_anthropic", stub)
    monkeypatch.setenv("PM_API_KEY", "sk-x")
    monkeypatch.setenv("PM_OPTIMIZER_PROVIDER", "anthropic")
    client = L.get_llm("optimizer")
    assert isinstance(client, _FakeChatAnthropic)
    assert client.kw["api_key"] == "sk-x"


# --------------------------------------------------------------------------
# 10. 瞬时连接故障重试（第 6 轮真实事故：48 分钟一轮里 4 次盲评 + 多次评估
#     全部因 OpenAIConnectionError 丢失，只因重试策略只覆盖 429）
# --------------------------------------------------------------------------
def test_transient_conn_error_classification():
    assert L._is_transient_conn_error(Exception("OpenAIConnectionError: Connection error.")) is True
    assert L._is_transient_conn_error(Exception("ReadTimeout: read timeout")) is True
    assert L._is_transient_conn_error(Exception("502 Bad Gateway")) is True
    # 请求本身有问题 / 限流 都不属于"连接抖动"，不能靠原地重试解决
    assert L._is_transient_conn_error(ValueError("bad schema")) is False
    assert L._is_transient_conn_error(Exception("Error code: 429")) is False


def test_conn_retry_recovers_after_blip(monkeypatch):
    """一次连接抖动后恢复：不应把整次调用判死。"""
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("Connection error.")
        return "ok"

    assert L._invoke_with_conn_retry("evaluator", flaky) == "ok"
    assert calls["n"] == 2


def test_conn_retry_gives_up_after_budget(monkeypatch):
    monkeypatch.setattr(L, "TRANSIENT_CONN_RETRIES", 2)
    calls = {"n": 0}

    def always_down():
        calls["n"] += 1
        raise ConnectionError("Connection error.")

    with pytest.raises(ConnectionError):
        L._invoke_with_conn_retry("evaluator", always_down)
    assert calls["n"] == 3  # 首次 + 2 次重试，之后放弃


def test_conn_retry_does_not_swallow_real_errors():
    def bad_request():
        raise ValueError("dimension_scores Field required")

    with pytest.raises(ValueError):
        L._invoke_with_conn_retry("evaluator", bad_request)


def test_rate_limit_path_also_gets_conn_retry(monkeypatch):
    """限流重试循环里也应享受连接重试：429 → 连接抖动 → 成功。"""
    fake = _FakeLLM(
        [
            _rate_limit_exc(),
            ConnectionError("Connection error."),
            _Resp("ok", usage={"input_tokens": 1, "output_tokens": 1}),
        ]
    )
    _patch_llm(monkeypatch, fake)
    resp = L._invoke_with_rate_limit_retry("optimizer", lambda client: client.invoke([]))
    assert resp.content == "ok"


# --------------------------------------------------------------------------
# 11. 解析失败的处理方向（第 7 轮：evaluate#2 重试 3 次全败）
#     解析失败是"结构问题"，降温会让模型更确定地复现同一份坏 JSON。
# --------------------------------------------------------------------------
def test_parse_failure_hint_lists_required_top_level_keys(monkeypatch, caplog):
    """缺字段是常见成因：下一次重试必须把"必须包含哪些顶层键"写进提示。"""
    cls = _model_cls()
    fake = _FakeLLM([RuntimeError("native down"), _Resp("not json"), _Resp('{"value": 6}')])
    _patch_llm(monkeypatch, fake)
    with caplog.at_level("WARNING", logger="pm.llm"):
        L.structured_call("mockgen", cls, "sys", "user", max_retries=3)
    assert "必须包含这些顶层键" in caplog.text
    assert "value" in caplog.text  # 至少列出目标模型的字段名


def test_endpoint_exception_still_decays_temperature(monkeypatch):
    """端点/网络类异常与采样随机性无关，沿用降温策略（不要误改成升温）。"""
    cls = _model_cls()
    temps: list[float] = []

    class _T(_FakeLLM):
        def with_config(self, **kw: Any) -> _T:
            temps.append(kw.get("temperature"))
            return self

    # 端点异常（连接类）在 _invoke_with_conn_retry 里先重试；这里用非连接类的普通异常
    fake = _T(
        [RuntimeError("native down"), RuntimeError("upstream exploded"), _Resp('{"value": 7}')]
    )
    _patch_llm(monkeypatch, fake)
    out, _meta = L.structured_call("mockgen", cls, "sys", "user", max_retries=3)
    assert out.value == 7
    assert len(temps) >= 2 and temps[1] <= temps[0]


# --------------------------------------------------------------------------
# 12. schema 形状误解的修复（第 9 轮：evaluate 过 schema 仅 25%，根因是"平铺 vs 嵌套"）
#     实测形态：模型把嵌套字段的子键直接平铺到顶层，导致 dimension_scores 等 missing。
# --------------------------------------------------------------------------
def _eval_cls() -> type[Any]:
    """真实的 EvaluationResult（本项目里嵌套最深的 schema，正是出事的那一个）。"""
    from pm.schemas import EvaluationResult

    return EvaluationResult


def test_schema_skeleton_lists_only_required_keys_and_is_valid_json():
    """骨架必须只含**必填**键（不含 judge/sample_*/weighted_score 等派生字段），且是合法 JSON。

    第 9 轮的旧提示把 12 个顶层键全列（含 5~6 个派生字段）→ 噪声大且诱导编造。
    """
    import json

    cls = _eval_cls()
    skeleton = L._schema_skeleton(cls)
    parsed = json.loads(skeleton)
    assert set(parsed) == set(L._model_required_fields(cls))
    assert parsed["dimension_scores"] == dict.fromkeys(
        L._model_required_fields(cls.model_fields["dimension_scores"].annotation), 0
    )
    # 派生字段一个都不该出现
    for derived in ("weighted_score", "judge", "passed", "sample_scores", "cache_hit"):
        assert derived not in skeleton
    # 嵌套必须保持嵌套（这正是模型最容易搞错的地方）
    assert isinstance(parsed["dimension_scores"], dict)


def test_model_required_fields_excludes_derived():
    """必填字段判定：只认 is_required()，派生字段（有 default）必须被排除。"""
    cls = _eval_cls()
    required = L._model_required_fields(cls)
    assert "dimension_scores" in required
    assert "model_reported_score" in required
    assert "should_revise" in required
    for derived in ("issues", "suggestions", "judge", "weighted_score", "n_samples"):
        assert derived not in required


def test_repair_shape_collapses_flat_subkeys_into_nested():
    """核心修复：全子键平铺到顶层时，唯一确定地收拢成嵌套对象。"""
    cls = _eval_cls()
    flat = {
        "task_completion": 5,
        "format_adherence": 6,
        "constraint_compliance": 5,
        "robustness": 6,
        "quality": 9,
        "model_reported_score": 6,
        "should_revise": True,
    }
    fixed = L._repair_shape(cls, flat)
    assert fixed["dimension_scores"] == {
        "task_completion": 5,
        "format_adherence": 6,
        "constraint_compliance": 5,
        "robustness": 6,
        "quality": 9,
    }
    # 已收拢的子键不该在顶层残留
    assert "task_completion" not in fixed
    # 收拢后的数据必须真能过 schema
    assert cls.model_validate(fixed).dimension_scores.quality == 9


def test_repair_shape_leaves_ambiguous_input_alone():
    """部分子键 / 顶层已有同名字段 → 不动（歧义交给模型重写，不靠代码猜）。"""
    cls = _eval_cls()
    partial = {"task_completion": 5, "quality": 9}
    assert L._repair_shape(cls, partial) == partial
    both = {"dimension_scores": {"task_completion": 1}, "task_completion": 5}
    assert L._repair_shape(cls, both) == both
    # 非 dict 原样返回（防御性）
    assert L._repair_shape(cls, [1, 2]) == [1, 2]  # type: ignore[arg-type]


def test_repair_shape_enables_one_shot_recovery_in_structured_call(monkeypatch):
    """端到端含义：模型给出"平铺"的坏 JSON 时，**第一次就能修好**，无需再重试。

    这是第 9 轮 25% 成功率的主要成因（3 个字段 missing 全因平铺）——
    修好后该形态不再消耗重试次数。
    """
    cls = _eval_cls()
    flat = json.dumps(
        {
            "task_completion": 5,
            "format_adherence": 6,
            "constraint_compliance": 5,
            "robustness": 6,
            "quality": 9,
            "model_reported_score": 6,
            "should_revise": True,
        }
    )
    fake = _FakeLLM([RuntimeError("native down"), _Resp(flat)])
    _patch_llm(monkeypatch, fake)
    out, meta = L.structured_call("evaluator", cls, "sys", "user", max_retries=3)
    assert meta["attempts"] == 1  # 一次命中，没有浪费重试
    assert out.dimension_scores.quality == 9
    assert out.model_reported_score == 6


def test_nested_field_names_lists_nested_required_fields():
    """重试提示要能点名嵌套字段（"别平铺"比泛泛列键有效）。"""
    cls = _eval_cls()
    hint = L._nested_field_names(cls)
    assert "dimension_scores" in hint
    assert "task_completion" in hint


def test_retry_hint_mentions_nested_requirement(monkeypatch, caplog):
    """重试提示里必须出现"嵌套/别平铺"的明确要求，而不只是列键名。"""
    cls = _eval_cls()
    fake = _FakeLLM([RuntimeError("native down"), _Resp("not json"), _Resp("still bad")])
    _patch_llm(monkeypatch, fake)
    with caplog.at_level("WARNING", logger="pm.llm"):
        with pytest.raises(RuntimeError):
            L.structured_call("evaluator", cls, "sys", "user", max_retries=2)
    assert "嵌套对象" in caplog.text
    assert "不要" in caplog.text and "平铺" in caplog.text


# --------------------------------------------------------------------------
# 13. token 预算被推理耗尽（第 11 轮发现，此前沿着 9 轮都没看出来）
#     思考型模型把 reasoning 计入 max_tokens → 正文额度归零 → 空返回/LengthFinish。
# --------------------------------------------------------------------------
def test_role_token_budgets_leave_room_for_reasoning():
    """预算必须覆盖「推理 + 正文」。

    第 11 轮直接对照（同模型/同端点/同任务，调用次数同为 15）：
        evaluator   max_tokens=6000 → 额度耗尽 **5 次**（reasoning 实测 5477~6000）
        evaluator_b max_tokens=8000 → **0 次**
    """
    for role in ("evaluator", "evaluator_b", "arbiter"):
        assert L.MAX_TOKENS[role] >= 8000  # 有对照证据
    # 以下为按同一口径推算（reasoning ≈ prompt × 1.5 + 正文余量），无逐角色对照
    assert L.MAX_TOKENS["clarifier"] >= 3000  # 实测 reasoning=1500 恰好吃满旧值
    assert L.MAX_TOKENS["comparator"] >= 2500  # 旧值 600 连一次推理都不够
    assert L.MAX_TOKENS["mockgen"] >= 4000
    assert L.MAX_TOKENS["optimizer"] >= 6000
    assert L.MAX_TOKENS["reviser"] >= 6000


def test_length_finish_error_is_detected_and_usage_parsed():
    """额度耗尽要被识别，且能解出 usage —— 否则告警给不出建议值。"""
    msg = (
        "LengthFinishReasonError: Could not parse response content as the length limit "
        "was reached - CompletionUsage(completion_tokens=6000, prompt_tokens=3551, "
        "total_tokens=9551, completion_tokens_details=CompletionTokensDetails("
        "accepted_prediction_tokens=None, audio_tokens=None, reasoning_tokens=6000))"
    )
    exc = RuntimeError(msg)
    assert L._is_length_finish_error(exc)
    assert L._usage_int(exc, "completion_tokens") == 6000
    assert L._usage_int(exc, "reasoning_tokens") == 6000
    assert L._usage_int(exc, "prompt_tokens") == 3551
    # 连接类错误不能被误判（否则会给出错误的调参建议）
    assert not L._is_length_finish_error(RuntimeError("OpenAIConnectionError: Connection error."))


def test_token_exhaustion_warning_mentions_reasoning_and_setting(caplog):
    """额度耗尽时告警必须点名「推理」并给出该改哪个环境变量。

    这类失败此前伪装成普通的"结构化输出不可用"，在日志里瞒了 9 轮 ——
    所以告警的**可操作性**本身就是要被测的行为。
    """
    cfg = _Cfg(max_tokens=6000)
    exc = RuntimeError(
        "LengthFinishReasonError: Could not parse response content as the "
        "length limit was reached - CompletionUsage(completion_tokens=6000, "
        "prompt_tokens=3551, total_tokens=9551, reasoning_tokens=6000)"
    )
    with caplog.at_level("WARNING", logger="pm.llm"):
        L._warn_if_token_budget_exhausted("evaluator", exc, cfg)
    assert "推理" in caplog.text
    assert "reasoning_tokens=6000" in caplog.text
    assert "PM_EVALUATOR_MAX_TOKENS" in caplog.text


def test_token_exhaustion_without_reasoning_says_output_is_long(caplog):
    """几乎无 reasoning 的耗尽 → 诊断成"输出本身太长"，别误导用户去调预算。"""
    cfg = _Cfg(max_tokens=6000)
    exc = RuntimeError(
        "LengthFinishReasonError: Could not parse response content as the "
        "length limit was reached - CompletionUsage(completion_tokens=6000, "
        "prompt_tokens=3551, total_tokens=9551, reasoning_tokens=300)"
    )
    with caplog.at_level("WARNING", logger="pm.llm"):
        L._warn_if_token_budget_exhausted("evaluator", exc, cfg)
    assert "输出" in caplog.text and "长" in caplog.text
    assert "推理" not in caplog.text


def test_non_length_error_produces_no_budget_warning(caplog):
    """非额度类异常不应触发预算告警（避免把连接抖动误报成配置问题）。"""
    cfg = _Cfg(max_tokens=6000)
    with caplog.at_level("WARNING", logger="pm.llm"):
        L._warn_if_token_budget_exhausted("evaluator", RuntimeError("boom"), cfg)
    assert "MAX_TOKENS" not in caplog.text


# --------------------------------------------------------------------------
# 12. ③ 解耦：限流预算与解析重试预算分离（2026-09-15）
#     旧版通道 B 的每次解析重试各自满额退避（最坏 4×60s），且限流上抛被
#     except 大兜底当成"解析失败"消耗一次重试名额 —— 429 吃掉重试预算。
# --------------------------------------------------------------------------
def test_channel_b_rate_limit_raises_without_consuming_retries(monkeypatch):
    """限流预算耗尽：立即上抛原始 429，不把 3 次解析重试名额烧完。"""
    calls = {"invoke": 0}

    def fake_rate_limited(role, fn, overrides=None, deadline=None):
        calls["invoke"] += 1
        raise RuntimeError("Error code: 429 - rate limit exceeded")

    monkeypatch.setattr(L, "_invoke_with_rate_limit_retry", fake_rate_limited)
    cls = _model_cls()
    with pytest.raises(RuntimeError, match="429"):
        L.structured_call("mockgen", cls, "sys", "user", max_retries=3)
    # 通道 A 1 次 + 通道 B 第 1 次即终止；旧行为会烧满 A1 + B3 = 4 次
    assert calls["invoke"] == 2


def test_rate_limit_deadline_trims_wait(monkeypatch):
    """外层共享 deadline 早于单调用预算时，等待被裁剪：一次退避都不该发生。"""
    sleeps: list[float] = []
    monkeypatch.setattr(L, "RATE_LIMIT_RETRIES", 5)
    monkeypatch.setattr(L, "RATE_LIMIT_MAX_WAIT", 60.0)
    monkeypatch.setattr(L, "RATE_LIMIT_BASE_SLEEP", 100.0)
    monkeypatch.setattr(L, "_key_pool", lambda role: ["only-key"])
    monkeypatch.setattr(L.time, "sleep", lambda s: sleeps.append(s))

    def always_429(_llm):
        raise RuntimeError("Error code: 429 - tpm exhausted")

    with pytest.raises(RuntimeError, match="429"):
        L._invoke_with_rate_limit_retry("evaluator", always_429, deadline=time.monotonic() + 10.0)
    # 首次退避想等 100s（单 Key 加倍 200s），剩余预算仅 ~10s → 一次都不睡
    assert sleeps == []


def test_total_wait_budget_covers_max_wait():
    """总预算必须不小于单调用预算，否则通道 B 拿不到任何等待窗口。"""
    assert L.RATE_LIMIT_TOTAL_WAIT >= L.RATE_LIMIT_MAX_WAIT
