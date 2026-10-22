"""插件面板（`pm/panel.py` + `pm/web/_panel.py` + `run.py panel`）的回归。

形状照 `tests/test_web_console.py` 来，因为那套断言管的是**同一类故障**：单页 HTML 的脚本
是整段编译的，任一处语法错会让浏览器丢掉整个 `<script>` —— 表现是"页面在、按钮全无反应"，
而只看状态码的 Python 断言照样全绿。

这个文件独有的三条承重断言：
1. **判据不在面板里**：三个 POST 端点的返回必须与当场跑 `run.py` 的返回相等
   （差分，不是"看起来对"）。面板只能改渲染，不能改判定。
2. **转发底层的加固项与 MCP 逐条相等**：两份 subprocess 转发会漂，漂了就有半个产品
   把任务文本当命令执行。同一次调用量两边。
3. **零调用是真的**：把 Key 全摘掉，①②③ 三张卡对应的端点必须照样给出判定。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import types
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pm import cliforward as fwd
from pm import mcp_server
from pm import panel as panel_mod
from pm import server as server_mod
from pm.cli import panelcmd
from pm.web._panel import PANEL_HTML

ROOT = Path(__file__).resolve().parents[1]

DEFECTY = "你是分析师。所有结论位置一律标注数据缺失。<输入>abc"
CLEAN = (
    "你是销售数据分析师。输入为 CSV 文本。\n\n[约束]\n"
    "1. 只输出一个 JSON 对象，含 ranking、yoy、anomalies 三个键。\n"
    '2. 输入为空字符串时输出 {"error":"empty_input"}。\n'
    "3. 个别字段缺失时该字段写 null，其余字段照常输出。\n"
    "4. 不得输出分析过程文字，只输出 JSON。\n"
    "5. 结论必须引用输入里的具体数值。\n"
    "6. 同比缺失时写 null 并在 notes 说明缺哪个月的基期。\n"
    "7. 排名按本期金额降序。\n"
)

PANEL_PATHS = {
    "/panel",
    "/api/panel/capabilities",
    "/api/panel/templates",
    "/api/panel/check",
    "/api/panel/cases-check",
    "/api/panel/diff",
}


@pytest.fixture()
def client() -> TestClient:
    return TestClient(server_mod.app)


@pytest.fixture(autouse=True)
def _reset_probe_cache():
    """连接自检探针是进程内缓存的 —— 不重置会让上一条用例的读数串进下一条。"""
    before = dict(panel_mod._probe_cache)
    panel_mod._probe_cache.update({"at": 0.0, "ran": False, "ok": False, "note": ""})
    yield
    panel_mod._probe_cache.clear()
    panel_mod._probe_cache.update(before)


def _cli(args: list[str]) -> dict:
    """当场跑一次真 CLI，作为"面板判据 == CLI 判据"的对照边。"""
    proc = subprocess.run(
        [sys.executable, str(ROOT / "run.py"), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        cwd=str(ROOT),
        stdin=subprocess.DEVNULL,
        env=os_environ_utf8(),
    )
    assert proc.returncode in (0, 1, 2, 3), proc.stderr[-300:]
    return json.loads(proc.stdout)


def os_environ_utf8() -> dict[str, str]:
    """子进程必须显式 UTF-8：这台机器的默认码页是 cp936，中文输出会打崩管道读取。"""
    return {**os.environ, "PYTHONIOENCODING": "utf-8"}


# ---------------------------------------------------------------------------
# A. 单页 HTML 的安全层（与富控制台同一套约束）
# ---------------------------------------------------------------------------
def test_panel_html_is_the_declared_single_page() -> None:
    assert "<!DOCTYPE html>" in PANEL_HTML
    assert "runCheck" in PANEL_HTML


def test_panel_has_no_external_resources() -> None:
    """零外部资源是 CSP 能用 default-src 'self' 的前提（与 test_web_console 同判据）。"""
    assert not re.search(r"(src|href)\s*=\s*[\"']https?://", PANEL_HTML), "面板引入了外部资源"


def test_panel_has_no_inline_handlers_or_style_attrs() -> None:
    """nonce CSP 不保护属性级内联：出现 onclick / style="…" 就是"按钮点了没反应"的静默退化。"""
    handlers = [
        (i + 1, line.strip()[:70])
        for i, line in enumerate(PANEL_HTML.splitlines())
        if re.search(r"\son[a-z]+\s*=\s*\"", line) and not line.strip().startswith("//")
    ]
    assert not handlers, f"仍有内联事件属性：{handlers}"
    style_attrs = [
        (i + 1, line.strip()[:70])
        for i, line in enumerate(PANEL_HTML.splitlines())
        if 'style="' in line and not line.strip().startswith("//")
    ]
    assert not style_attrs, f"仍有内联 style 属性：{style_attrs}"


def _panel_script_body() -> str:
    m = re.search(r'<script nonce="[^"]*">\n([\s\S]*?)\n</script>', PANEL_HTML)
    assert m, "没找到面板的 <script> 段（pm/web/_panel.py 的组装结构变了）"
    body = m.group(1)
    # 锚点漂了只截到一小截时，语法检查等于没做 —— 与富控制台同一条防呆
    assert len(body.splitlines()) > 150, f"<script> 段只截到 {len(body.splitlines())} 行"
    return body


@pytest.mark.skipif(shutil.which("node") is None, reason="本机没有 node，跳过整页 JS 语法验证")
def test_panel_script_parses() -> None:
    tmp = Path(tempfile.gettempdir()) / "pm_panel_script_parse.cjs"
    tmp.write_text(_panel_script_body(), encoding="utf-8")
    proc = subprocess.run(
        [shutil.which("node"), "--check", str(tmp)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    assert proc.returncode == 0, f"面板整页 JS 语法错误：\n{proc.stderr[:600]}"


def test_panel_page_nonce_matches_csp(client) -> None:
    resp = client.get("/panel")
    assert resp.status_code == 200
    csp = resp.headers.get("Content-Security-Policy", "")
    m = re.search(r"'nonce-([^']+)'", csp)
    assert m, f"CSP 里没有 nonce：{csp}"
    nonce = m.group(1)
    html = resp.text
    assert server_mod.CSP_NONCE_PLACEHOLDER not in html, "占位符没被替换掉"
    assert f'<script nonce="{nonce}">' in html
    assert f'<style nonce="{nonce}">' in html
    assert html.count("<script") == html.count("<script nonce=")
    assert html.count("<style") == html.count("<style nonce=")
    assert client.get("/panel").headers["Content-Security-Policy"] != csp, (
        "nonce 复用了：重放注入就成立"
    )


def test_panel_page_has_security_headers(client) -> None:
    resp = client.get("/panel")
    csp = resp.headers.get("Content-Security-Policy", "")
    assert "default-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert "'unsafe-inline'" not in csp
    assert resp.headers.get("X-Content-Type-Options") == "nosniff"
    assert resp.headers.get("X-Frame-Options") == "DENY"
    assert resp.headers.get("Referrer-Policy") == "no-referrer"


@pytest.mark.skipif(shutil.which("node") is None, reason="本机没有 node，跳过 JS 行为验证")
def test_bucket_bars_emits_balanced_divs() -> None:
    """`bucketBars` 的 `<div>` 必须配平。

    真事故形状（2026-10-14 浏览器里点出来的）：legend 那个 div 忘了闭合，浏览器把后面的
    `.kv` 与 findings 全吸进 legend 里 —— 结构错了但页面照样"能看"，所以状态码 200、
    语法检查也过，只有 DOM 读数会暴露（`.legend span` 一下多出 6 项）。
    """
    js = _panel_script_body()
    m = re.search(r"^function bucketBars\(d\)\{[\s\S]*?^\}", js, re.M)
    assert m, "没找到 bucketBars（渲染函数改名了，这条守卫要同步）"
    # bucketBars 用到同文件里的 esc()/n()，单独抽出来跑会 ReferenceError（第一次就栽在这）
    helpers = []
    for pat in (r"^function esc\(s\)\{[\s\S]*?^\}", r"^function n\(x\)\{.*\}$"):
        h = re.search(pat, js, re.M)
        assert h, f"面板脚本里没找到 {pat}，这条守卫的取样要同步更新"
        helpers.append(h.group(0))
    tmp = Path(tempfile.gettempdir()) / "pm_panel_buckets.cjs"
    tmp.write_text(
        "\n".join(helpers)
        + "\n"
        + m.group(0)
        + "\nconst out = bucketBars({cases:4, veto_capable:2,"
        " veto_strong:1, echo_only:1, judge_only:1, no_assertion:1});\nprocess.stdout.write(out);\n",
        encoding="utf-8",
    )
    proc = subprocess.run(
        [shutil.which("node"), str(tmp)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=True,
    )
    html = proc.stdout
    assert html.count("<div") == html.count("</div>"), f"div 不配平：{html}"
    assert html.count("<span") == html.count("</span>"), f"span 不配平：{html}"
    assert 'class="bar"' in html and 'class="legend"' in html


def tempfile_gettempdir_alias() -> str:
    return tempfile.gettempdir()


def test_panel_routes_are_mounted_on_the_app() -> None:
    """摘掉 `app.include_router(panel_router)` 必须红 —— 否则面板模块在但路由不在。"""
    mounted = {getattr(r, "path", "") for r in server_mod.app.routes}
    missing = PANEL_PATHS - mounted
    assert not missing, f"面板路由没挂到 app 上：{sorted(missing)}"


# ---------------------------------------------------------------------------
# B. 连接自检与模板列举
# ---------------------------------------------------------------------------
def test_capabilities_reports_booleans_without_leaking_the_token(client, monkeypatch) -> None:
    monkeypatch.setenv("PM_API_TOKEN", "sekret-panel-token")
    monkeypatch.setenv("PM_API_KEY", "sekret-api-key")
    body = client.get("/api/panel/capabilities").json()
    assert set(body) == {
        "service",
        "version",
        "cli_ok",
        "token_required",
        "key_ready",
        "mode",
        "note",
    }, f"连接自检的字段变了：{sorted(body)}"
    assert body["token_required"] is True and body["key_ready"] is True
    assert "sekret" not in json.dumps(body, ensure_ascii=False), "把密钥或 token 回显给了前端"
    # 没 Key 时必须报成 none —— ④那张卡靠这个决定置灰
    for name in ("PM_API_KEY", "PM_API_KEYS", "PM_FAKE_BACKEND"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PM_API_KEY", "")
    monkeypatch.setenv("PM_API_KEYS", "")
    assert client.get("/api/panel/capabilities").json()["mode"] == "none"


def test_capabilities_probe_really_runs_the_cli_and_is_cached(client, monkeypatch) -> None:
    """ "注册成功"≠"在干活"：探针必须真跑一次 CLI，但又不能每次开面板都跑。"""
    calls: list[list[str]] = []

    def fake(args, timeout: float = 60.0) -> dict:
        calls.append(list(args))
        return {"mode": "prompt_check", "ok": True}

    monkeypatch.setattr(panel_mod, "run_cli_json", fake)
    assert client.get("/api/panel/capabilities").json()["cli_ok"] is True
    assert client.get("/api/panel/capabilities").json()["cli_ok"] is True
    assert len(calls) == 1, f"探针没走缓存（跑了 {len(calls)} 次）"
    assert calls[0][:2] == ["check", "--prompt"]


def test_capabilities_reports_a_broken_forwarder_instead_of_hiding_it(client, monkeypatch) -> None:
    def boom(args, timeout: float = 60.0) -> dict:
        raise fwd.ForwardError("run.py 不在")

    monkeypatch.setattr(panel_mod, "run_cli_json", boom)
    body = client.get("/api/panel/capabilities").json()
    assert body["cli_ok"] is False and "run.py 不在" in body["note"]


def test_templates_listing_matches_the_dir(client) -> None:
    on_disk = sorted(p.name for p in (ROOT / "case_templates").glob("*.json"))
    body = client.get("/api/panel/templates").json()
    assert [t["name"] for t in body["templates"]] == on_disk
    assert on_disk, "case_templates/ 空了，面板第二张卡就没有默认对象"
    assert all(t["cases"] > 0 for t in body["templates"])
    assert body["max_cases"] == panel_mod.MAX_SEED_CASES


@pytest.mark.parametrize(
    "name", ["../../.env", "..\\..\\.env", "nope.json", "sales_analysis.json/"]
)
def test_template_names_are_whitelisted_not_guessed(client, name: str) -> None:
    """贴一个 `template=../../.env` 就能把仓库外的文件读出来回显 —— 只认可列举到的名字。"""
    r = client.post("/api/panel/cases-check", json={"template": name})
    assert r.status_code == 404, (r.status_code, r.text[:200])


# ---------------------------------------------------------------------------
# C. 判据同源：面板返回必须等于当场跑 CLI
# ---------------------------------------------------------------------------
def test_check_endpoint_equals_the_cli_verbatim(client) -> None:
    via_cli = _cli(["check", "--prompt", DEFECTY, "--json"])
    via_panel = client.post("/api/panel/check", json={"prompt": DEFECTY}).json()
    assert via_panel == via_cli, "面板与 CLI 的体检判据分叉了"
    assert via_panel["ok"] is False and via_panel["findings"], "这份样本本该命中规则"


def test_clean_prompt_still_passes_through_the_panel(client) -> None:
    via_panel = client.post("/api/panel/check", json={"prompt": CLEAN}).json()
    assert via_panel["ok"] is True and via_panel["findings"] == []


def test_cases_check_endpoint_equals_the_cli_for_every_template(client) -> None:
    """五份自带模板逐个对：面板不许在 CLI 之上再加自己的判定。"""
    from pm.cliforward import TEMPLATE_DIR

    for p in sorted(TEMPLATE_DIR.glob("*.json")):
        via_cli = _cli(["check", "--cases-file", str(p), "--json"])
        via_panel = client.post("/api/panel/cases-check", json={"template": p.name}).json()
        # source 是"从哪儿读的"，两边传的路径写法必然不同；其余字段必须逐字相等
        for d in (via_cli, via_panel):
            d.pop("source", None)
        assert via_panel == via_cli, f"{p.name}：面板判据与 CLI 不等"
        assert via_panel["cases"] > 0 and via_panel["veto_capable"] <= via_panel["cases"]


def test_cases_check_accepts_pasted_json(client) -> None:
    """粘一份自建考卷也要给同样的四桶分区（面板第二张卡的全部信息量就在这里）。"""
    cases = [
        {"input": "卖了多少", "expected": "100"},  # 字面量不在输入里 ⇒ 强判据
        {"input": "总额 100 元", "expected": "100"},  # expected 就在 input 里 ⇒ 只挡丢内容
        {"input": "x", "expected": ""},  # 空 expected ⇒ 根本不执行
        {"input": "怎么判", "expected": "必须先列缺失", "assert_mode": "rule"},  # 交评委
    ]
    raw = json.dumps(cases, ensure_ascii=False)
    via_cli = _cli(["check", "--cases-file", _write_tmp_cases(raw), "--json"])
    via_panel = client.post("/api/panel/cases-check", json={"cases": raw}).json()
    for d in (via_cli, via_panel):
        d.pop("source", None)
    assert via_panel == via_cli
    assert via_panel["cases"] == 4
    assert via_panel["veto_capable"] == 2, "两条字面断言能否决，另两条不能"
    assert via_panel["veto_strong"] == 1
    assert via_panel["echo_only"] == 1
    assert via_panel["judge_only"] == 1
    assert via_panel["no_assertion"] == 1
    # 四桶必须守恒：分区口径由 pm/casesaudit.py 保证，面板只是渲染它
    assert via_panel["veto_capable"] + via_panel["judge_only"] + via_panel["no_assertion"] == 4


def _write_tmp_cases(raw: str) -> str:
    f = Path(tempfile.gettempdir()) / "pm_panel_cases_tmp.json"
    f.write_text(raw, encoding="utf-8")
    return str(f)


def test_diff_endpoint_flags_newly_introduced_pattern(client) -> None:
    after = CLEAN + "\n所有结论位置一律标注数据缺失。\n"
    d = client.post("/api/panel/diff", json={"before": CLEAN, "after": after}).json()
    assert d["mode"] == "prompt_diff" and d["regressed"] is True
    assert "blanket_missing_branch" in d["rules"]["introduced"]
    assert _cli(["diff", *_two_tmp_files(CLEAN, after), "--json"])["regressed"] is True


def _two_tmp_files(before: str, after: str) -> list[str]:
    d = Path(tempfile.gettempdir())
    a, b = d / "pm_panel_a.md", d / "pm_panel_b.md"
    a.write_text(before, encoding="utf-8")
    b.write_text(after, encoding="utf-8")
    return [str(a), str(b)]


def test_zero_call_paths_work_without_any_key(monkeypatch) -> None:
    """把 Key 摘干净再走一遍：免费体检要是偷偷依赖 Key，这条就会红。"""
    for name in ("PM_API_KEY", "PM_API_KEYS", "PM_API_KEY_1", "PM_FAKE_BACKEND", "PM_API_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PM_API_KEY", "")
    c = TestClient(server_mod.app)
    assert c.post("/api/panel/check", json={"prompt": DEFECTY}).json()["mode"] == "prompt_check"
    assert (
        c.post("/api/panel/diff", json={"before": CLEAN, "after": DEFECTY}).json()["mode"]
        == "prompt_diff"
    )
    assert c.get("/api/panel/templates").json()["templates"]


# ---------------------------------------------------------------------------
# D. 门禁与入参校验
# ---------------------------------------------------------------------------
def test_panel_data_routes_honor_pm_api_token(client, monkeypatch) -> None:
    """设了 PM_API_TOKEN 之后，三个 POST 必须被自家门禁挡住；页面与自检仍然可达
    （用户得先看见"要 token"才能把 token 填进去）。"""
    monkeypatch.setenv("PM_API_TOKEN", "panel-gate")
    for path, body in (
        ("/api/panel/check", {"prompt": DEFECTY}),
        ("/api/panel/cases-check", {"template": "support_triage.json"}),
        ("/api/panel/diff", {"before": CLEAN, "after": DEFECTY}),
    ):
        assert client.post(path, json=body).status_code == 401, path
        assert (
            client.post(path, json=body, headers={"X-API-Key": "panel-gate"}).status_code == 200
        ), path
    assert client.get("/panel").status_code == 200
    assert client.get("/api/panel/capabilities").status_code == 200


@pytest.mark.parametrize(
    "path,body",
    [
        ("/api/panel/check", {"prompt": ""}),
        ("/api/panel/cases-check", {"template": "", "cases": "   "}),
        ("/api/panel/diff", {"before": "x", "after": ""}),
        ("/api/panel/check", {"prompt": "超长" * 20000}),
    ],
)
def test_bad_requests_are_rejected_before_any_subprocess(
    client, monkeypatch, path: str, body: dict
) -> None:
    """入参护栏必须在起子进程之前生效 —— 否则一个空 prompt 也能派一发进程。"""
    seen: list[list[str]] = []
    original = panel_mod.run_cli_json

    def spy(args, timeout: float = 60.0):
        seen.append(list(args))
        return original(args, timeout=timeout)

    monkeypatch.setattr(panel_mod, "run_cli_json", spy)
    assert client.post(path, json=body).status_code == 422
    assert seen == [], f"坏入参还是把子进程派出去了：{seen}"


def test_forward_layer_raises_a_neutral_error_not_an_http_one() -> None:
    """底层不认 HTTP：桌面窗口没有"翻成 404"这一层，它要的是同一句人话直接上界面。
    翻成 404 那一半由 test_template_names_are_whitelisted_not_guessed 打真实端点覆盖。"""
    with pytest.raises(fwd.TemplateNotFound):
        fwd.template_path("../../.env")


# ---------------------------------------------------------------------------
# E. 入口与转发底层
# ---------------------------------------------------------------------------
def test_panel_subcommand_help_from_the_real_entry() -> None:
    proc = subprocess.run(
        [sys.executable, str(ROOT / "run.py"), "panel", "--help"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        cwd=str(ROOT),
        stdin=subprocess.DEVNULL,
        env=os_environ_utf8(),
    )
    assert proc.returncode == 0, proc.stderr[-300:]
    assert "run.py panel" in proc.stdout
    assert "--no-browser" in proc.stdout


def test_panel_url_and_host_guard() -> None:
    assert panelcmd._panel_url("0.0.0.0", 9) == "http://127.0.0.1:9/panel"
    assert panelcmd._panel_url("127.0.0.1", 8765).endswith("/panel")
    notes = " ".join(panelcmd._guard_open_host("0.0.0.0", 8765))
    assert "PM_API_TOKEN" in notes, "绑到局域网却不提 token，等于把端点交给同网段"
    assert "PM_API_TOKEN" not in " ".join(panelcmd._guard_open_host("127.0.0.1", 8765))


def test_panel_and_mcp_forwarders_agree_on_hardening(monkeypatch) -> None:
    """两份 subprocess 转发（MCP 与面板）必须逐条同加固项。

    为什么钉这一条：面板与 MCP 是同一条判据的两个出口，漂一点就只有半边
    "任务文本里的 `; rm -rf /` 不会被当命令执行"成立。
    """
    captured: dict[str, dict] = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = list(cmd)
        captured["kw"] = dict(kw)
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout='{"ok": 1}', stderr="")

    monkeypatch.setattr(fwd.subprocess, "run", fake_run)

    assert fwd.run_cli_json(["check", "--prompt", "x"]) == {"ok": 1}
    panel_kwargs = dict(captured["kw"])
    panel_cmd = list(captured["cmd"])

    assert mcp_server._run_cli_json(["check", "--prompt", "x"]) == {"ok": 1}
    mcp_kwargs = dict(captured["kw"])
    mcp_cmd = list(captured["cmd"])

    for name, cmd, kw in (("panel", panel_cmd, panel_kwargs), ("mcp", mcp_cmd, mcp_kwargs)):
        assert cmd[0] == sys.executable, f"{name} 不是用当前解释器起的"
        assert cmd[1].endswith("run.py"), f"{name} 没打到 run.py"
        assert "shell" not in kw, f"{name} 出现了 shell=True（任务文本可被当命令执行）"
        assert kw.get("stdin") is subprocess.DEVNULL, f"{name} 的 stdin 会继承管道并永久阻塞"
        assert kw["env"]["PYTHONIOENCODING"] == "utf-8", f"{name} 在 cp936 机上会把子进程输出打崩"
        assert kw["cwd"] == str(ROOT), f"{name} 的工作目录不是仓库根"

    # 退出码协议：0~3 都是"正常返回"，其余才抛
    for code in (0, 1, 2, 3):
        monkeypatch.setattr(
            fwd.subprocess,
            "run",
            lambda c, _code=code, **k: subprocess.CompletedProcess(
                args=c, returncode=_code, stdout=json.dumps({"code": _code}), stderr=""
            ),
        )
        assert fwd.run_cli_json(["x"])["code"] == code
    monkeypatch.setattr(
        panel_mod.subprocess,
        "run",
        lambda c, **k: subprocess.CompletedProcess(args=c, returncode=99, stdout="", stderr="boom"),
    )
    with pytest.raises(fwd.ForwardError, match="退出码异常"):
        fwd.run_cli_json(["x"])


def test_panel_forwarder_reports_stderr_when_stdout_is_not_json(monkeypatch) -> None:
    """CLI 打印了 traceback 时，面板要给出 stderr 而不是一个裸 JSONDecodeError。"""

    def fake_run(cmd, **kw):
        return subprocess.CompletedProcess(
            args=cmd, returncode=1, stdout="Traceback...", stderr="坏在这里"
        )

    monkeypatch.setattr(fwd.subprocess, "run", fake_run)
    with pytest.raises(fwd.ForwardError, match="没有给出合法 JSON"):
        fwd.run_cli_json(["check", "--prompt", "x"])


# ---------------------------------------------------------------------------
# 入口三件：参数解析、真的把 uvicorn 叫起来、浏览器延后开
# ---------------------------------------------------------------------------
# 这一组是被跳过最多的一形：`_serve` 只有 4 行，但"入口坏了、测的却是被调函数"
# 在本仓已经栽过两次（run.py 的 cp936 入口、modelhub 的流式 return）。


def test_panel_parser_defaults_and_flags() -> None:
    p = panelcmd._build_parser()
    d = p.parse_args([])
    assert (d.host, d.port, d.no_browser) == ("127.0.0.1", panelcmd.DEFAULT_PORT, False)
    got = p.parse_args(["--host", "0.0.0.0", "--port", "9999", "--no-browser"])
    assert (got.host, got.port, got.no_browser) == ("0.0.0.0", 9999, True)


def test_panel_command_actually_hands_uvicorn_the_app(monkeypatch, capsys) -> None:
    """假 uvicorn 接住参数：不占端口也能证明"点了 panel 真的会起 pm.server:app"。"""
    seen: dict[str, object] = {}
    fake = types.ModuleType("uvicorn")
    fake.run = lambda app, **kw: seen.update(app=app, **kw)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "uvicorn", fake)

    assert panelcmd.panel_command(["--port", "8799", "--no-browser"]) == 0
    assert seen["app"] == "pm.server:app", f"面板起了别的应用：{seen}"
    assert (seen["host"], seen["port"]) == ("127.0.0.1", 8799)
    out = capsys.readouterr().out
    assert "http://127.0.0.1:8799/panel" in out
    assert "只监听 127.0.0.1" in out, "没把暴露面说清楚"


def test_panel_serve_reports_a_missing_dependency_as_exit_2(monkeypatch, capsys) -> None:
    """没装 uvicorn 时给人的是一行人话 + 退出码 2，不是一个裸 ImportError 栈。"""
    monkeypatch.setitem(sys.modules, "uvicorn", None)
    assert panelcmd.panel_command(["--no-browser"]) == 2
    assert "uvicorn" in capsys.readouterr().err


def test_panel_clean_shutdown_stays_exit_0(monkeypatch) -> None:
    """uvicorn 正常收工（SystemExit(0)）不能被当成启动失败。"""
    fake = types.ModuleType("uvicorn")

    def _clean(app: object, **kw: object) -> None:
        raise SystemExit(0)

    fake.run = _clean  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "uvicorn", fake)
    assert panelcmd.panel_command(["--port", "8799", "--no-browser"]) == 0


def test_panel_port_conflict_becomes_actionable_exit_2(monkeypatch, capsys) -> None:
    """端口被占用（最常见的"上次那个面板还开着"）要给换端口的建议 + 退出码 2。

    修复前 uvicorn 的 SystemExit(1) 会原样穿出去：用户看到一行 bind 报错，
    退出码是 1（运行失败），而这是配置层面的冲突。
    """
    fake = types.ModuleType("uvicorn")

    def _boom(app: object, **kw: object) -> None:
        raise SystemExit(1)

    fake.run = _boom  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "uvicorn", fake)

    assert panelcmd.panel_command(["--port", "8799", "--no-browser"]) == 2
    err = capsys.readouterr().err
    assert "8799" in err and "--port 8800" in err


def test_panel_opens_the_browser_only_after_a_delay(monkeypatch) -> None:
    """uvicorn 还没监听就 open，第一下必然连不上 —— 所以开浏览器要延后。"""
    opened: list[str] = []
    monkeypatch.setattr(panelcmd.webbrowser, "open", lambda url: opened.append(url) or True)
    t = panelcmd._schedule_open("http://127.0.0.1:1/panel", delay=0.01)
    assert opened == [], "延时应到点才开"
    assert t.daemon is True, "非守护线程会把退出挡住"
    t.join(2.0)
    assert opened == ["http://127.0.0.1:1/panel"]


# ---------------------------------------------------------------------------
# 后端那三条"宿主在我们脚下变了"的分支
# ---------------------------------------------------------------------------
def test_template_that_disappeared_between_list_and_read_is_404(monkeypatch) -> None:
    """白名单是当场列举的；列举与读取之间文件被删掉（TOCTOU）时给一句人话而不是 500。"""
    monkeypatch.setattr(fwd, "template_names", lambda: ["ghost.json"])
    with pytest.raises(fwd.TemplateNotFound) as e:
        fwd.template_path("ghost.json")
    assert "读不到" in str(e.value)


def test_panel_opens_the_browser_unless_told_not_to(monkeypatch, capsys) -> None:
    """不带 --no-browser 时必须安排开浏览器（那一行是唯一的人类入口，缺了它命令像没反应）。"""
    seen: dict[str, object] = {}
    fake = types.ModuleType("uvicorn")
    fake.run = lambda app, **kw: seen.update(app=app, **kw)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "uvicorn", fake)
    scheduled: list[str] = []
    monkeypatch.setattr(
        panelcmd, "_schedule_open", lambda url, delay=0.8: scheduled.append(url) or object()
    )

    assert panelcmd.panel_command(["--port", "8798"]) == 0
    assert scheduled == ["http://127.0.0.1:8798/panel"]
    assert "http://127.0.0.1:8798/panel" in capsys.readouterr().out, (
        "地址要打印出来（无桌面环境时那是唯一入口）"
    )


def test_unusable_template_dir_yields_empty_list_not_a_crash(monkeypatch, tmp_path: Path) -> None:
    """`case_templates/` 变成一个文件或被删掉时，列举退成空表而不是 500。

    注意：这里**没测** "目录 ACL 坏了抛 OSError" 那一形 —— 实测 Python 3.12 的
    `Path.glob` 对不可读目录只返回空迭代（本机那个坏掉的 `.pytest_tmp` 就是这样），
    所以那个 except 够不到，`_template_names` 里也不留它。
    """
    hostile = tmp_path / "not-a-dir"
    hostile.write_text("x", encoding="utf-8")
    monkeypatch.setattr(fwd, "TEMPLATE_DIR", hostile)
    assert fwd.template_names() == []
    assert TestClient(server_mod.app).get("/api/panel/templates").json()["templates"] == []


def test_broken_template_json_is_counted_as_zero_cases(monkeypatch, tmp_path: Path) -> None:
    """坏 JSON 不该把整条列举炸掉：点名 0 条，让人自己去看那份文件。"""
    (tmp_path / "bad.json").write_text("{not json", encoding="utf-8")
    (tmp_path / "good.json").write_text(
        json.dumps([{"input": "a", "expected": "b"}]), encoding="utf-8"
    )
    monkeypatch.setattr(fwd, "TEMPLATE_DIR", tmp_path)
    body = TestClient(server_mod.app).get("/api/panel/templates").json()
    assert {t["name"]: t["cases"] for t in body["templates"]} == {"bad.json": 0, "good.json": 1}


def test_run_cli_text_shares_the_same_hardening(monkeypatch) -> None:
    """`--preflight` 这类只打表格的入口走 run_cli_text —— 它必须走同一个 `_spawn_cli`。

    否则会出现"JSON 那条不许用 shell，文本那条偷偷用了"这种半边加固。
    """
    captured: dict[str, object] = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = list(cmd)
        captured["kw"] = dict(kw)
        return subprocess.CompletedProcess(
            args=cmd, returncode=1, stdout="clarifier | ✅", stderr=""
        )

    monkeypatch.setattr(fwd.subprocess, "run", fake_run)
    out, code = fwd.run_cli_text(["--preflight"])
    assert out == "clarifier | ✅" and code == 1, "退出码要原样递给界面（0 才算全绿）"
    assert "shell" not in captured["kw"]
    assert captured["kw"].get("stdin") is subprocess.DEVNULL
    assert captured["kw"]["env"]["PYTHONIOENCODING"] == "utf-8"


def test_run_cli_json_live_can_actually_be_killed(monkeypatch, tmp_path) -> None:
    """中止不是"调了一下 terminate"就算数：要真把一个 OS 进程掐掉、并当场拿回控制权。

    用一个会挂 30 秒的假 run.py —— 真子进程、真句柄，但不花一分钱。
    """
    import threading
    import time

    slow = tmp_path / "slow_run.py"
    slow.write_text("import time\ntime.sleep(30)\nprint('{}')\n", encoding="utf-8")
    monkeypatch.setattr(fwd, "RUN_PY", slow)

    held: dict[str, object] = {}
    box: dict[str, object] = {}

    def register(proc: object) -> None:
        held["proc"] = proc

    def call() -> None:
        try:
            fwd.run_cli_json_live(["x"], register, timeout=60.0)
        except BaseException as e:  # noqa: BLE001 - 中止后必然抛，正是要看的
            box["exc"] = e

    t = threading.Thread(target=call, daemon=True)
    t.start()
    time.sleep(1.0)
    proc = held.get("proc")
    assert proc is not None, "register 没拿到子进程句柄 —— 中止按钮是死的"
    t0 = time.monotonic()
    proc.terminate()  # type: ignore[attr-defined]
    t.join(15)
    assert not t.is_alive(), "terminate 之后调用还没返回，中止按钮救不了场"
    assert time.monotonic() - t0 < 12, f"掐进程耗时异常：{time.monotonic() - t0:.1f}s"
    assert isinstance(box.get("exc"), fwd.ForwardError), f"中止应报成转发失败，实得 {box}"
    assert held.get("proc") is None, "register(None) 没被调用，UI 会一直以为还在跑"
