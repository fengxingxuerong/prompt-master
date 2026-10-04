"""ModelHub 网关的池/断路器/虚拟密钥回归（进程内，零出网）。

为什么补这块：`pm/modelhub/*` 实测覆盖率原本 34%（pool 23% / vkeys 21%），
而 2026-09-25 排查"流式通道丢了 return"时看清了原因 —— 网关的验收用例全在
`tests_modelhub/*.py` 那 5 个**脚本**里（要一个活网关 + 真 Key，pytest 收集 0 条），
所以流水线里这一整块等于没有测试。这里挑的是"错了会静默坑人"的三层：
配置解析（Key 丢了会不会悄悄用别的端点）、断路器（切不切、什么时候回来）、
虚拟密钥（停用后还能不能用）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from pm.modelhub import pool as P
from pm.modelhub import vkeys as V


# --------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------
def _spec(name: str = "m1", **kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"name": name, "base_url": "http://127.0.0.1:9/v1", "api_key": "sk-x"}
    base.update(kw)
    return base


def _config(pool_specs: list[dict[str, Any]]) -> dict[str, Any]:
    return {"pool": pool_specs}


@pytest.fixture
def cfg_file(tmp_path: Path) -> Path:
    p = tmp_path / "modelhub.json"
    p.write_text(
        json.dumps(_config([_spec("a", priority=1), _spec("b", priority=2)])), encoding="utf-8"
    )
    return p


@pytest.fixture
def hub(cfg_file: Path, monkeypatch: pytest.MonkeyPatch) -> P.ModelHub:
    monkeypatch.setenv("PMH_BREAK_THRESHOLD", "3")
    monkeypatch.setenv("PMH_COOLDOWN_SECONDS", "60")
    return P.ModelHub(config_path=cfg_file)


@pytest.fixture
def isolated_data(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """虚拟密钥/智能体台账落到 tmp_path：不碰真实 data/*.json。"""
    d = tmp_path / "vh-data"
    d.mkdir()
    monkeypatch.setenv("PMH_DATA_DIR", str(d))
    return d


# --------------------------------------------------------------------------
# 配置解析：占位符与错误必须显式
# ---------------------------------------------------------------------------
def test_placeholders_resolve_nested(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PM_API_KEY_Z", "sk-real")
    out = P._resolve_placeholders(
        {"a": "${PM_API_KEY_Z}", "list": [{"b": "${PM_API_KEY_Z}"}], "n": 5}, dict(os.environ)
    )
    assert out == {"a": "sk-real", "list": [{"b": "sk-real"}], "n": 5}


def test_missing_key_env_raises_naming_the_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    """少填一个 Key 必须启动即报错并点名变量。

    静默变成空串 = 拿空 Key 去打上游，症状是"401 到处飞"，谁也想不到是没配环境变量。
    """
    monkeypatch.delenv("PMH_ABSENT_VAR", raising=False)
    with pytest.raises(P.ConfigError) as e:
        P._resolve_placeholders({"api_key": "${PMH_ABSENT_VAR}"}, {})
    assert "PMH_ABSENT_VAR" in str(e.value)


def test_empty_string_counts_as_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    env = {"PMH_EMPTY": "   "}
    monkeypatch.setenv("PMH_EMPTY", "   ")
    with pytest.raises(P.ConfigError):
        P._resolve_placeholders({"k": "${PMH_EMPTY}"}, env)


def test_missing_config_file_error_mentions_the_example(tmp_path: Path) -> None:
    with pytest.raises(P.ConfigError) as e:
        P.ModelHub(config_path=tmp_path / "nope.json")
    assert "example" in str(e.value), "报错要给出可执行的下一步"


def test_invalid_pool_shapes_are_rejected(tmp_path: Path) -> None:
    cases = {
        "没有 pool 数组": {"other": 1},
        "pool 是空数组": {"pool": []},
        "pool 项不是对象": {"pool": ["x"]},
        "缺 api_key": {"pool": [{"name": "a", "base_url": "http://x/v1"}]},
        "全部 enabled=false": {"pool": [_spec("a", enabled=False)]},
    }
    for label, payload in cases.items():
        p = tmp_path / f"{abs(hash(label))}.json"
        p.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(P.ConfigError) as e:
            P.ModelHub(config_path=p)
        assert "pool" in str(e.value) or "enabled" in str(e.value), f"{label}：{e.value}"


def test_corrupt_config_is_a_config_error_with_path(tmp_path: Path) -> None:
    p = tmp_path / "broken.json"
    p.write_text("{ not json", encoding="utf-8")
    with pytest.raises(P.ConfigError) as e:
        P.ModelHub(config_path=p)
    assert str(p) in str(e.value)


def test_hot_reload_keeps_serving_and_exposes_the_error(hub: P.ModelHub, cfg_file: Path) -> None:
    """运行期配置坏掉：沿用旧池继续服务 + 把错误挂进 status()；启动期则拒绝。

    这条保护的意义：改坏配置文件不该把正在跑的网关弄崩；但"沿用旧配置"必须是
    看得见的状态，不能悄悄用着三天前的池。
    """
    cfg_file.write_text("{ broken", encoding="utf-8")
    # 不用 time.sleep 赌 mtime 跨格：windows runner 的文件系统时间戳粒度比本机更粗
    # （CI 两连红实证，本地怎么都复现不出），10ms 的盲睡赌不赢。与下方 utime 先例
    # 同款手法——显式把 mtime 拨到未来，让非强制热重载的 mtime 门**确定性**触发。
    future = time.time() + 10
    os.utime(cfg_file, (future, future))
    hub.list_models()  # 读一次，触发非强制热重载
    st = hub.status()
    assert st["last_config_error"], "热重载失败必须能从 status() 看到"
    assert len(st["models"]) == 2, "旧池要继续服务，不能清空"
    with pytest.raises(P.ConfigError):
        hub.reload()  # force 路径仍然显式报错


def test_bad_env_numbers_fall_back_to_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """PMH_* 填了非数字：回退默认而不是炸启动（但值确实不是用户要的那个）。"""
    monkeypatch.setenv("PMH_TIMEOUT", "abc")
    monkeypatch.setenv("PMH_BREAK_THRESHOLD", "-2")
    monkeypatch.setenv("PMH_COOLDOWN_SECONDS", "not-a-number")
    assert P._timeout_seconds() == 60.0
    assert P._break_threshold() == 3
    assert P._cooldown_seconds() > 0


# --------------------------------------------------------------------------
# 池视图与密钥不外泄
# ---------------------------------------------------------------------------
def test_public_dict_never_leaks_the_api_key() -> None:
    """/v1/models 是给人看的：upstream key 出现在任何公开视图里都是凭证外泄。"""
    entry = P.ModelEntry(_spec("m", api_key="sk-super-secret"))
    blob = json.dumps(entry.to_public_dict())
    assert "sk-super-secret" not in blob
    assert "api_key" not in entry.to_public_dict()


def test_upstream_model_defaults_to_public_name() -> None:
    assert P.ModelEntry(_spec("glm-5.2")).upstream_model == "glm-5.2"
    assert P.ModelEntry(_spec("外名", upstream_model="内名")).upstream_model == "内名"


def test_ordering_is_by_priority_then_name(hub: P.ModelHub) -> None:
    assert [e.name for e in hub._ordered_enabled()] == ["a", "b"]
    hub._entries[1].priority = 0
    assert [e.name for e in hub._ordered_enabled()] == ["b", "a"]


def test_disabled_models_never_enter_the_chain(tmp_path: Path) -> None:
    p = tmp_path / "mix.json"
    p.write_text(
        json.dumps(_config([_spec("on", enabled=True), _spec("off", enabled=False)])),
        encoding="utf-8",
    )
    h = P.ModelHub(config_path=p)
    assert [e.name for e in h._ordered_enabled()] == ["on"]


# --------------------------------------------------------------------------
# 断路器：连败到阈值才断，冷却到点自己回来
# ---------------------------------------------------------------------------
def test_breaker_opens_at_threshold_and_blocks_the_chain(hub: P.ModelHub) -> None:
    entry = hub._entries[0]
    hub._mark_failure(entry, "502")
    hub._mark_failure(entry, "502")
    assert entry.opened_at is None, "没到阈值就断=把偶发抖动当故障，池会被错误抽干"
    assert [e.name for e in hub._ordered_enabled()] == ["a", "b"]
    hub._mark_failure(entry, "502")
    assert entry.opened_at is not None
    assert [e.name for e in hub._ordered_enabled()] == ["b"], "断开后不该再被选中"


def test_status_reports_cooling_remaining(hub: P.ModelHub) -> None:
    """`cooling_remaining_s` 那段代码今天动过（为了给 opened_at 收窄类型），钉住行为。"""
    entry = hub._entries[0]
    for _ in range(3):
        hub._mark_failure(entry, "boom")
    models = {m["name"]: m for m in hub.status()["models"]}
    assert models["a"]["circuit"] == "open(cooling)"
    assert 0 < models["a"]["cooling_remaining_s"] <= 60
    assert models["b"]["circuit"] == "closed"
    assert models["b"]["cooling_remaining_s"] == 0


def test_one_success_clears_the_breaker(hub: P.ModelHub) -> None:
    entry = hub._entries[0]
    for _ in range(3):
        hub._mark_failure(entry, "boom")
    hub._mark_success(entry)
    assert (entry.opened_at, entry.consecutive_failures, entry.last_error) == (None, 0, None)
    assert "a" in [e.name for e in hub._ordered_enabled()]


def test_breaker_closes_itself_after_the_cooldown(hub: P.ModelHub, monkeypatch) -> None:
    monkeypatch.setenv("PMH_COOLDOWN_SECONDS", "0.2")
    entry = hub._entries[0]
    for _ in range(3):
        hub._mark_failure(entry, "boom")
    assert "a" not in [e.name for e in hub._ordered_enabled()]
    time.sleep(0.25)
    assert "a" in [e.name for e in hub._ordered_enabled()], "冷却到点必须自己回来（半开）"


def test_breaker_threshold_is_read_per_call(
    hub: P.ModelHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    """阈值改成 1 就该一击即断：它每次调用现读 env，不是启动时冻结的常量。"""
    monkeypatch.setenv("PMH_BREAK_THRESHOLD", "1")
    entry = hub._entries[0]
    hub._mark_failure(entry, "boom")
    assert entry.opened_at is not None


# --------------------------------------------------------------------------
# 响应校验：坏形状必须是语义异常，不是 KeyError
# ---------------------------------------------------------------------------
def test_validate_chat_response_rejects_missing_choices() -> None:
    with pytest.raises(P.GatewayError):
        P.ModelHub._validate_chat_response({"id": "x"})
    with pytest.raises(P.GatewayError):
        P.ModelHub._validate_chat_response({"choices": [{}]})
    assert P.ModelHub._validate_chat_response({"choices": [{"message": {"content": "hi"}}]}) == "hi"


# --------------------------------------------------------------------------
# 虚拟密钥：停用即拒、删除即拒、跨实例可见
# ---------------------------------------------------------------------------
def test_vkey_issue_verify_roundtrip(isolated_data: Path) -> None:
    store = V.VirtualKeyStore()
    rec = store.issue("agent-x", note="本地测试")
    assert rec["key"].startswith("vk-") and len(rec["key"]) > 20
    assert store.verify(rec["key"])["agent"] == "agent-x"
    assert store.verify("nope") is None
    assert store.verify("") is None
    assert store.verify("vk-not-issued") is None


def test_vkey_requires_an_agent(isolated_data: Path) -> None:
    store = V.VirtualKeyStore()
    with pytest.raises(V.ConfigError):
        store.issue("   ")


def test_disabled_vkey_is_rejected(isolated_data: Path) -> None:
    """停用必须让 verify 立刻返回 None —— 这是"吊销"唯一的执行点。"""
    store = V.VirtualKeyStore()
    key = store.issue("agent-y")["key"]
    assert store.set_enabled(key, False)["enabled"] is False
    assert store.verify(key) is None
    store.set_enabled(key, True)
    assert store.verify(key) is not None


def test_set_enabled_unknown_key_raises(isolated_data: Path) -> None:
    with pytest.raises(V.ConfigError):
        V.VirtualKeyStore().set_enabled("vk-does-not-exist", False)


def test_delete_removes_and_reports(isolated_data: Path) -> None:
    store = V.VirtualKeyStore()
    key = store.issue("agent-z")["key"]
    assert store.delete(key) is True
    assert store.verify(key) is None
    assert store.delete(key) is False, "删不存在的不该报错，但要如实返回 False"


def test_vkeys_are_visible_across_instances(isolated_data: Path) -> None:
    """另起一个 store 实例必须看得见已发的密钥：状态在盘上，不在内存里。
    （多 worker 场景下这是"进程内字典能不能当存储"的同一条测试。）"""
    key = V.VirtualKeyStore().issue("agent-w")["key"]
    assert V.VirtualKeyStore().verify(key)["agent"] == "agent-w"


def test_concurrent_issues_do_not_lose_keys(isolated_data: Path) -> None:
    """4 个线程各发一把：一把都不能丢。原子写 + 锁失守的表现是"发出去了但查不到"。

    线程里的异常必须带回主线程：2026-09-25 深夜全量跑红过一次，现场只有
    `assert 2 == 4` —— 两个 worker 根本没走到 append，而它们抛了什么**无处可查**
    （stderr 里没有线程栈）。那种红既不能归因也不能复现（同一条用例单跑 20 次、
    整文件 10 次、全量再 1 次都是绿），所以先把现场留住。
    """
    store = V.VirtualKeyStore()
    keys: list[str] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def worker() -> None:
        try:
            rec = store.issue("agent-c")
        except BaseException as e:  # noqa: BLE001 - 线程内异常必须显式回收，否则红得没有成因
            with lock:
                errors.append(e)
            return
        with lock:
            keys.append(rec["key"])

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, f"并发签发有 worker 抛异常：{[repr(e) for e in errors]}"
    assert len(set(keys)) == 4
    assert all(store.verify(k) is not None for k in keys), "并发写丢键"


def test_two_store_instances_in_one_process_do_not_lose_keys(isolated_data: Path) -> None:
    """同进程两个 store 实例并发签发：一把都不能丢，也不许抛。

    修前实测（两实例各发 200 把）：`issued=396 / 盘上 169`，还有一路 worker 直接抛
    `PermissionError(13, 另一个程序正在使用此文件)`。四个成因见 `docs/archive/fix-log.md` P30。
    这条**主要**盯的是"写前强制重读"：把它摘掉，3/3 红。
    共享路径锁与唯一 tmp 名是补完 —— 单独把它们换回旧写法，本机 3 轮都没复现丢失
    （强制重读已把窗口压到约 200µs），所以那两处不在这条的证据链里，别照着这个用例声称。
    """
    issued: list[str] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def worker(tag: str) -> None:
        store = V.VirtualKeyStore()  # 各建各的实例：就是要不复用同一把实例锁
        for _ in range(100):
            try:
                rec = store.issue(f"agent-{tag}")
            except BaseException as e:  # noqa: BLE001 - 线程内异常必须带回主线程
                with lock:
                    errors.append(e)
                return
            with lock:
                issued.append(rec["key"])

    threads = [threading.Thread(target=worker, args=(t,)) for t in ("x", "y")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, f"两个实例并发签发抛了：{[repr(e) for e in errors]}"
    on_disk = {r["key"] for r in V.VirtualKeyStore().list()}
    assert len(issued) == 200
    missing = [k for k in issued if k not in on_disk]
    assert not missing, f"并发签发丢了 {len(missing)} 把（发出但盘上没有）"


def test_verify_sees_a_key_written_in_the_same_filesystem_tick(isolated_data: Path) -> None:
    """`st_mtime` 没跨格时，热载门会"看不见"别人刚写的密钥 —— 鉴权路径不许因此判 None。

    本机实测粒度约 0.3~0.5ms，连续两次写有 78~84% 落在同一格里；两个实例紧挨着跑
    "A 签发 → B 校验"，修前 B 有 **17/300** 次说"这把不存在"（网关表现就是把合法 Key 401 掉）。
    这里用 `os.utime` 把时间戳按回去，把那个窗口做成**确定性**的：不靠运气复现。
    """
    a = V.VirtualKeyStore()
    b = V.VirtualKeyStore()
    a.issue("agent-early")  # 让 B 先建立基线快照与 mtime
    b.list()
    path = isolated_data / "vkeys.json"
    frozen = path.stat().st_mtime
    rec = a.issue("agent-late")
    os.utime(path, (path.stat().st_atime, frozen))  # 模拟"写了，但 mtime 没动"
    assert b.verify(rec["key"]) is not None, "mtime 未跨格时 verify 应强制重读一次再判不存在"


def test_two_processes_do_not_lose_issued_keys(isolated_data: Path) -> None:
    """跨进程（`--workers > 1`）两个解释器各发 60 把：盘上必须 120 把，且两路都不许崩。

    修前实测 3 轮：盘上 126 / 151 / 150 把（丢一半上下），其中 2 轮有一路子进程直接
    死在 `ConfigError：虚拟密钥库不可读 [Errno 13] Permission denied`。
    同进程的 RLock 跨不了进程，Windows 上不同文件句柄的字节锁在同进程内也互相不冲突 ——
    所以这条要真的起两个解释器才量得到。
    """
    child = isolated_data / "issue_child.py"
    root = Path(__file__).resolve().parents[1]
    child.write_text(
        "import sys\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "from pm.modelhub import vkeys as V\n"
        "s = V.VirtualKeyStore()\n"
        "print(sum(1 for _ in range(int(sys.argv[2])) if s.issue('agent-proc')))\n",
        encoding="utf-8",
    )
    # 真并发：两个子进程必须同时在跑（用 subprocess.run 串起来 = 一个跑完才起下一个，
    # 那样锁被摘掉也照样绿，什么都测不到）。
    procs = [
        subprocess.Popen(
            [sys.executable, str(child), str(root), "60"],
            cwd=str(root),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env={**os.environ, "PMH_DATA_DIR": str(isolated_data)},
        )
        for _ in range(2)
    ]
    outs = []
    for p in procs:  # 先起完再收：communicate 会等退出，顺序收不会漏管道缓冲
        out, err = p.communicate()
        outs.append((p.returncode, out, err))
    for rc, out, err in outs:
        assert rc == 0, f"子进程签发崩了：rc={rc} {out[-300:]} {err[-300:]}"
        assert out.strip() == "60"
    keys = {r["key"] for r in V.VirtualKeyStore().list()}
    assert len(keys) == 120, f"两个进程各发 60 把，盘上只有 {len(keys)} 把（跨进程互相覆盖）"


def test_corrupt_vkey_store_file_raises_with_path(isolated_data: Path) -> None:
    (isolated_data / "vkeys.json").write_text("{ broken", encoding="utf-8")
    with pytest.raises(V.ConfigError) as e:
        V.VirtualKeyStore().list()
    assert "vkeys.json" in str(e.value)


# --------------------------------------------------------------------------
# 智能体注册（同一套持久化纪律）
# ---------------------------------------------------------------------------
def test_agent_register_get_unregister(isolated_data: Path) -> None:
    store = V.AgentStore()
    rec = store.register("openclaw-demo", framework="openclaw", default_role="assistant")
    assert rec["name"] == "openclaw-demo"
    assert store.get("openclaw-demo") is not None
    assert store.get("nobody") is None
    assert "openclaw-demo" in [a["name"] for a in store.list()]
    assert store.unregister("openclaw-demo") is True
    assert store.get("openclaw-demo") is None
    assert store.unregister("openclaw-demo") is False
