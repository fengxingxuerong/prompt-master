"""ModelHub 网关的**运维面路由**回归（/v1/keys、/v1/agents、/v1/usage、/v1/metrics、
/v1/ledger*、/v1/pool/status、/v1/roles、/ 与 /console）—— 进程内 TestClient，零出网。

为什么单独一个文件：这些路由合计 194 条语句一直是 0 覆盖（`pm/modelhub/server.py` 整体
49% 的缺口几乎全在这里），而它们是看板、`/console` 和运维脚本唯一的数据来源。
网关的"能不能对话"早就有测试（`test_modelhub_stream_contract.py`），
"运维看得见什么"一条都没有 —— 于是第一轮就抓到一条真缺陷：`POST /v1/agents`
绑定不存在的角色时返回 **500**，而它自己的注释写着"不存在 → 422"
（`get_registry().get_role()` 抛的是带"可用角色清单"的 ConfigError，被路由原样丢给
Starlette 变成 Internal Server Error —— 最该说话的那句话被吞掉了）。

台账/密钥库/注册表都是**进程内单例 + 路径来自 env**，所以每条用例都先把三个单例清空、
再把 env 指到自己的 tmp 上；否则前一条用例发行的密钥会出现在后一条的清单里
（那种红看起来像"路由写错了"，其实是夹具串味）。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pm.modelhub import agents as AG
from pm.modelhub import ledger as LG
from pm.modelhub import server as S
from pm.modelhub import vkeys as VK

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def gateway(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """一个干净的网关：密钥库/注册表/台账/角色文件全部落在 tmp_path。"""
    store = tmp_path / "mh"
    store.mkdir()
    roles = tmp_path / "roles.json"
    roles.write_text(
        '{"roles": {"writer": {"system_prompt": "写"}, "judge": {"system_prompt": "评"}}}',
        encoding="utf-8",
    )
    monkeypatch.setenv("PMH_DATA_DIR", str(store))
    monkeypatch.setenv("PMH_LEDGER_PATH", str(store / "ledger.jsonl"))
    monkeypatch.setenv("PMH_ROLES", str(roles))
    monkeypatch.setenv("PMH_CONFIG", str(ROOT / "config" / "modelhub.json"))
    # config/modelhub.json 通过 ${PM_API_KEY_*} 占位符引用 .env，而 CI 是干净检出、
    # 没有 .env —— 池初始化解析占位符会直接 ConfigError（真发生过：CI 首跑 4 个 job
    # 全红在这里，本地却全绿，因为本机 .env 把它掩盖了）。这些路由（/v1/metrics、
    # /v1/usage）只读池状态与台账，零真实出网，假值即可满足"变量非空"。
    for _var in (
        "PM_API_KEY_1",
        "PM_API_KEY_AMD",
        "PM_API_KEY_NVIDIA",
        "PM_API_KEY_OPENROUTER",
        "PM_API_KEY_STEP",
    ):
        monkeypatch.setenv(_var, "test-key-not-real")
    monkeypatch.delenv("PMH_GATEWAY_TOKEN", raising=False)
    monkeypatch.setattr(VK, "_VK", None)
    monkeypatch.setattr(VK, "_AG", None)
    monkeypatch.setattr(AG, "_REGISTRY", None)
    monkeypatch.setattr(S, "_hub", None)
    return TestClient(S.app)


def _admin(token: str | None = None) -> dict[str, str]:
    return {"X-API-Key": token} if token else {}


# ---------------------------------------------------------------- 鉴权矩阵


def test_admin_endpoints_require_the_token_when_it_is_configured(
    gateway: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """设了 PMH_GATEWAY_TOKEN 之后，"没带 / 带错"都必须 401，不能退化成开放模式。"""
    monkeypatch.setenv("PMH_GATEWAY_TOKEN", "sekret")
    for path in ("/v1/keys", "/v1/ledger", "/v1/ledger/stats", "/v1/pool/status", "/v1/roles"):
        assert gateway.get(path).status_code == 401, path
        assert gateway.get(path, headers=_admin("wrong")).status_code == 401, (
            f"{path} 对错误口令放行"
        )
    assert gateway.get("/v1/keys", headers=_admin("sekret")).status_code == 200


def test_bearer_and_x_api_key_are_the_same_credential(
    gateway: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """两种写法都要认：OpenAI 客户端只会带 Authorization，运维脚本习惯带 X-API-Key。"""
    monkeypatch.setenv("PMH_GATEWAY_TOKEN", "sekret")
    via_bearer = gateway.get("/v1/keys", headers={"Authorization": "Bearer sekret"})
    via_header = gateway.get("/v1/keys", headers={"X-API-Key": "sekret"})
    assert via_bearer.status_code == via_header.status_code == 200
    # 大小写前缀也要认（客户端会发 "bearer"）
    assert gateway.get("/v1/keys", headers={"Authorization": "bearer sekret"}).status_code == 200, (
        "Authorization 前缀按大小写敏感 → OpenAI SDK 之外的客户端会被挡"
    )


def test_virtual_key_cannot_reach_admin_endpoints(
    gateway: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """vk- 密钥能对话、能看用量，但不能发行/删除密钥 —— 否则一把发给智能体的钥匙等于管理员。"""
    monkeypatch.setenv("PMH_GATEWAY_TOKEN", "sekret")
    vk = gateway.post("/v1/keys", json={"agent": "bot-a"}, headers=_admin("sekret")).json()["key"][
        "key"
    ]
    assert gateway.get("/v1/usage", headers={"X-API-Key": vk}).status_code == 200
    assert gateway.get("/v1/keys", headers={"X-API-Key": vk}).status_code == 401


def test_revoked_virtual_key_stops_working_everywhere(
    gateway: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PATCH enabled=false 之后，"200 但还能用"是最坏的结果 —— 撤销必须立刻生效。"""
    monkeypatch.setenv("PMH_GATEWAY_TOKEN", "sekret")
    issued = gateway.post("/v1/keys", json={"agent": "bot-a"}, headers=_admin("sekret")).json()[
        "key"
    ]
    key = issued["key"]
    assert gateway.get("/v1/metrics", headers={"X-API-Key": "sekret"}).status_code == 200
    assert gateway.get("/v1/usage", headers={"X-API-Key": key}).status_code == 200
    gateway.patch(f"/v1/keys/{key}", json={"enabled": False}, headers=_admin("sekret"))
    assert gateway.get("/v1/usage", headers={"X-API-Key": key}).status_code == 401


