"""卡死机修复的回归钉：一次调用最坏挂多久，必须由**声明的预算**说了算，不能由层叠说了算。

实测背景（2026-10-03，本地装死端点探针，零出网）：
- `PM_TIMEOUT=300` 时一次 `invoke()` 真实打到端点 **3 次**。倍增来自 **openai SDK 的隐式
  `max_retries=2`**（`DEFAULT_MAX_RETRIES=2`），不是我方 `_invoke_with_conn_retry`——
  后者对真实超时文本 `OpenAITimeoutError: Request timed out.` 判 `transient=False`，一发都不重试。
- 同一条链再叠通道 B 的 3 次解析重试（每次各吃满 3×timeout）⇒ 单次 `structured_call` 上界约 1 小时。
  2026-10-02 真端点轮量到的 15 分钟日志空洞就是那个 3×300s。
- 历史真实延迟：24 条 >60s 的单调用记录里最慢 **122.2s**（revise 的 plain 调用）⇒ 600s 预算
  放得下 3~4 次正常尝试，只砍病态形态。

钉的四件事：①SDK 层显式钉住（不再有看不见的重试层）；②超时/5xx 按**实测文本与状态码**分类，
不靠响应体子串；③墙钟预算在**每次真实发起之前**检查（旧版 `deadline` 只约束 sleep，挂住的调用不受约束）；
④层叠算术一旦反弹就红。
"""

from __future__ import annotations

import json
import sys
import threading
import time
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar

import pytest
from pm import llm as L
from pm.schemas import EvaluationResult

# 探针量到的真实异常文本，原样照抄，不要改写成"更好看"的形式
_TIMEOUT_TEXT = "OpenAITimeoutError: Request timed out."
_CONN_TEXT = "OpenAIConnectionError: Connection error."


class _Resp:
    def __init__(self, content: str = "{}"):
        self.content = content
        self.usage_metadata: dict[str, int] = {}
        self.response_metadata: dict[str, Any] = {}


class _Clock:
    """可控时钟：假调用用 `advance()` 表示"这一发吃掉了多久"，读表本身不计时。"""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class _HangingLLM:
    """每次 invoke 假装吃满一个 timeout 后抛超时——真实故障的形态。"""

    def __init__(self, clock: _Clock, per_attempt: float = 100.0):
        self.clock = clock
        self.per_attempt = per_attempt
        self.n = 0

    def invoke(self, _messages: Any) -> Any:
        self.n += 1
        self.clock.advance(self.per_attempt)
        raise Exception(_TIMEOUT_TEXT)

    def with_structured_output(self, *_a: Any, **_k: Any) -> _HangingLLM:
        return self

    def with_config(self, *_a: Any, **_k: Any) -> _HangingLLM:
        return self


def _exc_with_status(status: int, text: str) -> Exception:
    e = Exception(text)
    e.status_code = status  # type: ignore[attr-defined]
    return e


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    monkeypatch.setattr(L.time, "sleep", lambda _s: None)
    monkeypatch.setenv("PM_API_KEY", "sk-test")
    # 单发 timeout 钉成 100s：假调用每发正好吃掉这么多，预算算式才对得上真实形态
    monkeypatch.setenv("PM_TIMEOUT", "100")
    for k in ("PM_API_KEYS", "PM_SDK_RETRIES", "PM_CALL_BUDGET", "PM_CONN_RETRIES"):
        monkeypatch.delenv(k, raising=False)
    yield


# --------------------------------------------------------------------------
# ① SDK 层必须被显式钉住：看不见的那一层就是量到的那 3×
# --------------------------------------------------------------------------
def _capture_openai_kwargs(monkeypatch, **env: str) -> dict[str, Any]:
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    captured: dict[str, Any] = {}

    class _FakeChatOpenAI:
        def __init__(self, **kw: Any):
            captured.update(kw)

    stub = types.ModuleType("langchain_openai")
    stub.ChatOpenAI = _FakeChatOpenAI  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "langchain_openai", stub)
    L.get_llm("evaluator")
    return captured


