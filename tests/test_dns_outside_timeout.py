"""DNS 解析挂起**不受 httpx timeout 约束**（§三十·二十三）。

实测证据（2026-10-07，零成本，本地黑洞）：

| 失败形态          | 声明 timeout | 实际耗时 | 结论 |
| ----------------- | ------------ | -------- | ---- |
| 拒绝连接          | 300s         | 2.08s    | ✅ |
| listen 但不回话   | 300s         | 300.01s  | ✅ 精确生效 |
| 不可路由 IP       | 300s         | 21.05s   | ✅ |
| **DNS 挂起**      | **300s**     | **420.4s** | ❌ **跟 DNS 走完** |

⇒ 所以 `PM_TIMEOUT` / `PM_CALL_BUDGET` 只能约束**连接与读取**，
   约束不了**域名解析**。一次 DNS 挂起就能让「墙钟预算 600s」变成 10581s
   （§三十·二十二·四 实测那发）。

这一节把它钉成回归：改动 DNS/超时相关代码后必须仍然是红的。

§三十·二十三·五 在 httpx **之上**补了「有界预解析」（`PM_DNS_BUDGET`，默认 5s）——
它不改 httpx 的行为，所以上面两条库行为判据原样保留（"翻红改判据"的时机是
httpx 本身管住 DNS 之时）；下面追加的判据钉的是修复本身。
"""

from __future__ import annotations

import socket
import threading
import time

import httpx
import pm.llm as L
import pytest


def test_httpx_timeout_does_not_bound_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """httpx 的 timeout 管到 connect/read，**管不到 getaddrinfo**。

    判据：DNS 挂 1.5s、timeout 0.3s ⇒ 实际耗时应 >1.5s（跟着 DNS 走完）。
    用秒级数字而不是 300/420，因为判据要能在测试里跑完。
    ⚠️ 这是**故意钉住一个缺陷**：真修好之后本条会翻绿，
        那时把它改成"应 ≤0.5s"并同步 §三十·二十三 的结论。
    """
    dns_hang = 1.5
    timeout = 0.3
    real = socket.getaddrinfo

    def slow(host, port, *a, **k):  # type: ignore[no-untyped-def]
        if "pm-dns-hang-probe" in str(host):
            time.sleep(dns_hang)
            raise socket.gaierror("模拟 DNS 挂起")
        return real(host, port, *a, **k)

    monkeypatch.setattr(socket, "getaddrinfo", slow)
    start = time.monotonic()
    with (
        pytest.raises(Exception),  # noqa: B017
        httpx.Client(timeout=httpx.Timeout(timeout)) as c,
    ):
        c.get("http://pm-dns-hang-probe.invalid/")
    elapsed = time.monotonic() - start

    assert elapsed > dns_hang, (
        f"只耗 {elapsed:.2f}s：httpx 已经能拦住 DNS 了，"
        "请更新本条判据并同步 docs/evaluation.md §三十·二十三"
    )


def test_socket_defaulttimeout_cannot_bound_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """`socket.setdefaulttimeout` 也管不住 getaddrinfo（别指望它兜底）。"""
    dns_hang = 1.5
    real = socket.getaddrinfo

    def slow(host, port, *a, **k):  # type: ignore[no-untyped-def]
        if "pm-dns-hang-probe2" in str(host):
            time.sleep(dns_hang)
            raise socket.gaierror("模拟 DNS 挂起")
        return real(host, port, *a, **k)

    monkeypatch.setattr(socket, "getaddrinfo", slow)
    monkeypatch.setattr(socket, "setdefaulttimeout", lambda *_a, **_k: None)
    start = time.monotonic()
    try:
        socket.getaddrinfo("pm-dns-hang-probe2.invalid", 9)
    except socket.gaierror:
        pass
    elapsed = time.monotonic() - start
    assert elapsed > dns_hang * 0.8, (
        f"只耗 {elapsed:.2f}s：DNS 路径上有别的东西在起作用，请查清并更新判据"
    )