# ---------------------------------------------------------------- 密钥 CRUD


def test_key_lifecycle_is_visible_and_idempotent_delete_is_404(gateway: TestClient) -> None:
    """发行 → 清单里能看到 → 删除 → 再删必须 404（而不是静默成功）。"""
    issued = gateway.post("/v1/keys", json={"agent": "bot-a", "note": "ci"}).json()["key"]
    assert issued["agent"] == "bot-a" and issued["enabled"] is True
    listed = gateway.get("/v1/keys").json()["keys"]
    assert [k["key"] for k in listed] == [issued["key"]]
    assert gateway.delete(f"/v1/keys/{issued['key']}").json()["ok"] is True
    assert gateway.get("/v1/keys").json()["keys"] == []
    assert gateway.delete(f"/v1/keys/{issued['key']}").status_code == 404


def test_key_issue_rejects_blank_agent_with_422_not_500(gateway: TestClient) -> None:
    """绑定不到 agent 的密钥没有意义（台账按 agent 归账）；报 422 说清楚，别抛 500。"""
    r = gateway.post("/v1/keys", json={"agent": "   "})
    assert r.status_code == 422
    assert "agent" in r.json()["detail"]


def test_key_patch_and_delete_of_unknown_key_are_404(gateway: TestClient) -> None:
    assert gateway.patch("/v1/keys/vk-nope", json={"enabled": False}).status_code == 404
    assert gateway.delete("/v1/keys/vk-nope").status_code == 404