def test_get_llm_pins_sdk_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """没显式钉住时 openai SDK 自己重试 2 次 ⇒ 一次 invoke 最坏 3×PM_TIMEOUT。"""
    assert L.SDK_RETRIES == 0, "缺省必须关掉 SDK 层重试，重试决策只留我方一层"
    kw = _capture_openai_kwargs(monkeypatch)
    assert kw.get("max_retries") == 0, f"ChatOpenAI 未收到 max_retries：{sorted(kw)}"


def test_sdk_retries_is_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    """要显式打开也走同一个开关，不许散落在构造点各写各的。

    开关在 import 期求值（与 `PM_CONN_RETRIES` 等一致），所以这里钉常量而不是环境变量。
    """
    monkeypatch.setattr(L, "SDK_RETRIES", 2)
    kw = _capture_openai_kwargs(monkeypatch)
    assert kw.get("max_retries") == 2


def test_new_knobs_are_documented_in_env_example() -> None:
    """代码里加了开关、`.env.example` 里没有 = 下一次卡死事故仍然没人知道有闸可拧。

    这条是"示例与代码分叉"的最小闭环（本仓历史上一次性漏过 11 个 PM_*）。
    """
    example = (Path(__file__).resolve().parent.parent / ".env.example").read_text(encoding="utf-8")
    for name in ("PM_CALL_BUDGET", "PM_SDK_RETRIES"):
        assert name in example, f".env.example 缺 {name}"
    # 而且要说清"0 是什么语义"，否则照抄一个数就以为懂了
    assert "0 = 不设上限" in example


# --------------------------------------------------------------------------
# ② 分类按实测文本与状态码，不靠响应体子串
# --------------------------------------------------------------------------
def test_real_timeout_text_is_transient() -> None:
    """真实超时文本此前**永不命中** hint ⇒ 那两条 readtimeout 是死代码。"""
    assert L._is_transient_conn_error(Exception(_TIMEOUT_TEXT)) is True


def test_status_code_5xx_is_transient_even_without_hint_text() -> None:
    """探针实测 503 文本是 `Error code: 503 - {'error': ...}`，不含 '503 service' ⇒ 旧版漏判。"""
    e = _exc_with_status(503, "OpenAIAPIError: Error code: 503 - {'error': {'message': 'quota'}}")
    assert L._is_transient_conn_error(e) is True


def test_client_errors_and_rate_limit_stay_non_transient() -> None:
    assert L._is_transient_conn_error(ValueError("dimension_scores Field required")) is False
    assert (
        L._is_transient_conn_error(_exc_with_status(400, "Error code: 400 - bad request")) is False
    )
    # 429 归限流通道（退避 + 换 Key），不能同时被连接重试吃掉名额
    assert L._is_transient_conn_error(_exc_with_status(429, "Error code: 429 - slow down")) is False
    assert L._is_rate_limit_error(_exc_with_status(429, "Error code: 429 - slow down")) is True


def test_plain_connection_error_still_transient() -> None:
    """原有能力不许退化：第 6 轮真实事故靠这条救回来的。"""
    assert L._is_transient_conn_error(Exception(_CONN_TEXT)) is True


# --------------------------------------------------------------------------
# ③ 墙钟预算：约束"工作"，不只是约束 sleep
# --------------------------------------------------------------------------
def test_conn_retry_stops_when_wall_deadline_is_gone(monkeypatch: pytest.MonkeyPatch) -> None:
    """旧版：连接类故障按 PM_CONN_RETRIES 无墙钟上限原地重试。"""
    clock = _Clock()
    monkeypatch.setattr(L.time, "monotonic", clock)
    monkeypatch.setattr(L, "TRANSIENT_CONN_RETRIES", 5)  # 名额给够，看的是预算能不能拦住
    fake = _HangingLLM(clock)

    with pytest.raises(L.CallBudgetExceeded) as got:
        L._invoke_with_conn_retry(
            "evaluator", lambda: fake.invoke([]), budget=L.WallBudget("evaluator", 250.0)
        )

    assert fake.n == 3, f"每发 100s、预算 250s，却发了 {fake.n} 发"
    msg = str(got.value)
    assert "evaluator" in msg and "预算" in msg, f"失败现场没写清谁的多少预算：{msg[:120]}"


