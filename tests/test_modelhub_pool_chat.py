"""`ModelHub.chat()` 的切换/重试/记账回归 —— 假上游，进程内，零出网。

为什么单独一个文件：`pool.chat()` 是**每一次真实调用都会走的循环**（候选排序 → 逐个尝试 →
瞬时错误原地重试 → 非瞬时切换 → 断路器记分 → 台账落两 kinds 事件），而它连同
`server.py` 的同一条链一直是 0 覆盖（`pm/modelhub/pool.py` 61% 的缺口几乎全在 402-553）。
网关出事的地方恰恰就在这条链上：`stream=true` 丢 return、错误帧 `str + bytes`、
401 之后该不该重试 —— 三次都是"没有用例所以没人重跑"。

这里钉的是**行为契约**而不是实现细节：
- 谁先被试（priority、指定模型置顶、不在池内要留一条可对账的 switch 事件）；
- 什么算瞬时（原地重试一次，代价是 sleep 1.5s —— 测试直接把 sleep 抓住，不假装它不存在）；
- 什么该切换（400/假成功不重试，立刻走下一个）；
- 全失败时**报的是什么**（尝试顺序 + 最后一个错误，运维拿这一行决定下一步）；
- 台账与响应里的 `request_id` 必须是同一个（`X-Modelhub-Request-Id` 就靠它对账）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pm.modelhub import ledger as LG
from pm.modelhub import pool as P
from pm.modelhub.pool import GatewayError, ModelHub, ModelPoolExhaustedError

MSG = [{"role": "user", "content": "你好"}]


def _tag(url: str) -> str:
    """从 `http://host/<v1-x>/chat/completions` 里取出"哪个模型"的段名。"""
    return url.split("//", 1)[1].split("/", 2)[1]


def _ok(text: str = "回答") -> dict[str, Any]:
    return {
        "choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 5},
    }


class _Upstream:
    """按调用顺序吐出脚本：dict=200 响应，Exception=抛出。"""

    def __init__(self, script: list[Any]) -> None:
        self._script = list(script)
        self.calls: list[dict[str, Any]] = []

    def __call__(self, url: str, api_key: str, payload: dict[str, Any], timeout: float) -> dict:
        self.calls.append({"url": url, "api_key": api_key, "payload": payload})
        if not self._script:
            raise AssertionError("上游被多打了：脚本已用尽（切换链走过头了）")
        out = self._script.pop(0)
        if isinstance(out, BaseException):
            raise out
        return out


def _config(pool: list[dict[str, Any]], tmp_path: Path) -> Path:
    p = tmp_path / "modelhub.json"
    p.write_text(json.dumps({"pool": pool}, ensure_ascii=False), encoding="utf-8")
    return p


@pytest.fixture(autouse=True)
def ledger_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "mh"
    d.mkdir()
    path = d / "ledger.jsonl"
    monkeypatch.setenv("PMH_LEDGER_PATH", str(path))
    monkeypatch.setenv("PMH_DATA_DIR", str(d))
    monkeypatch.setenv("PMH_BREAK_THRESHOLD", "3")
    monkeypatch.setenv("PMH_COOLDOWN_SECONDS", "600")
    return path


def _hub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pool: list[dict[str, Any]]) -> ModelHub:
    specs = [
        {
            "name": s["name"],
            "base_url": f"http://127.0.0.1:9/v1-{s['name']}",
            "api_key": f"sk-{s['name']}",
            "priority": s.get("priority", 1),
            **({"enabled": s["enabled"]} if "enabled" in s else {}),
        }
        for s in pool
    ]
    return ModelHub(config_path=_config(specs, tmp_path))


def _rows(path: Path, type: str | None = None) -> list[dict[str, Any]]:
    return LG.query_ledger(type=type) if type else LG.query_ledger()


def test_first_model_succeeds_and_no_switch_is_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ledger_path: Path
) -> None:
    up = _Upstream([_ok("hi")])
    monkeypatch.setattr(ModelHub, "_http_post_json", up)
    hub = _hub(tmp_path, monkeypatch, [{"name": "a"}, {"name": "b"}])
    out = hub.chat(MSG)
    assert out["content"] == "hi" and out["model"] == "a" and out["failovers"] == 0
    assert out["usage"]["completion_tokens"] == 5
    assert out["request_id"] and len(out["request_id"]) >= 8
    assert [c["url"] for c in up.calls] == ["http://127.0.0.1:9/v1-a/chat/completions"]
    assert up.calls[0]["api_key"] == "sk-a", "带错密钥 ⇒ 症状会变成'那个端点一直 401'"
    assert _rows(ledger_path, "call") and not _rows(ledger_path, "switch")
    row = _rows(ledger_path, "call")[0]
    assert (row["model"], row["success"], row["attempts"], row["failovers"]) == ("a", True, 1, 0)
    assert row["content_chars"] == 2


def test_transient_error_retries_in_place_and_the_cost_is_real(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ledger_path: Path
) -> None:
    """401/429/5xx 在同一模型上原地重试一次（D-009，实测 2s 后即恢复）。

    重试不是免费的：代码里是 `time.sleep(1.5)`。这里把 sleep 抓住而不是放它真睡，
    同时**断言它确实睡了** —— 一次最坏两跳的调用会白等 3 秒，这个数字要能被用例说出口。
    """
    slept: list[float] = []
    monkeypatch.setattr(P.time, "sleep", lambda s: slept.append(s))
    up = _Upstream([GatewayError("401 偶发", http_status=401), _ok("第二次成")])
    monkeypatch.setattr(ModelHub, "_http_post_json", up)
    hub = _hub(tmp_path, monkeypatch, [{"name": "a"}, {"name": "b"}])
    out = hub.chat(MSG)
    assert out["transient_retried"] is True and out["model"] == "a"
    assert out["failovers"] == 0  # 原地重试不算切换
    assert len(up.calls) == 2 and slept == [1.5]
    rows = _rows(ledger_path, "call")
    assert [(r["success"], r["attempts"]) for r in rows] == [(True, 2)]
    st = hub.status()["models"]
    a = next(m for m in st if m["name"] == "a")
    assert a["consecutive_failures"] == 0, (
        "既有契约：一次成功即清零连败（见 test_modelhub_pool_and_keys）。"
        "重试路径也走同一条 —— 那次 401 只在台账里留痕，不该把模型踢出链路。"
    )


def test_non_transient_error_switches_without_retrying(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ledger_path: Path
) -> None:
    """400 这种"请求本身不对"不该原地重试（浪费 1.5 秒还必然失败）—— 直接换下一个模型。"""
    slept: list[float] = []
    monkeypatch.setattr(P.time, "sleep", lambda s: slept.append(s))
    up = _Upstream(
        [
            GatewayError("坏请求", http_status=400),
            _ok("来自备"),
        ]
    )
    monkeypatch.setattr(ModelHub, "_http_post_json", up)
    hub = _hub(tmp_path, monkeypatch, [{"name": "a", "priority": 1}, {"name": "b", "priority": 2}])
    out = hub.chat(MSG)
    assert out["model"] == "b" and out["failovers"] == 1
    assert slept == [], "400 走了原地重试路径"
    assert [_tag(c["url"]) for c in up.calls] == ["v1-a", "v1-b"]
    switches = _rows(ledger_path, "switch")
    assert [(s["from_model"], s["to_model"], s["failover_index"]) for s in switches] == [
        ("a", "b", 1)
    ]
    assert switches[0]["request_id"] == out["request_id"], "切换事件必须能跟这次调用对上账"


def test_fake_success_empty_choices_switches_rather_than_counting_as_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ledger_path: Path
) -> None:
    """200 但 choices 为空是"假成功"——把它当失败才会切换；当成功就会把空正文交给上游业务。"""
    monkeypatch.setattr(P.time, "sleep", lambda s: None)
    up = _Upstream([{"choices": []}, _ok("备的正文")])
    monkeypatch.setattr(ModelHub, "_http_post_json", up)
    hub = _hub(tmp_path, monkeypatch, [{"name": "a"}, {"name": "b"}])
    out = hub.chat(MSG)
    assert out["model"] == "b"
    failed = next(r for r in _rows(ledger_path, "switch"))
    assert "choices" in (failed["detail"] or "")


def test_all_models_failing_reports_the_order_and_the_last_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ledger_path: Path
) -> None:
    monkeypatch.setattr(P.time, "sleep", lambda s: None)
    up = _Upstream(
        [
            GatewayError("上游 500", http_status=500),
            GatewayError("重试仍 500", http_status=500),
            GatewayError("备也 502", http_status=502),
            GatewayError("重试仍 502", http_status=502),
        ]
    )
    monkeypatch.setattr(ModelHub, "_http_post_json", up)
    hub = _hub(tmp_path, monkeypatch, [{"name": "a"}, {"name": "b"}])
    with pytest.raises(ModelPoolExhaustedError) as e:
        hub.chat(MSG)
    msg = str(e.value)
    assert "a, b" in msg, f"没有列出尝试顺序：{msg}"
    assert "502" in msg, "最后一个错误没进消息"
    row = _rows(ledger_path, "call")[0]
    assert row["success"] is False and row["attempts"] == 2 and row["failovers"] == 1
    assert row["error_type"] == "GatewayError" and row["http_status"] == 502


def test_all_models_disabled_is_a_config_error_not_a_runtime_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ledger_path: Path
) -> None:
    """池子里一个 enabled 模型都没有 = 配置写错，**构造期**就该点名，不该等到第一个请求。"""
    up = _Upstream([])
    monkeypatch.setattr(ModelHub, "_http_post_json", up)
    with pytest.raises(P.ConfigError) as e:
        _hub(tmp_path, monkeypatch, [{"name": "a", "enabled": False}])
    assert "enabled" in str(e.value)
    assert up.calls == []


def test_every_model_broken_open_exhausts_the_pool_before_any_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ledger_path: Path
) -> None:
    """断路器全开时（配置合法、运行期不可用）才是 chat 期的 ModelPoolExhausted。"""
    monkeypatch.setattr(P.time, "sleep", lambda s: None)

    def down(url: str, api_key: str, payload: dict[str, Any], timeout: float) -> dict:
        raise GatewayError("一直 400", http_status=400)

    monkeypatch.setattr(ModelHub, "_http_post_json", staticmethod(down))
    hub = _hub(tmp_path, monkeypatch, [{"name": "solo"}])
    for _ in range(3):
        with pytest.raises(ModelPoolExhaustedError):
            hub.chat(MSG)
    assert hub.status()["models"][0]["circuit"].startswith("open")  # 读数带冷却态后缀
    up = _Upstream([])
    monkeypatch.setattr(ModelHub, "_http_post_json", up)
    with pytest.raises(ModelPoolExhaustedError) as e:
        hub.chat(MSG)
    assert "没有可用模型" in str(e.value)
    assert up.calls == [], "全部熔断还去打网络"


def test_specified_model_is_tried_first_and_unknown_name_falls_back_with_a_ledger_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ledger_path: Path
) -> None:
    up = _Upstream([_ok("备")])
    monkeypatch.setattr(ModelHub, "_http_post_json", up)
    hub = _hub(tmp_path, monkeypatch, [{"name": "a", "priority": 1}, {"name": "b", "priority": 2}])
    out = hub.chat(MSG, model="b")
    assert out["model"] == "b" and _tag(up.calls[0]["url"]) == "v1-b"

    up2 = _Upstream([_ok("主")])
    monkeypatch.setattr(ModelHub, "_http_post_json", up2)
    out2 = hub.chat(MSG, model="ghost")
    assert out2["model"] == "a", "请求了池外模型就该回落主备链，而不是报错"
    note = _rows(ledger_path, "switch")[-1]
    assert note["reason"] == "model_not_in_pool" and "ghost" in (note["detail"] or "")


def test_stream_true_is_a_400_and_writes_no_call_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ledger_path: Path
) -> None:
    """不支持的能力要在入口就拒绝（400），而不是打出去再说；也不该污染台账。"""
    up = _Upstream([])
    monkeypatch.setattr(ModelHub, "_http_post_json", up)
    hub = _hub(tmp_path, monkeypatch, [{"name": "a"}])
    with pytest.raises(GatewayError) as e:
        hub.chat(MSG, stream=True)
    assert e.value.http_status == 400
    assert up.calls == [] and _rows(ledger_path) == []


def test_extra_params_reach_the_upstream_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ledger_path: Path
) -> None:
    up = _Upstream([_ok()])
    monkeypatch.setattr(ModelHub, "_http_post_json", up)
    hub = _hub(tmp_path, monkeypatch, [{"name": "a"}])
    hub.chat(MSG, temperature=0.2, max_tokens=512, agent="bot-a", role="evaluator")
    sent = up.calls[0]["payload"]
    assert sent["temperature"] == 0.2 and sent["max_tokens"] == 512, (
        "chat() 的 **params 必须并进上游请求体"
    )
    assert sent["model"] and sent["messages"] == MSG
    row = _rows(ledger_path, "call")[0]
    assert (row["agent"], row["role"]) == ("bot-a", "evaluator"), (
        "归账字段丢了 ⇒ /v1/usage 按 agent 聚合会全空"
    )


def test_breaker_opens_after_the_threshold_and_the_model_is_then_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ledger_path: Path
) -> None:
    """连败到阈值（这里 3 次）就该出链：否则每个请求都要先陪它失败一遍。"""
    monkeypatch.setattr(P.time, "sleep", lambda s: None)

    def failing(url: str, api_key: str, payload: dict[str, Any], timeout: float) -> dict:
        if "v1-a" in url:
            raise GatewayError("一直 400", http_status=400)
        return _ok("备")

    monkeypatch.setattr(ModelHub, "_http_post_json", staticmethod(failing))
    hub = _hub(tmp_path, monkeypatch, [{"name": "a", "priority": 1}, {"name": "b", "priority": 2}])
    for _ in range(3):
        hub.chat(MSG)
    st = {m["name"]: m for m in hub.status()["models"]}
    assert st["a"]["circuit"].startswith("open"), st["a"]
    up = _Upstream([_ok("只走备")])
    monkeypatch.setattr(ModelHub, "_http_post_json", up)
    out = hub.chat(MSG)
    assert out["model"] == "b"
    assert all("v1-a" not in c["url"] for c in up.calls), "熔断后还在打主模型"
