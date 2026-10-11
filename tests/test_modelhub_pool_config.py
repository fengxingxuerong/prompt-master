"""`pm/modelhub/pool.py` 的配置加载 + 热重载保护回归（进程内，零出网）。

为什么单独一个文件：池子的失败不只发生在网络上，更多发生在"配置那一刻"——
密钥占位符没解析、配置写坏、运行期配置被删/被改坏。这些的**共同危险是静默**：
`_reload_if_needed` 第 210-257 行的注释说清了设计取舍——启动期（force）或没有旧配置
可用时，显式 ConfigError 拒绝启动；但**已经跑着的进程在配置突然变坏时要沿用旧配置
继续服务**（不能因为一次手误编辑就把在线的池子打空），只把错误透到 `status()` 的
`last_config_error` 字段让人看见。

这条取舍两侧都没测试过：一边没人保证"坏配置真的会拒绝启动"，另一边没人保证
"坏配置不会把正在服务的池子打空"。这里两侧都钉住。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pm.modelhub import pool as P
from pm.modelhub.pool import ConfigError, ModelHub


def _spec(name: str = "a", **kw: Any) -> dict[str, Any]:
    return {
        "name": name,
        "base_url": "http://127.0.0.1:9/v1",
        "api_key": "sk-x",
        **kw,
    }


def _write_cfg(
    path: Path,
    specs: list[dict[str, Any]] | None = None,
    raw: str | None = None,
    bump: float = 5.0,
) -> Path:
    """写配置并把 mtime 往后推 `bump` 秒。

    热重载是按 mtime 触发的；连续两次写可能落在同一时间戳刻度上，
    那样第二次根本不会触发重载，测的就不是"坏了会怎样"而是"根本没看文件"。
    """
    path.write_text(
        raw if raw is not None else json.dumps({"pool": specs or [_spec("a"), _spec("b")]}),
        encoding="utf-8",
    )
    _bump_mtime(path, bump)
    return path


def _bump_mtime(path: Path, seconds: float) -> None:
    import os

    st = path.stat()
    os.utime(path, (st.st_atime + seconds, st.st_mtime + seconds))


@pytest.fixture
def isolated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """不加载项目根 .env，也不让本机 PMH_* 变量影响阈值解析。"""
    monkeypatch.setattr(P, "load_dotenv", None)
    monkeypatch.delenv("PMH_CONFIG", raising=False)
    monkeypatch.delenv("PMH_BREAK_THRESHOLD", raising=False)


# --------------------------------------------------------------------------
# 配置解析：坏了要喊出来
# --------------------------------------------------------------------------
def test_project_root_points_at_the_repo_root(isolated_env: None) -> None:
    """`parents[2]` 是默认配置路径与 .env 的锚；指错了症状是"明明有配置却说找不到"。"""
    root = P._project_root()
    assert (root / "pm" / "modelhub" / "pool.py").is_file(), root


def test_missing_config_file_is_an_error_not_an_empty_pool(
    isolated_env: None, tmp_path: Path
) -> None:
    with pytest.raises(ConfigError) as e:
        P._load_json(tmp_path / "nope.json")
    assert "不存在" in str(e.value)


def test_unreadable_config_file_is_reported_as_such(isolated_env: None, tmp_path: Path) -> None:
    """存在但读不了（目录/BACL 拒绝）必须和"不存在"区分开：前者是环境问题，
    后者提醒你从 example 复制一份。"""
    a_dir = tmp_path / "confdir"
    a_dir.mkdir()
    with pytest.raises(ConfigError) as e:
        P._load_json(a_dir)
    assert "不可读" in str(e.value)


def test_corrupt_json_points_at_the_line_number(isolated_env: None, tmp_path: Path) -> None:
    p = tmp_path / "modelhub.json"
    p.write_text('{\n  "pool": [\n    {"name": oops}\n  ]\n}\n', encoding="utf-8")
    with pytest.raises(ConfigError) as e:
        P._load_json(p)
    assert "损坏" in str(e.value)


def test_top_level_must_be_an_object(isolated_env: None, tmp_path: Path) -> None:
    """顶层写成数组（pool 直接放最外面）时不能让它变成 `data.get("pool")` 静默取空。"""
    p = tmp_path / "modelhub.json"
    p.write_text(json.dumps([{"name": "a"}]), encoding="utf-8")
    with pytest.raises(ConfigError) as e:
        P._load_json(p)
    assert "顶层" in str(e.value)


def test_pool_array_missing_or_empty_is_rejected(isolated_env: None, tmp_path: Path) -> None:
    for raw in ('{"pool": []}', '{"models": []}'):
        p = _write_cfg(tmp_path / f"cfg-{abs(hash(raw))}.json", raw=raw)
        with pytest.raises(ConfigError) as e:
            ModelHub(config_path=p)
        assert "pool" in str(e.value)


def test_entry_missing_required_field_names_it(isolated_env: None, tmp_path: Path) -> None:
    p = _write_cfg(tmp_path / "c.json", [_spec("a"), {"name": "b", "base_url": "http://x"}])
    with pytest.raises(ConfigError) as e:
        ModelHub(config_path=p)
    assert "api_key" in str(e.value)


def test_dotenv_placeholder_that_resolves_empty_is_a_config_error(
    isolated_env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`api_key: "${PM_API_KEY_X}"` 而变量不存在 → 必须拒绝启动。若静默放过，
    症状是每个模型都 401，而 401 不会告诉你是环境变量空着。"""
    monkeypatch.delenv("PM_API_KEY_X", raising=False)
    p = _write_cfg(tmp_path / "c.json", [_spec("a", api_key="${PM_API_KEY_X}")])
    with pytest.raises(ConfigError) as e:
        ModelHub(config_path=p)
    assert "PM_API_KEY_X" in str(e.value)