def test_wall_deadline_clamps_per_attempt_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """剩余预算不足一个 timeout 时要把客户端 timeout 夹小，否则最后一发直接冲过预算。"""
    clock = _Clock()
    monkeypatch.setattr(L.time, "monotonic", clock)
    seen: list[dict[str, Any]] = []
    fake = _HangingLLM(clock, per_attempt=5.0)

    def fake_get_llm(role: str, overrides: dict[str, Any] | None = None) -> Any:
        seen.append(dict(overrides or {}))
        return fake

    monkeypatch.setattr(L, "get_llm", fake_get_llm)
    base = L.build_config("evaluator").timeout
    with pytest.raises(L.CallBudgetExceeded):
        L._invoke_with_rate_limit_retry(
            "evaluator",
            lambda llm: llm.invoke([]),
            budget=L.WallBudget("evaluator", 9.5),
        )
    assert seen, "没经过 get_llm，夹取无处生效"
    for ov in seen:
        assert "timeout" in ov, f"未按剩余预算夹 timeout：{ov}"
        assert 0 < ov["timeout"] <= min(base, 9.5), f"夹出来的 timeout 越界：{ov}"


def test_structured_call_raises_budget_error_instead_of_burning_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """预算耗尽要当场停，不能把剩下的解析重试名额各再烧一个 timeout。"""
    clock = _Clock()
    monkeypatch.setattr(L.time, "monotonic", clock)
    monkeypatch.setattr(L, "CALL_BUDGET", 250.0)
    fake = _HangingLLM(clock)
    monkeypatch.setattr(L, "get_llm", lambda role, overrides=None: fake)

    with pytest.raises(L.CallBudgetExceeded):
        L.structured_call("evaluator", EvaluationResult, "sys", "user")
    assert fake.n <= 3, f"通道 A+B 烧了 {fake.n} 发，预算形同虚设"


def test_plain_call_honors_the_same_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _Clock()
    monkeypatch.setattr(L.time, "monotonic", clock)
    monkeypatch.setattr(L, "CALL_BUDGET", 250.0)
    fake = _HangingLLM(clock)
    monkeypatch.setattr(L, "get_llm", lambda role, overrides=None: fake)

    with pytest.raises(L.CallBudgetExceeded):
        L.plain_call("target", "sys", "user")
    assert fake.n <= 3


def test_budget_survives_a_slow_but_successful_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """预算是给病态形态用的，不该把"慢但会成功"的正常重试砍掉。"""
    clock = _Clock()
    monkeypatch.setattr(L.time, "monotonic", clock)
    monkeypatch.setattr(L, "CALL_BUDGET", 600.0)
    n = 0

    class _L:
        def invoke(self, _m: Any) -> Any:
            nonlocal n
            n += 1
            clock.advance(100)  # 每发 100s，与实测最慢成功调用 122s 同量级
            if n < 3:
                raise Exception(_TIMEOUT_TEXT)
            return _Resp('{"ok": true}')

        def with_structured_output(self, *_a: Any, **_k: Any) -> Any:
            return self

        def with_config(self, *_a: Any, **_k: Any) -> Any:
            return self

    monkeypatch.setattr(L, "get_llm", lambda role, overrides=None: _L())
    text, _meta = L.plain_call("target", "sys", "user")
    assert text == '{"ok": true}'
    assert n == 3


# --------------------------------------------------------------------------
# ④ 层叠算术：谁再叠一层看不见的重试，这条就红
# --------------------------------------------------------------------------
def test_worst_case_is_bounded_by_the_declared_budget() -> None:
    """最坏墙钟 = 预算 + 至多一发尝试（预算只在发与发之间检查）。

    SDK 层一旦回到缺省 2，`(1+SDK)×(1+conn)×timeout` 就冲破这条界——
    那正是 2026-10-02 量到 15 分钟空洞的形状。
    """
    timeout = L._int_env("PM_TIMEOUT", 120)
    chain = (1 + L.SDK_RETRIES) * (1 + L.TRANSIENT_CONN_RETRIES) * timeout
    assert chain <= L.CALL_BUDGET + timeout, (
        f"单发链条 {chain}s 已超「预算+一发」（{L.CALL_BUDGET + timeout}s）⇒ 层叠又长回来了"
    )