# --------------------------------------------------------------- 智能体 CRUD


def test_agent_lifecycle_through_the_api(gateway: TestClient) -> None:
    gateway.post("/v1/agents", json={"name": "bot-a", "framework": "native-http"})
    body = gateway.get("/v1/agents").json()
    assert body["persisted"] is True
    assert [a["name"] for a in body["agents"]] == ["bot-a"]
    assert gateway.delete("/v1/agents/bot-a").json()["removed"] == "bot-a"
    assert gateway.delete("/v1/agents/bot-a").status_code == 404


def test_agent_with_unknown_role_gets_422_listing_the_real_roles(
    gateway: TestClient,
) -> None:
    """本轮抓到的缺陷：路由里的注释写着"不存在 → 422"，实测返回 **500 Internal Server Error**。

    注册表抛的 ConfigError 自带"可用角色：writer, judge"这句运维最需要的话，
    但它穿过 Starlette 就只剩 "Internal Server Error" —— 症状是"网关坏了"，
    而真相是"你写错了角色名"。这条用例存在的意义就是不让它再退回去。
    """
    r = gateway.post("/v1/agents", json={"name": "bot-b", "default_role": "writre"})
    assert r.status_code == 422, f"实际 {r.status_code}: {r.text[:120]}"
    detail = r.json()["detail"]
    assert "writre" in detail and "writer" in detail and "judge" in detail


def test_agent_registration_is_idempotent_and_replaces_fields(gateway: TestClient) -> None:
    gateway.post("/v1/agents", json={"name": "bot-c", "framework": "langchain"})
    again = gateway.post(
        "/v1/agents", json={"name": "bot-c", "framework": "dify", "default_role": "judge"}
    ).json()["agent"]
    assert again["framework"] == "dify" and again["default_role"] == "judge"
    assert len(gateway.get("/v1/agents").json()["agents"]) == 1


# ------------------------------------------------------------ 台账与聚合


def _seed_ledger() -> None:
    """两条 call（一成一败，分属两个 agent/模型）+ 一条 switch。"""
    LG.append_call_event(
        request_id="r1",
        model="m-a",
        endpoint="http://127.0.0.1:9/v1",
        success=True,
        latency_ms=100,
        attempts=2,
        failovers=1,
        http_status=200,
        error_type=None,
        error_msg=None,
        agent="bot-a",
        role="evaluator",
        content_chars=42,
    )
    LG.append_call_event(
        request_id="r2",
        model="m-b",
        endpoint="http://127.0.0.1:9/v1",
        success=False,
        latency_ms=50,
        attempts=1,
        failovers=0,
        http_status=500,
        error_type="GatewayError",
        error_msg="boom",
        agent="bot-b",
        role="target",
        content_chars=None,
    )
    LG.append_switch_event(
        request_id="r2",
        from_model="m-a",
        to_model="m-b",
        reason="circuit_open",
        detail=None,
        failover_index=1,
    )


def test_ledger_query_filters_ordering_and_limit_clamp(gateway: TestClient) -> None:
    _seed_ledger()
    all_rows = gateway.get("/v1/ledger").json()
    assert all_rows["count"] == 3
    assert all_rows["rows"][0]["type"] == "switch", "必须按时间倒序（最新在前）"
    assert (
        gateway.get("/v1/ledger", params={"type": "call", "success": "true"}).json()["count"] == 1
    )
    assert gateway.get("/v1/ledger", params={"success": "0"}).json()["count"] == 1
    assert gateway.get("/v1/ledger", params={"model": "m-a"}).json()["count"] == 1
    assert gateway.get("/v1/ledger", params={"request_id": "r2"}).json()["count"] == 2
    # limit 越界要夹住，不能拿 limit=0 把所有行吐出来，也不能让 99999 变成 DoS
    assert gateway.get("/v1/ledger", params={"limit": 0}).json()["count"] == 1
    assert len(gateway.get("/v1/ledger", params={"limit": 2}).json()["rows"]) == 2
    assert gateway.get("/v1/ledger", params={"limit": 99999}).json()["count"] == 3


