"""Web 控制台渲染安全回归（存储型 XSS 防线）。

背景：控制台把交付报告（含**任务原文与模型输出**）渲染进 innerHTML。
`md2html` 曾先 `esc()` 转义、再对代码块做 `c.replace(/&lt;/g,"<")` 反向还原 ——
于是报告里围栏内的 `<img src=x onerror=...>` 会以可执行形态进 DOM，等价于
任何能提交任务的人都能给控制台投毒（存储型 XSS）。

这里从两层把它钉死：
1. 源码层：禁止再出现任何"把 &lt; 还原成 <"的写法，且渲染入口必须是 esc()；
2. 行为层：本机有 node 时，直接抽出 esc + md2html 真跑一遍典型载荷，
   断言输出里除白名单标签外不存在任何原始尖括号（没有 node 则跳过，源码层断言仍生效）。
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pm import server as server_mod
from pm import web as web_mod

# 2026-09-18 pm/web.py 拆包为 pm/web/ 子包：WEB_CONSOLE_HTML 由 CSS/HTML/JS 三部分
# 组装而来（内容与拆分前逐字节一致）。这里直接读组装结果，正则断言照常工作。
WEB_SRC = web_mod.WEB_CONSOLE_HTML

# 允许出现在渲染结果里的标签（渲染器自身产生的结构标签）
_ALLOWED_TAGS = re.compile(r"</?(h1|h2|h3|pre|code|b|ul|li|table|thead|tbody|tr|th|td|blockquote)>")

# 典型注入载荷：报告正文里完全可能出现这些东西（模型输出、用户 task 原文）
_PAYLOADS = [
    "结果\n```\n<img src=x onerror=alert(document.domain)>\n```",
    "结果\n```\n<script>alert(1)</script>\n```",
    '结果\n```\n" onmouseover="alert(1)\n```',
    "结果\n```\n<iframe src=//evil.example></iframe>\n```",
    "正文 <img src=x onerror=alert(1)> 与 **加粗**",
]


def _extract_js() -> str:
    """从 pm/web.py 抽出 esc 与 md2html 的源码（供 node 直接执行）。"""
    esc = re.search(r"^const esc = .*$", WEB_SRC, re.M)
    fn = re.search(r"^function md2html\(md\)\{[\s\S]*?^\}", WEB_SRC, re.M)
    assert esc, "没找到 esc 定义（pm/web.py 结构变了，本用例需要同步更新）"
    assert fn, "没找到 md2html 定义（pm/web.py 结构变了，本用例需要同步更新）"
    return esc.group(0) + "\n" + fn.group(0)


# --------------------------------------------------------------------------
# 源码层
# --------------------------------------------------------------------------
def test_no_reverse_unescape_of_angle_brackets():
    """禁止任何把已转义的 &lt; / &gt; 还原成 < / > 的写法。"""
    offenders = [
        line.strip()
        for line in WEB_SRC.splitlines()
        # 注释里会引用旧写法作为反面教材，不算违规
        if not line.strip().startswith("//")
        and (
            re.search(r"replace\(/&lt;/g\s*,\s*[\"']<", line)
            or re.search(r"replace\(/&gt;/g\s*,\s*[\"']>", line)
        )
    ]
    assert not offenders, f"出现了反向还原转义的写法，会重新打开 XSS：{offenders}"


def test_md2html_escapes_before_rendering():
    """渲染第一步必须是 esc(md)：后续所有替换都跑在已转义的文本上。"""
    fn = re.search(r"^function md2html\(md\)\{[\s\S]*?^\}", WEB_SRC, re.M)
    assert fn, "没找到 md2html"
    first_line = fn.group(0).splitlines()[1]
    assert "esc(md)" in first_line, f"md2html 首行不再是 esc(md)：{first_line!r}"


def test_console_has_no_external_resources():
    """控制台不引外部资源 —— CSP 才能用 default-src 'self'。"""
    assert not re.search(r"(src|href)\s*=\s*[\"']https?://", WEB_SRC), "控制台引入了外部资源"


# --------------------------------------------------------------------------
# 行为层（需要 node）
# --------------------------------------------------------------------------
def _run_in_node(js: str, md: str) -> str:
    """把抽出来的 JS 交给 node 真跑一遍，拿渲染结果字符串。"""
    script = js + "\nconst out = md2html(process.argv[2]);\nprocess.stdout.write(out);\n"
    tmp = Path(tempfile.gettempdir()) / "pm_md2html_case.cjs"
    tmp.write_text(script, encoding="utf-8")
    proc = subprocess.run(
        [shutil.which("node"), str(tmp), md],
        capture_output=True,
        text=True,
        encoding="utf-8",  # node 的 stdout 恒为 UTF-8；不指定时 Windows 中文机会按 GBK 解码，reader 线程直接炸掉
        timeout=30,
        check=True,
    )
    return proc.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="本机没有 node，跳过 JS 行为验证")
@pytest.mark.parametrize("payload", _PAYLOADS)
def test_md2html_output_has_no_raw_tags(payload: str):
    """渲染结果里除白名单结构标签外，不能有任何原始尖括号。"""
    out = _run_in_node(_extract_js(), payload)
    residue = _ALLOWED_TAGS.sub("", out)
    assert "<" not in residue, f"仍有未转义的标签内容：{residue!r}"
    assert ">" not in residue, f"仍有未转义的标签内容：{residue!r}"


@pytest.mark.skipif(shutil.which("node") is None, reason="本机没有 node，跳过 JS 行为验证")
def test_md2html_still_renders_markdown():
    """加固不能把正常渲染弄坏：标题、加粗、行内代码、代码块都要还在。"""
    out = _run_in_node(_extract_js(), "# 标题\n**粗体** 与 `code`\n\n```\nprint(a < b)\n```")
    assert "<h1>标题</h1>" in out
    assert "<b>粗体</b>" in out
    assert "<code>code</code>" in out
    assert "<pre>print(a &lt; b)" in out


# --------------------------------------------------------------------------
# 服务层安全头
# --------------------------------------------------------------------------
def test_console_page_has_security_headers():
    client = TestClient(server_mod.app)
    resp = client.get("/")
    assert resp.status_code == 200
    csp = resp.headers.get("Content-Security-Policy", "")
    assert "default-src 'self'" in csp, f"控制台缺少 CSP：{dict(resp.headers)}"
    assert "frame-ancestors 'none'" in csp
    assert resp.headers.get("X-Content-Type-Options") == "nosniff"
    assert resp.headers.get("X-Frame-Options") == "DENY"
    assert resp.headers.get("Referrer-Policy") == "no-referrer"


def _nonce_from_csp(csp: str) -> str:
    m = re.search(r"'nonce-([^']+)'", csp)
    assert m, f"CSP 里没有 nonce：{csp}"
    return m.group(1)


def test_csp_uses_nonce_and_not_unsafe_inline():
    """CSP 不能退化成 'unsafe-inline' —— 那等于只防外联、不防注入的内联脚本。"""
    client = TestClient(server_mod.app)
    resp = client.get("/")
    csp = resp.headers.get("Content-Security-Policy", "")
    assert "'unsafe-inline'" not in csp, f"CSP 退回了 unsafe-inline：{csp}"
    nonce = _nonce_from_csp(csp)
    assert f"'nonce-{nonce}'" in csp


def test_console_html_nonce_matches_csp_header():
    """响应头里的 nonce 必须和 HTML 里 script/style 标签上的 nonce 一致。

    不一致的表现是控制台整页失去交互（自己的脚本被自家 CSP 拦掉），
    而 HTTP 状态码还是 200 —— 只看状态码的测试会漏掉这种故障。
    """
    client = TestClient(server_mod.app)
    resp = client.get("/")
    nonce = _nonce_from_csp(resp.headers.get("Content-Security-Policy", ""))
    html = resp.text
    assert server_mod.CSP_NONCE_PLACEHOLDER not in html, "占位符没被替换掉"
    assert f'<script nonce="{nonce}">' in html, "script 标签没带上本请求的 nonce"
    assert f'<style nonce="{nonce}">' in html, "style 标签没带上本请求的 nonce"
    # 每个内联 script/style 标签都必须带 nonce，漏一个就会被拦
    assert html.count("<script") == html.count("<script nonce=")
    assert html.count("<style") == html.count("<style nonce=")


def test_nonce_is_unique_per_request():
    client = TestClient(server_mod.app)
    n1 = _nonce_from_csp(client.get("/").headers.get("Content-Security-Policy", ""))
    n2 = _nonce_from_csp(client.get("/").headers.get("Content-Security-Policy", ""))
    assert n1 and n2 and n1 != n2, "nonce 复用了：重放注入就成立"


def test_no_inline_event_handlers_or_style_attrs():
    """CSP 收紧后，控制台自身不能再用内联事件属性与 style 属性。

    这两类写法在 nonce CSP 下会被浏览器直接拦掉（属性级内联不受 nonce 保护），
    表现为按钮点了没反应、局部样式丢失 —— 静默的功能退化，必须有测试盯着。
    """
    offenders = [
        (i + 1, line.strip()[:80])
        for i, line in enumerate(WEB_SRC.splitlines())
        if re.search(r'\son[a-z]+\s*=\s*"', line) and not line.strip().startswith("//")
    ]
    assert not offenders, f"仍有内联事件属性：{offenders}"
    style_attrs = [
        (i + 1, line.strip()[:80])
        for i, line in enumerate(WEB_SRC.splitlines())
        if 'style="' in line and not line.strip().startswith("//")
    ]
    assert not style_attrs, f"仍有内联 style 属性：{style_attrs}"


def test_docs_exempt_from_csp_but_keeps_other_headers():
    """/docs 走 CDN，套 CSP 会白屏；但防嵌套/防嗅探头照给。"""
    client = TestClient(server_mod.app)
    resp = client.get("/docs")
    assert resp.status_code == 200
    assert "Content-Security-Policy" not in resp.headers
    assert resp.headers.get("X-Frame-Options") == "DENY"
    assert resp.headers.get("X-Content-Type-Options") == "nosniff"


def test_console_html_is_served_as_declared():
    """WEB_CONSOLE_HTML 必须真的是控制台文档（防常量被误改后测试空转）。"""
    assert "<!DOCTYPE html>" in web_mod.WEB_CONSOLE_HTML
    assert "md2html" in web_mod.WEB_CONSOLE_HTML