def test_the_budget_bound_assumes_dns_is_instant() -> None:
    """本项目声称的墙钟上界（预算 + 最后一发）**隐含假设 DNS 立刻返回**。

    这个假设以前是隐形的：`WallBudget` 的文档只说"发起前查剩余"，
    没人说它管不到解析。把它写成一个会失败的判据。
    """
    import inspect

    from pm.llm import WallBudget

    doc = inspect.getdoc(WallBudget) or ""
    assert "DNS" in doc, (
        "WallBudget 的文档没有声明它管不到域名解析——"
        "这就是「预算 600s 实际 10581s」一直没被当成 bug 看的原因"
    )
    assert "解析" in doc, "文档要说明：连接与读取受约束，解析不受"


# --------------------------------------------------------------------------
# 修复侧（§三十·二十三·五）：httpx 之上补一层「有界预解析」
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _fresh_dns_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """本文件的用例共用模块级正面缓存；每条换一只新缓存，互不串味。"""
    monkeypatch.setattr(L, "_DNS_RESOLVED", set())


def _gated_resolver(monkeypatch: pytest.MonkeyPatch, target: str) -> threading.Event:
    """把解析器换成"对 target 挂住"的假货，返回放行用的 Event。

    收尾必放行：不留一个拿着真 resolver 的悬挂线程——挂住的假解析器醒来后
    只 raise gaierror，绝不去碰真解析（真 resolver 只对**其它**主机才被调用）。
    """
    release = threading.Event()
    real = socket.getaddrinfo

    def gated(host, port, *a, **k):  # type: ignore[no-untyped-def]
        if target in str(host):
            release.wait(5.0)
            raise socket.gaierror("模拟 DNS 挂起")
        return real(host, port, *a, **k)

    monkeypatch.setattr(socket, "getaddrinfo", gated)
    return release


def test_bounded_resolution_raises_within_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """修复判据：解析预算 0.5s、解析器挂住 ⇒ ~0.5s 上抛 DnsResolutionTimeout。

    修复前这个形态实跑 1800s（§三十·二十三·一 实测表）；修复后要快到能在测试里等。
    上界给 2s：CI 慢机也要能过，但仍远小于「跟着解析器走完」的任何形态。
    """
    release = _gated_resolver(monkeypatch, "pm-dns-hang-fix")
    monkeypatch.setattr(L, "DNS_BUDGET", 0.5)
    start = time.monotonic()
    with pytest.raises(L.DnsResolutionTimeout) as got:
        L.resolve_host_bounded("evaluator", "pm-dns-hang-fix.invalid", 9, 0.5)
    elapsed = time.monotonic() - start
    release.set()
    assert elapsed < 2.0, f"花了 {elapsed:.2f}s：预解析没有按预算快速失败"
    # 必须是预算类错误：不能被 _is_transient_conn_error 认成可重试（否则重试把墙钟再烧一遍）
    assert not L._is_transient_conn_error(got.value)


def test_resolution_error_is_not_masked(monkeypatch: pytest.MonkeyPatch) -> None:
    """解析**报错**（NXDOMAIN 等）不在预解析上抛：真实请求会以它本来的
    异常类型与重试分类失败，预解析不抢戏。"""
    real = socket.getaddrinfo

    def broken(host, port, *a, **k):  # type: ignore[no-untyped-def]
        if "pm-dns-nxdomain" in str(host):
            raise socket.gaierror(8, "模拟 NXDOMAIN")
        return real(host, port, *a, **k)

    monkeypatch.setattr(socket, "getaddrinfo", broken)
    elapsed = L.resolve_host_bounded("evaluator", "pm-dns-nxdomain.invalid", 9, 2.0)
    assert elapsed < 2.0  # 返回而非上抛——上抛权留给真实请求