def test_dotenv_placeholder_inside_nested_structures_is_resolved(
    isolated_env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """占位符不只出现在顶层字段里（head/family 等嵌套位置同样可能放变量名）。
    递归分支漏掉 dict/list 时，`str.replace` 版本会把正常字符串也误判。"""
    monkeypatch.setenv("PM_API_KEY_NEST", "sk-nested")
    monkeypatch.setenv("PM_NOTES_NEST", "备注深一层")
    p = _write_cfg(
        tmp_path / "c.json",
        [
            {
                "name": "a",
                "base_url": "http://127.0.0.1:9/v1",
                "api_key": "${PM_API_KEY_NEST}",
                "family": "deepseek",
                "notes": "${PM_NOTES_NEST}",
            }
        ],
    )
    hub = ModelHub(config_path=p)
    m = hub.list_models()[0]
    assert m["name"] == "a"


def test_dotenv_is_explicitly_loaded_on_construction(
    isolated_env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """构造期要加载项目根 .env（占位符靠它解析）。这条钉住"加载动作本身"还在，
    否则换机器上表现为"本机能跑、CI 上报找不到密钥"。"""
    seen: list[Path] = []

    def fake_load(path: Path) -> bool:
        seen.append(path)
        return True

    monkeypatch.setattr(P, "load_dotenv", fake_load)
    monkeypatch.setenv("PM_API_KEY_SELFTEST", "sk-ok")
    p = _write_cfg(tmp_path / "c.json", [_spec("a", api_key="${PM_API_KEY_SELFTEST}")])
    ModelHub(config_path=p)
    assert seen and seen[0].name == ".env", seen


# --------------------------------------------------------------------------
# 运行期热重载：坏配置不能把正在服务的池子打空
# --------------------------------------------------------------------------
def test_config_deleted_at_runtime_keeps_serving_the_old_pool(
    isolated_env: None, tmp_path: Path
) -> None:
    """配置被误删/被 mv 走：已在服务中的池必须接着用，只把错误暴露到 status()。"""
    p = _write_cfg(tmp_path / "c.json")
    hub = ModelHub(config_path=p)
    before = hub.list_models()
    assert len(before) == 2
    p.unlink()
    after = hub.list_models()
    assert after == before, "配置文件没了就把池子清空了"
    assert "不存在" in (hub.status()["last_config_error"] or "")


def test_stat_failure_at_runtime_degrades_to_old_config(
    isolated_env: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`exists()` 说在、`stat()` 却失败（ACL/竞态）：同样沿用旧配置。
    这一侧的 if 如果写反成"一律 raise"，一次抖动就会打空在线池子。"""
    p = _write_cfg(tmp_path / "c.json")
    hub = ModelHub(config_path=p)
    monkeypatch.setattr(Path, "exists", lambda self, **kw: True)

    def boom(self: Path, **kw: Any) -> Any:
        raise OSError(5, "拒绝访问")

    monkeypatch.setattr(Path, "stat", boom)
    assert len(hub.list_models()) == 2
    err = hub.status()["last_config_error"] or ""
    assert "不可访问" in err, err


def test_stat_failure_at_startup_is_fatal(
    isolated_env: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`exists()` 说在、`stat()` 却失败，且**还没有旧配置可沿用**：此时必须拒绝启动
    （不能拿一个空池子顶上去）。与"运行期沿用旧配置"是同一条 except 的两侧。"""
    p = _write_cfg(tmp_path / "c.json")
    monkeypatch.setattr(Path, "exists", lambda self, **kw: True)

    def boom(self: Path, **kw: Any) -> Any:
        raise OSError(5, "拒绝访问")

    monkeypatch.setattr(Path, "stat", boom)
    with pytest.raises(ConfigError) as e:
        ModelHub(config_path=p)
    assert "不可访问" in str(e.value)


def test_broken_config_while_running_falls_back_to_the_previous_entries(
    isolated_env: None, tmp_path: Path
) -> None:
    """配置还在但内容坏了（JSON 断了/占位符丢了）：旧池继续服务。"""
    p = _write_cfg(tmp_path / "c.json")
    hub = ModelHub(config_path=p)
    good = hub.list_models()
    p.write_text('{"pool": [ {"name": ', encoding="utf-8")  # 写一半的 JSON
    _bump_mtime(p, 5.0)
    assert hub.list_models() == good
    assert "热重载失败" in (hub.status()["last_config_error"] or "")


def test_repaired_config_is_picked_up_on_the_next_reload(
    isolated_env: None, tmp_path: Path
) -> None:
    """修好之后要能恢复（否则"沿用旧配置"会变成永久粘住）。"""
    p = _write_cfg(tmp_path / "c.json", [_spec("a")])
    hub = ModelHub(config_path=p)
    p.write_text('{"pool": []}', encoding="utf-8")
    _bump_mtime(p, 5.0)
    assert hub.status()["last_config_error"]
    _write_cfg(p, [_spec("a"), _spec("b"), _spec("c")])
    assert len(hub.list_models()) == 3, "配置修好了却没恢复"
    assert hub.status()["last_config_error"] is None


def test_no_enabled_model_is_a_config_even_at_runtime(isolated_env: None, tmp_path: Path) -> None:
    """全 disabled：启动期就报，不该等到第一个请求才发现池子是空的。"""
    p = _write_cfg(tmp_path / "c.json", [_spec("a", enabled=False)])
    with pytest.raises(ConfigError):
        ModelHub(config_path=p)


# --------------------------------------------------------------------------
# 阈值解析：坏值一律回退默认，绝不"更激进"
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("raw", "expected"),
    [("", 3), ("garbage", 3), ("-2", 3), ("0", 3), ("7", 7)],
)
def test_break_threshold_falls_back_to_the_default_on_bad_values(
    isolated_env: None, monkeypatch: pytest.MonkeyPatch, raw: str, expected: int
) -> None:
    """`PMH_BREAK_THRESHOLD=0/-2` 这种手误原本会变成"一击即断"（比默认更激进），
    症状是"池子莫名其妙只剩一个模型"。坏值必须回退默认值而不是被"合理化"。"""
    monkeypatch.setenv("PMH_BREAK_THRESHOLD", raw)
    assert P._break_threshold() == expected


def test_routes_are_the_published_surface(isolated_env: None, tmp_path: Path) -> None:
    """`list_routes()` 是 /v1 的对外契约面；改路由要连这条一起改。"""
    p = _write_cfg(tmp_path / "c.json", [_spec("a")])
    hub = ModelHub(config_path=p)
    assert hub.list_routes() == [
        "/v1/models",
        "/v1/chat/completions",
        "/v1/ledger",
        "/v1/pool/status",
        "/v1/agents",
    ]