def test_budget_zero_means_no_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """`PM_CALL_BUDGET=0` 显式回到旧行为（不设上限），给长跑留出口，不是悄悄失效。"""
    monkeypatch.setattr(L, "CALL_BUDGET", 0.0)
    n = 0

    class _L:
        def invoke(self, _m: Any) -> Any:
            nonlocal n
            n += 1
            if n < 3:
                raise Exception(_CONN_TEXT)
            return _Resp('{"ok": true}')

        def with_config(self, *_a: Any, **_k: Any) -> Any:
            return self

    monkeypatch.setattr(L, "get_llm", lambda role, overrides=None: _L())
    text, _meta = L.plain_call("target", "sys", "user")
    # 没有预算时连接重试名额就是唯一的闸门：1 发 + PM_CONN_RETRIES(2) = 3 发
    assert n == 3, f"预算关掉时应当正好用满连接重试名额，实际 {n} 发"
    assert text == '{"ok": true}'


# --------------------------------------------------------------------------
# ⑤ 真端点实证：本地装死 server，数它真实收到几次请求
# --------------------------------------------------------------------------
# 本文件的 autouse 夹具会把 `time.sleep` 打成 no-op（为了不退避 5/10/20s）。
# 装死端点必须**真的**挂住，所以这里在 import 期就把真实 sleep 绑住，
# 不受后续任何 monkeypatch 影响。
_REAL_SLEEP = time.sleep


class _HangingHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    hits: ClassVar[list[float]] = []

    def do_POST(self) -> None:
        n = int(self.headers.get("content-length") or 0)
        self.rfile.read(n)
        type(self).hits.append(time.monotonic())
        _REAL_SLEEP(3)  # 装死，比客户端 timeout 长
        try:
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", "2")
            self.end_headers()
            self.wfile.write(b"{}")
        except OSError:
            pass

    def log_message(self, *a: Any) -> None:
        pass


def _hanging_client(monkeypatch, retries: int):
    """起一个"每发都挂 3s"的本地端点，返回 (ChatOpenAI, 关掉我方重试的钩子, 端点命中数)。"""
    _HangingHandler.hits = []
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", 0), _HangingHandler)
    except OSError:  # pragma: no cover - 沙箱/CI 禁绑定时跳过，不算绿
        pytest.skip("本地无法绑定端口 ⇒ 端点实证改跑 scripts/measure_retry_layering.py")
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setattr(L, "TRANSIENT_CONN_RETRIES", retries)
    from langchain_openai import ChatOpenAI

    llm = ChatOpenAI(
        api_key="sk-test",
        model="probe",
        base_url=f"http://127.0.0.1:{port}/v1",
        timeout=1.0,
        max_retries=L.SDK_RETRIES,
    )

    def call_once() -> float:
        t0 = time.monotonic()
        try:
            L._invoke_with_conn_retry("evaluator", lambda: llm.invoke([("human", "hi")]))
            raise AssertionError("端点装死却调用成功 ⇒ timeout 没生效，计数无意义")
        except AssertionError:
            raise
        except Exception as e:  # noqa: BLE001 - SDK 异常类型随版本变，判据放在文本上
            assert "timed out" in str(e).lower(), f"不是超时，计数解释不了：{type(e).__name__}: {e}"
        return time.monotonic() - t0

    return srv, call_once