def test_successful_resolution_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    """成功解析进正面缓存：同一 host:port 第二次直接返回 0.0，不再开线程。"""
    calls: list[str] = []

    def fake(host, port, *a, **k):  # type: ignore[no-untyped-def]
        calls.append(str(host))
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]

    monkeypatch.setattr(socket, "getaddrinfo", fake)
    first = L.resolve_host_bounded("evaluator", "cache-probe.invalid", 9, 2.0)
    second = L.resolve_host_bounded("evaluator", "cache-probe.invalid", 9, 2.0)
    assert first >= 0.0
    assert second == 0.0, "命中缓存的解析应零成本返回"
    assert len(calls) == 1, f"缓存没生效：getaddrinfo 被调了 {len(calls)} 次"


def test_ip_literal_and_missing_base_url_never_touch_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """零出网的构造性保证：IP 字面量跳过解析；无显式 base_url 时预解析不开火。"""

    def boom(*_a: object, **_k: object) -> object:
        raise AssertionError("不应发起任何解析")

    monkeypatch.setattr(socket, "getaddrinfo", boom)
    assert L.resolve_host_bounded("evaluator", "127.0.0.1", 9, 5.0) == 0.0
    # conftest 清空连接配置后的测试默认态：base_url 恒为 None ⇒ 预解析不开火
    assert L._endpoint_host(L.build_config("evaluator")) is None
    # 显式配置才取得到 (host, port)，端口与协议默认也要取对
    assert L._endpoint_host(
        L.build_config("evaluator", {"base_url": "https://api.example.cn/v1"})
    ) == ("api.example.cn", 443)
    assert L._endpoint_host(L.build_config("evaluator", {"base_url": "http://10.0.0.1:9/v1"})) == (
        "10.0.0.1",
        9,
    )


def test_dns_budget_zero_disables_preflight(monkeypatch: pytest.MonkeyPatch) -> None:
    """`PM_DNS_BUDGET=0` = 关闭预解析（旧行为）：连 resolver 都不该被碰。"""

    def boom(*_a: object, **_k: object) -> object:
        raise AssertionError("预解析已关闭，不应发起解析")

    monkeypatch.setattr(socket, "getaddrinfo", boom)
    monkeypatch.setattr(L, "DNS_BUDGET", 0.0)
    monkeypatch.setenv("PM_BASE_URL", "http://some-host.invalid:9/v1")
    L._preflight_dns("evaluator", None, L.WallBudget("evaluator", 600.0), [0])


def test_preflight_fires_before_first_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """集成判据：DNS 挂起 + 显式 base_url + 有预算 ⇒ **发起任何端点请求之前**上抛。

    修复前的形态：counter=1、跟着解析器吃满 timeout（实测 10581s 那发就是 counter=1）。
    这条钉三件事：异常类型对（预算类、非重试类）、0 次真实请求、且快。
    """
    release = _gated_resolver(monkeypatch, "pm-dns-hang-fix")
    monkeypatch.setattr(L, "DNS_BUDGET", 0.5)
    monkeypatch.setenv("PM_BASE_URL", "http://pm-dns-hang-fix.invalid:9/v1")
    monkeypatch.setenv("PM_API_KEY", "sk-test")

    def must_not_build(_role: str, _overrides: dict[str, object] | None = None) -> object:
        raise AssertionError("预解析没拦住：走到了构造客户端/发请求这一步")

    monkeypatch.setattr(L, "get_llm", must_not_build)
    counter = [0]
    start = time.monotonic()
    with pytest.raises(L.DnsResolutionTimeout) as got:
        L._invoke_with_rate_limit_retry(
            "evaluator",
            lambda llm: llm.invoke([]),
            counter=counter,
            budget=L.WallBudget("evaluator", 600.0),
        )
    elapsed = time.monotonic() - start
    release.set()
    assert elapsed < 3.0, f"花了 {elapsed:.2f}s"
    assert counter[0] == 0, "DNS 挂起时不应有任何真实请求发出去"
    assert "pm-dns-hang-fix" in str(got.value)
    assert not L._is_transient_conn_error(got.value)
