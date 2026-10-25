"""台账页的存储型 XSS 回归（2026-10-31 只读审计实测）。

`name` / `deliver_date` 等字段的写入方是**公开的** POST /api/orders（name 仅限长度）。
旧版把它们裸拼进 innerHTML，于是 `<img src=x onerror=alert(1)>` 原样落盘、
运营者一开台账页就执行。exec 场景下「只限长度」不是防线，转义才是。

判据用「裸插值形态」而不是「剥掉 esc 再匹配」：后者在内层有括号时会假阳。
"""

from __future__ import annotations

import re
from pathlib import Path

PAGE = Path(__file__).resolve().parents[1] / "static" / "ledger.html"


def _js() -> str:
    src = PAGE.read_text(encoding="utf-8")
    return "\n".join(re.findall(r"<script>(.*?)</script>", src, re.S))


def test_page_defines_an_escaper() -> None:
    assert "function esc" in _js(), "台账页没有转义函数"


def test_untrusted_order_fields_are_escaped() -> None:
    js = _js()
    for dangerous in (
        "<td>${o.name}",
        '<td class="mono">${o.deliver_date',
        "${o.order_id}<br>",
        "onclick=\"transit('${o.order_id}",
        "grade ${o.spec}",
    ):
        assert dangerous not in js, f"发现未转义的订单字段 `{dangerous}`：公开下单接口可注入"
    for must in ("esc(o.name)", "esc(o.deliver_date", "esc(o.order_id)"):
        assert must in js, f"转义没有真的接到 {must} 上"


def test_no_raw_interpolation_of_names_anywhere_in_the_table() -> None:
    r"""兜底：表格渲染段里不允许出现任何 `\${o.<字段>}` 的裸形态。"""
    js = _js()
    start = js.find("innerHTML = (d.orders")
    assert start >= 0, "没找到订单表格渲染段（页面结构变了，本用例需同步）"
    segment = js[start : start + 2500]
    bare = re.findall(r"\$\{o\.(?!\s*\))[a-z_]+\}", segment)
    assert not bare, f"表格里还有裸插值：{bare[:5]}"

def test_401_is_not_painted_as_empty_data() -> None:
    """设了 token 后页面拿不到数：必须说"需要令牌"，不许画成"暂无订单"。

    实测缺陷（2026-10-31 审计）：页面 JS 从不发 X-API-Key（也没有拿到它的途径），
    而 401 的 `{"detail":…}` 被当正常数据 ⇒ 台账显示"undefined 笔 / 暂无订单"。
    用空集冒充没数据，是这一整类里最坏的一种谎。
    """
    js = _js()
    assert "!r.ok" in js, "取数没有检查响应码"
    assert "401" in js and "X-API-Key" in js, "401 没有给出可读原因"