def test_endpoint_hits_are_our_visible_retry_count(monkeypatch: pytest.MonkeyPatch) -> None:
    """装死端点收到的请求数 == 我方那层**可见**的连接重试数，不含 SDK 的隐式倍增。

    修复前这里量到的是 (1+SDK 2)×(1+我方) —— 隐式那层不看代码看不见、日志不留痕。
    """
    srv, call_once = _hanging_client(monkeypatch, retries=2)
    try:
        wall = call_once()
        assert len(_HangingHandler.hits) == 1 + L.TRANSIENT_CONN_RETRIES, (
            f"端点收到 {len(_HangingHandler.hits)} 次，我方只允许 1+{L.TRANSIENT_CONN_RETRIES} 次"
        )
        assert wall >= 2.5, f"三发各挂 1s 却只花了 {wall:.1f}s，timeout 没生效"
    finally:
        srv.shutdown()
        srv.server_close()


def test_sdk_layer_adds_no_invisible_hits(monkeypatch: pytest.MonkeyPatch) -> None:
    """把我方重试也关掉 ⇒ 一次超时应当**只**打端点一次。

    这条才是"SDK 隐式重试被钉住"的端点级实证：缺省 `max_retries=2` 时这里会收到 3 次。
    """
    srv, call_once = _hanging_client(monkeypatch, retries=0)
    try:
        wall = call_once()
        assert len(_HangingHandler.hits) == 1, (
            f"端点收到 {len(_HangingHandler.hits)} 次，隐式层又长回来了"
        )
        assert wall < 2.0, f"单发 1s timeout 却挂了 {wall:.1f}s"
    finally:
        srv.shutdown()
        srv.server_close()


def test_small_budget_still_fires_the_first_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    """预算比 `PM_TIMEOUT` 小时，第一发仍然要发得出去（timeout 已被夹进预算）。

    这条钉的是修复过程中我自己踩出来的错：绝对下限 5s 把**第一发**一起拦掉了 ⇒
    装死端点差分实测到"端点收到 0 次请求、墙钟 0s，报的却是预算用尽"——
    读起来像端点坏了，其实是闸门装反。见 `.scratch` 的差分记录。
    """
    clock = _Clock()
    monkeypatch.setattr(L.time, "monotonic", clock)
    monkeypatch.setattr(L, "CALL_BUDGET", 3.0)
    used: list[float] = []

    class _L:
        def __init__(self, timeout: float):
            self.timeout = timeout

        def invoke(self, _m: Any) -> Any:
            used.append(self.timeout)
            clock.advance(self.timeout)
            raise Exception(_TIMEOUT_TEXT)

        def with_config(self, *_a: Any, **_k: Any) -> Any:
            return self

    monkeypatch.setattr(
        L, "get_llm", lambda role, overrides=None: _L(float((overrides or {}).get("timeout", 100)))
    )
    with pytest.raises(L.CallBudgetExceeded):
        L.plain_call("target", "sys", "user")
    assert used == [3.0], f"第一发没被夹进预算，或根本没发出去：{used}"


def test_meta_reports_wall_clock_and_request_count(monkeypatch: pytest.MonkeyPatch) -> None:
    """预算被谁吃掉、打了几发，必须进产物；不然 15 分钟静默在日志里是隐形的。"""
    clock = _Clock()
    monkeypatch.setattr(L.time, "monotonic", clock)
    monkeypatch.setattr(L, "CALL_BUDGET", 250.0)
    fake = _HangingLLM(clock)
    monkeypatch.setattr(L, "get_llm", lambda role, overrides=None: fake)
    with pytest.raises(L.CallBudgetExceeded) as got:
        L.structured_call("evaluator", EvaluationResult, "sys", "user")
    info = got.value.details
    assert info["requests"] == fake.n, f"请求数没对上：{info} vs n={fake.n}"
    assert info["budget_s"] == 250
    assert info["role"] == "evaluator"
    assert 100 <= info["wall_s"] <= 350, f"墙钟读数不在「预算+一发」界内：{info}"
    assert "Request timed out" in info["last_error"], f"最后一次错误没带上现场：{info}"


def test_json_extract_helper_still_works() -> None:
    """防手滑：本文件依赖 extract_json_object 的既有语义。"""
    assert L.extract_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert json.loads('{"b": 2}') == {"b": 2}
