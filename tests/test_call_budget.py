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
# ⑤ 真端点实证：本地"读完就掐"的 server，数它真实收到几次请求
# --------------------------------------------------------------------------
# 为什么不是"装死挂住"：客户端 timeout 与端点线程被调度的先后是一场**竞赛**。本机带
# `--cov` 跑时 coverage 要给每个新线程装 tracer，handler 线程在客户端 1s 超时之后才起来
# 读请求，读到的是已关闭的连接（WinError 10053），记账停在 0 ⇒ 这两条实证连红 5 轮，
# 不带 --cov 又全绿。判据挂在时序上就是错的，跟机器快慢、跟"环境有没有坏"都无关。
# 改成"读完请求才掐断"：失败由端点亲手制造，必然发生在记账之后 ⇒ 计数与调度解耦。
# 副作用是墙钟不再包含挂等（本文件 autouse 夹具还把 sleep 打成 no-op），所以这里
# 不再断言墙钟，改为断言"这个异常确实会被重试层认成瞬时故障"——那条才是计数的解释权。


class _CuttingHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    hits: ClassVar[list[float]] = []

    def do_POST(self) -> None:
        n = int(self.headers.get("content-length") or 0)
        self.rfile.read(n)
        type(self).hits.append(time.monotonic())  # 先记账，再制造失败
        self.close_connection = True  # 不给响应：连接关掉，客户端立刻拿到连接错误

    def log_message(self, *a: Any) -> None:
        pass


def _cutting_client(monkeypatch, retries: int):
    """起一个"每发都读完就掐"的本地端点，返回 (server, 打一发并返回落点异常的钩子)。"""
    _CuttingHandler.hits = []
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", 0), _CuttingHandler)
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
        timeout=5.0,  # 只是兜底：正常路径上失败由端点掐断制造，等不到这 5s
        max_retries=L.SDK_RETRIES,
    )

    def call_once() -> Exception:
        try:
            L._invoke_with_conn_retry("evaluator", lambda: llm.invoke([("human", "hi")]))
        except AssertionError:
            raise
        except Exception as e:
            # 前置条件，不是放宽：失败必须由**端点掐断**制造。如果这里是 timeout，说明
            # handler 线程连 5s 都没被调度到 —— 那计数就不可解释，必须报成"实证环境坏了"，
            # 而不是让人去读一个看起来像"重试层多打了/少打了"的差值。
            text = f"{type(e).__name__}: {e}".lower()
            if "timed out" in text or "timeout" in text:
                raise AssertionError(
                    "端点没抢先掐断，是客户端自己等满 timeout 才失败的"
                    f"（{type(e).__name__}: {e}）⇒ 本次计数不可解释，先查这台机器能不能"
                    "在 5s 内调度起一个 handler 线程，别引用下面任何数字"
                ) from e
            return e
        raise AssertionError("端点掐断了连接却调用成功 ⇒ 失败没走到重试层，计数无意义")

    return srv, call_once


def test_endpoint_hits_are_our_visible_retry_count(monkeypatch: pytest.MonkeyPatch) -> None:
    """端点收到的请求数 == 我方那层**可见**的连接重试数，不含 SDK 的隐式倍增。

    修复前这里量到的是 (1+SDK 2)×(1+我方) —— 隐式那层不看代码看不见、日志不留痕。
    """
    srv, call_once = _cutting_client(monkeypatch, retries=2)
    try:
        err = call_once()
        assert L._is_transient_conn_error(err), (
            f"端点掐断的连接没被认成瞬时故障（{type(err).__name__}: {err}）"
            "⇒ 计数是别的东西贡献的，这条断言的解释权已经丢了"
        )
        assert len(_CuttingHandler.hits) == 1 + L.TRANSIENT_CONN_RETRIES, (
            f"端点收到 {len(_CuttingHandler.hits)} 次，我方只允许 1+{L.TRANSIENT_CONN_RETRIES} 次"
        )
    finally:
        srv.shutdown()
        srv.server_close()


def test_sdk_layer_adds_no_invisible_hits(monkeypatch: pytest.MonkeyPatch) -> None:
    """把我方重试也关掉 ⇒ 端点掐断一次连接就应当**只**收到一发。

    这条才是"SDK 隐式重试被钉住"的端点级实证：缺省 `max_retries=2` 时这里会收到 3 次
    （SDK 对连接错误同样重试），我方名额已经清零，多出来的只可能来自隐式层。
    """
    srv, call_once = _cutting_client(monkeypatch, retries=0)
    try:
        err = call_once()
        assert len(_CuttingHandler.hits) == 1, (
            f"端点收到 {len(_CuttingHandler.hits)} 次，隐式层又长回来了（末发异常：{err}）"
        )
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


# ---------------------------------------------------------------------------
# 四条"只被时序撞上才覆盖"的分支（2026-10-05 补）
#
# 连跑两遍全量套件，本文件的 pm/llm.py 未覆盖行在 21 ↔ 25 之间摆，逐行差集
# 精确到 410 / 734 / 1286-1288。那不是一个数字难看的问题，而是**这四条分支的
# 正确性过去没有任何用例在管**：多数轮次里它们根本不执行，谁改坏了也不会红。
# 与 vkeys 那三条退避分支同一形状，所以用同一方法——条件注入，不等真时序。
# ---------------------------------------------------------------------------