def test_ledger_stats_counts_switches_and_groups_by_model(gateway: TestClient) -> None:
    _seed_ledger()
    stats = gateway.get("/v1/ledger/stats").json()
    assert (stats["calls"], stats["success"], stats["failed"], stats["switches"]) == (
        2,
        1,
        1,
        1,
    )
    assert stats["by_model"]["m-a"] == {"calls": 1, "success": 1}
    assert stats["exists"] is True


def test_usage_groups_by_agent_and_model_and_honours_the_agent_filter(
    gateway: TestClient,
) -> None:
    _seed_ledger()
    body = gateway.get("/v1/usage").json()
    assert body["totals"]["calls"] == 2 and body["totals"]["success"] == 1
    assert body["totals"]["success_rate"] == 0.5
    agg = body["by_agent"]
    assert agg["bot-a"]["m-a"]["latency_ms_sum"] == 100
    assert agg["bot-a"]["m-a"]["failovers_sum"] == 1
    assert agg["bot-a"]["m-a"]["content_chars_sum"] == 42
    assert agg["bot-b"]["m-b"]["success"] == 0
    day = next(iter(agg["bot-a"]["m-a"]["days"]))
    assert agg["bot-a"]["m-a"]["days"][day] == 1
    only_b = gateway.get("/v1/usage", params={"agent": "bot-b"}).json()
    assert only_b["totals"]["calls"] == 1 and list(only_b["by_agent"]) == ["bot-b"]
    assert "persistent_daily" in body, "日聚合是断档复盘用的，不能只给窗口内数据"


class _FakeHub:
    """只实现路由用到的 `status()`。"""

    def __init__(self, models: list[dict[str, Any]]) -> None:
        self._models = models

    def status(self) -> dict[str, Any]:
        return {"models": self._models, "config_path": "cfg.json", "last_config_error": None}


def test_metrics_reports_success_rate_and_open_circuits(gateway: TestClient, monkeypatch) -> None:
    _seed_ledger()
    monkeypatch.setattr(
        S,
        "_hub_instance",
        lambda: _FakeHub(
            [
                {"name": "m-a", "circuit": "closed", "consecutive_failures": 0, "enabled": True},
                {"name": "m-b", "circuit": "open", "consecutive_failures": 3, "enabled": True},
            ]
        ),
    )
    m = gateway.get("/v1/metrics").json()
    assert (
        m["modelhub_calls_total"],
        m["modelhub_calls_success"],
        m["modelhub_calls_failed"],
    ) == (2, 1, 1)
    assert m["modelhub_success_rate"] == 0.5
    assert m["modelhub_switches_total"] == 1
    assert m["modelhub_pool_models"] == 2
    assert m["modelhub_pool_circuits_open"] == 1
    assert m["modelhub_circuits"]["m-b"]["consecutive_failures"] == 3


def test_metrics_on_an_empty_ledger_says_none_instead_of_crashing(
    gateway: TestClient, monkeypatch
) -> None:
    """0 次调用时成功率没有定义 —— 除零会把这个路由变成 500，而它是监控面板拉的第一个接口。"""
    monkeypatch.setattr(S, "_hub_instance", lambda: _FakeHub([]))
    m = gateway.get("/v1/metrics").json()
    assert m["modelhub_calls_total"] == 0 and m["modelhub_success_rate"] is None


