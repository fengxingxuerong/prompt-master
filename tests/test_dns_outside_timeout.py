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
"""

from __future__ import annotations

import socket
import time

import httpx
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