def test_budget_expiry_has_two_arms_and_both_are_reachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """410 行的两臂：没到线判"未到期"、到了线判"到期"。

    之前只有其中一臂被执行过（另一臂靠真时序恰好走到），于是把 `<= 0` 写成 `< 0`
    这类改法在多数轮次里不会让任何测试变红——差的那 1 秒正好是"预算等于 0 时
    第一发该不该直接拒"的行为差别。
    """
    clock = _Clock()
    monkeypatch.setattr(L.time, "monotonic", clock)
    budget = L.WallBudget("evaluator", 10.0)
    assert budget.expired() is False
    clock.advance(9.9)
    assert budget.expired() is False, "还没到线就判到期，会把可用预算白扔掉"
    clock.advance(0.2)
    assert budget.expired() is True
    assert budget.remaining() <= 0


@pytest.mark.parametrize(
    "env_base_url,expect_present",
    [(None, False), ("https://example.test/v1", True)],
    ids=["没设 base_url", "设了 base_url"],
)
def test_base_url_is_passed_only_when_configured(
    monkeypatch: pytest.MonkeyPatch, env_base_url: str | None, expect_present: bool
) -> None:
    """734 行的两臂。缺 base_url 时不能把空串塞进 ChatOpenAI。

    空串会让 SDK 用**它自己的默认端点**，于是"配置指向 A、实际打到 B"——
    这类错误在账单和读数上都看不出来（本项目 H1 缓存指纹事故就是它的近亲）。
    """
    monkeypatch.delenv("PM_BASE_URL", raising=False)
    if env_base_url is not None:
        monkeypatch.setenv("PM_BASE_URL", env_base_url)
    captured = _capture_openai_kwargs(monkeypatch)
    assert ("base_url" in captured) is expect_present
    if expect_present:
        assert captured["base_url"] == env_base_url


def _conn_error_llm(clock: _Clock) -> _HangingLLM:
    """把"每发吃满 timeout 后抛超时"换成"抛连接错"——形态相同、分类不同。"""
    fake = _HangingLLM(clock)

    def invoke(_messages: object) -> object:
        fake.n += 1
        clock.advance(fake.per_attempt)
        raise Exception(_CONN_TEXT)

    fake.invoke = invoke  # type: ignore[method-assign]
    return fake


def test_transient_conn_error_with_budget_left_reraises_original(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """1286 假臂 → 1288：预算还剩时**原样上抛**，且不再叠加解析重试名额。

    注意"原样上抛"不等于"只发一发"：传输层自己那一链（3 发）是有意保留的，
    这条真正拦的是旧写法在这里又套一层"再烧两次、每次再吃满一整条重试链"——
    那层叠加才是实测 45 分钟挂死的另一半来源。
    """
    clock = _Clock()
    monkeypatch.setattr(L.time, "monotonic", clock)
    monkeypatch.setattr(L, "CALL_BUDGET", 0.0)  # 预算关着 → budget 为 None
    fake = _conn_error_llm(clock)
    monkeypatch.setattr(L, "get_llm", lambda role, overrides=None: fake)

    with pytest.raises(Exception) as got:
        L.structured_call("evaluator", EvaluationResult, "sys", "user")
    assert "Connection error" in str(got.value), (
        f"应该原样上抛，实测：{type(got.value)}: {got.value}"
    )
    assert not isinstance(got.value, L.CallBudgetExceeded)
    # 6 = 传输层一链 (1+PM_CONN_RETRIES=2) 3 发 × 两条通道（原生结构化输出 + JSON 文本降级）。
    # 这里要钉的是"不再乘上解析重试名额"：旧写法是 3 通道轮 × 3 发 × max_retries=3 = 18 发，
    # 那才是实测 45 分钟挂死的另一半来源。所以断言取实际形态 6，而不是想当然的 1。
    assert fake.n == 6, f"层叠比例变了要重新算最坏墙钟：n={fake.n}（预期 6，旧缺陷形态是 18）"


def test_transient_conn_error_with_exhausted_budget_raises_budget_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """预算见底 + 瞬时网络错 ⇒ 上抛的是 `CallBudgetExceeded`（不是 `Connection error`）。

    两个错都成立时选更可执行的那个：`Connection error` 让人去查网络，
    "墙钟预算 600s 用尽（实际 Xs，已发 N 次请求）"才告诉人该调哪个参数。

    ⚠️ 这条**没有**覆盖 `pm/llm.py:1287`，别照着函数名去信。实测（2026-10-05，
    `--cov=pm.llm --cov-report=term-missing` 逐行核对）：预算见底时抛点在内层
    `_invoke_with_conn_retry`（479 / 492 两处 `budget.exceeded(...)`），
    异常到外层已是 `CallBudgetExceeded`，于是走 1280-1281 的 `isinstance` 分支，
    外层 1286-1287 到不了。**这条留作"疑似死分支"的取证**：如果哪天要简化重试层，
    判据就是"能不能构造出一个绕过内层检查、直接到 1282 的瞬时错"。
    """
    clock = _Clock()
    monkeypatch.setattr(L.time, "monotonic", clock)
    monkeypatch.setattr(L, "CALL_BUDGET", 50.0)
    monkeypatch.setattr(L, "TRANSIENT_CONN_RETRIES", 0)
    fake = _conn_error_llm(clock)  # 每发吃 100s，一发就把 50s 预算穿掉
    monkeypatch.setattr(L, "get_llm", lambda role, overrides=None: fake)

    with pytest.raises(L.CallBudgetExceeded) as got:
        L.structured_call("evaluator", EvaluationResult, "sys", "user")
    info = got.value.details
    assert info["requests"] >= 1, f"预算错必须带上真实请求数：{info}"
    assert "Connection error" in info["last_error"], f"最后一次错误没带现场：{info}"