def test_pool_status_passes_the_hub_view_through(gateway: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(
        S,
        "_hub_instance",
        lambda: _FakeHub(
            [{"name": "m-a", "circuit": "closed", "consecutive_failures": 0, "enabled": False}]
        ),
    )
    body = gateway.get("/v1/pool/status").json()
    assert body["config_path"] == "cfg.json"
    assert body["models"][0]["enabled"] is False


# ------------------------------------------------------------------ 角色面


def test_roles_lists_what_the_registry_can_serve(gateway: TestClient) -> None:
    body = gateway.get("/v1/roles").json()
    assert body["roles"] == ["judge", "writer"]


def test_broken_roles_file_is_503_not_500(
    gateway: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """角色文件不可读时要"明确不可用"（503 + 路径），别让运维只看到一个 500 空壳。"""
    monkeypatch.setattr(AG, "_REGISTRY", None)
    monkeypatch.setenv("PMH_ROLES", str(tmp_path / "missing" / "roles.json"))
    r = gateway.get("/v1/roles")
    assert r.status_code == 503, r.status_code
    assert "roles.json" in r.json()["detail"]


# ------------------------------------------------------- 自描述与静态出口


def test_root_advertises_every_route_the_app_actually_serves(gateway: TestClient) -> None:
    """`GET /` 是智能体接入时读的第一份文档：新加路由忘了写进来，就等于没有文档。"""
    body = gateway.get("/").json()
    assert body["version"] == S.GATEWAY_VERSION
    listed = " ".join(body["routes"])
    served = sorted({r.path for r in S.app.routes if getattr(r, "path", "").startswith("/v1/")})
    # 带路径参数的（/v1/keys/{key}）在文档里写成 "/v1/keys/{key}" 形态，单独放过
    missing = [p for p in served if "{" not in p and p not in listed]
    assert not missing, f"这些路由没有出现在 / 的自述里：{missing}"


def test_console_is_served_as_html_and_not_empty(gateway: TestClient) -> None:
    r = gateway.get("/console")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "/v1/ledger" in r.text, "控制台没有引用台账路由 ⇒ 它展示的数据不是这些接口来的"


# ---------------------------------------------------- 控制台脚本的静态守卫
#
# 为什么用静态断言而不是跑一遍浏览器：CI 里没有浏览器，而这几条性质都是"字面上就不该出现"
# 的形态（写死的版本号、行内 onclick 拼数据、阻塞式 alert）。真正的渲染验证是
# 2026-09-25 手工点界面时做的（那次点出了"流式请求 68 秒画面只有一个 …"和
# 台账/密钥字段未转义两件事），静态守卫负责的是**别让它退回**。

CONSOLE_SRC = (ROOT / "pm" / "modelhub" / "console.py").read_text(encoding="utf-8")


# 注释里允许出现被禁的写法（那是在解释"为什么不用它"），所以三条守卫都只看非注释行
CONSOLE_CODE = "\n".join(ln for ln in CONSOLE_SRC.splitlines() if not ln.strip().startswith("//"))


def test_console_does_not_hardcode_its_own_version() -> None:
    """控制台里那个 `v1.1.0` 字面量：网关升到 1.2.0 之后，界面还会说自己是 1.1.0。

    版本单一源是这仓库已经付过学费的地方（`GATEWAY_VERSION` 三处出口读同一常量），
    但字面量藏在 JS 里逃过了那道检查 ⇒ 这里补一句：界面必须从 /api/health 取。
    """
    assert not re.search(r"v\d+\.\d+\.\d+", CONSOLE_CODE), "控制台写死了版本号，改从 /api/health 取"
    assert "/api/health" in CONSOLE_CODE, "版本必须来自 /api/health"


def test_console_does_not_put_row_data_into_inline_handlers() -> None:
    """`onclick="delKey('${k.key}')"`：密钥/agent 名里一个单引号就同时撕开属性和语句。"""
    inline = re.findall(r"<[^>]*\sonclick=", CONSOLE_CODE)
    assert not inline, f"HTML 标签里仍有行内 onclick（拼数据就是注入面）：{inline[:3]}"
    assert "alert(" not in CONSOLE_CODE, "阻塞式 alert 让台账面板读失败时整个页面僵住"


def _inner_html_blocks(src: str) -> list[str]:
    """取出 JS 里所有 `xxx.innerHTML = ...;` 的赋值块。

    守卫只该管这些块：写到 `textContent` 的外部文本不需要转义（浏览器本来就不解析它），
    在那儿套 esc 反而会把 `&lt;` 显给运维看。
    """
    blocks: list[str] = []
    cur: list[str] = []
    active = False
    for ln in src.splitlines():
        if ".innerHTML =" in ln:
            active = True
        if active:
            cur.append(ln)
            if ln.rstrip().endswith(";"):
                blocks.append("\n".join(cur))
                cur, active = [], False
    return blocks


def test_console_escapes_external_text_before_inner_html() -> None:
    """台账/池/密钥三张表都拼 innerHTML，而字段来自外部（上游错误文本、模型名、agent 名）。

    守卫取"最保守的一条"：innerHTML 块里出现裸字段插值（没经过 esc）就红。
    这不是完备证明（完备需要浏览器），但足以挡住"新加一列忘了转义"。
    """
    assert "function esc(" in CONSOLE_CODE, "转义 helper 被删了"
    blocks = _inner_html_blocks(CONSOLE_CODE)
    assert len(blocks) >= 3, f"只找到 {len(blocks)} 个 innerHTML 块 ⇒ 守卫的作用面已经变了"
    raw = [
        m.group(0)
        for block in blocks
        for m in re.finditer(r"\$\{[^}]+\}", block)
        if re.search(r"\$\{\s*[a-z]\.[a-z_]", m.group(0))
        and "esc(" not in m.group(0)
        and "?" not in m.group(0)  # 三元式产出的是固定 class 名，不是外部数据
    ]
    assert not raw, f"这些 innerHTML 插值没走 esc()：{raw[:6]}"


def test_console_has_no_dead_controls() -> None:
    """HTML 里每个有 id 的交互控件都必须在脚本里被接上。

    实测抓到的是一条**按了没反应的"刷新"按钮**（`#lLoad` 出现在 HTML 里，脚本一次都没引用它），
    以及两个"改了筛选、表还是上一次结果"的下拉（`#lType`/`#lSucc` 只被读值、没有 change 处理）。
    前者让运维以为网关卡了，后者更坏：让人拿不匹配的数据下结论。
    """
    html, js = CONSOLE_SRC.split("<script>", 1)
    controls = re.findall(r'<(button|select|input|textarea)[^>]*\bid="([^"]+)"', html)
    assert controls, "控制台里没有控件？解析口径变了，这条守卫要看一眼怎么写"
    dead = [i for _tag, i in controls if f"#{i}" not in js]
    assert not dead, f"这些控件没有任何接线：{dead}"
    # 逐个点名断言（原来写的是 `".onchange" in js`，删掉其中一个下拉的处理照样绿 ——
    # 是摘掉 `$("#lType").onchange` 那次变异把它照出来的）
    assert '$("#lLoad").onclick = loadLedger' in js, "台账刷新按钮没接 loadLedger"
    for sel in ("lType", "lSucc"):
        assert f'$("#{sel}").onchange = loadLedger' in js, f"#{sel} 改了筛选却不重新取数"


def test_console_shows_elapsed_time_while_waiting_for_first_byte() -> None:
    """屏幕上必须一直有"在等什么、等了多久、开始收了没有"。

    两个理由，第二个是我这轮自己踩的：①上游首字节会在秒级到几十秒级波动，没有反馈时
    运维的下一步通常是刷新或重启网关 —— 把其实还在跑的请求一起丢掉；②**客户端计时不可信**：
    同一次调用在隐藏标签页里 `performance.now()` 报 68767ms，而网关台账记的是 3160ms
    （后台标签页节流）。所以我一度把"68 秒"当成上游慢的证据报了出去 —— 那是浏览器给的数，
    不是网关给的。把"首字节"单独显示出来，就是让这两种时间不再混成一个数字。
    """
    assert "还没收到首个字节" in CONSOLE_CODE
    assert "setInterval(" in CONSOLE_CODE and "clearInterval(tick)" in CONSOLE_CODE, (
        "计时器没有收尾"
    )


def test_health_endpoint_is_open_and_reports_ok(gateway: TestClient) -> None:
    r = gateway.get("/api/health")
    assert r.status_code == 200
    assert r.json().get("status") in ("ok", "healthy"), r.json()
