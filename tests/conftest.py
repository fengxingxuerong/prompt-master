"""测试通用夹具。

测量层（重复采样 / 基线 / 成对盲评）默认开启后会成倍增加 LLM 调用数，
而多数既有测试是在数调用次数、比排序、验证控制流。这里把它们固定回旧口径，
让新行为由 tests/test_measurement.py 专门覆盖 —— 免得回归测试变成"跟着实现改数字"。

需要在单测里启用新口径时，在测试体内 monkeypatch.setenv 覆盖即可（测试体晚于夹具执行）。
"""

from __future__ import annotations

import os as _os
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# W16 修复（2026-09-22）：顺序依赖根因。
# pm.llm 在模块级 load_dotenv() 会把宿主机 .env 的 PM_JUDGE_JITTER=2.6 灌进进程
# 环境；pm.schemas 又在 import 时把该值冻结进模块常量 JUDGE_JITTER。于是
# "谁先被 import" 决定测量层用例结果：单跑 test_measurement 时 schemas 先于
# llm 加载 → 0.0（绿）；先跑 test_api 时 llm 先灌 env → 2.6（红）。
# 这里在任意测试模块加载前把常量钉回旧口径 0.0；需要测抖动的用例一律
# monkeypatch.setattr(schemas, "JUDGE_JITTER", ...) 显式覆盖（既有用例已如此）。
# ---------------------------------------------------------------------------
_os.environ.pop("PM_JUDGE_JITTER", None)

from pm import schemas as _pm_schemas

_pm_schemas.JUDGE_JITTER = 0.0


@pytest.fixture(autouse=True)
def legacy_measurement_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PM_SAMPLES_PER_CASE", "1")
    monkeypatch.setenv("PM_BASELINE", "0")
    monkeypatch.setenv("PM_PAIRWISE", "0")


@pytest.fixture(autouse=True)
def no_shared_disk_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认关掉落盘缓存：不让开发机 `logs/*_cache.json` 里的残留决定测试结果。

    真实场景：写一个断言“评委被调了一次”的用例，在自己机器上绿、在别人机器上红了 ——
    因为同样的 prompt/input/output 已经在缓存里。测试要验缓存时必须显式开
    （自带 tmp_path 当 PM_CACHE_DIR），否则一律走真调用。
    """
    monkeypatch.setenv("PM_EVAL_CACHE", "0")
    monkeypatch.setenv("PM_TARGET_CACHE", "0")


@pytest.fixture(autouse=True)
def isolate_modelhub_write_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """ModelHub 的两个**写入**目标一律封进 tmp_path：测试不可能碰到运营文件。

    真实事故（2026-09-25，我自己造成的）：新写的台账用例把隔离变量记成了 `PMH_DATA_DIR`，
    而 `ledger.ledger_path()` 读的是 `PMH_LEDGER_PATH`（默认 `<repo>/logs/modelhub_ledger.jsonl`）
    —— 于是夹具"看起来设了"，实际一次 `write_text` 把线上台账（411 条调用事件，
    覆盖 09-21 21:20 轮换之后到今天）截断成 5 行测试数据。轮换件 `.1` 之前的历史还在，
    **那 411 条找不回来了**。

    为什么修在 conftest 而不是那个测试文件里：这类失败的形式是"某个用例忘了设一个变量"，
    code review 防不住，下一个写 modelhub 测试的人一样会踩。封在这里，忘了设也只是写进 tmp。
    只封写路径（台账 JSONL + 日聚合目录），不封 PMH_CONFIG：那是只读的，
    且把它指向不存在的路径会让"起网关就 ConfigError"的正常断言变成假红。
    """
    store = tmp_path / "modelhub-store"
    store.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("PMH_LEDGER_PATH", str(store / "modelhub_ledger.jsonl"))
    monkeypatch.setenv("PMH_DATA_DIR", str(store))


@pytest.fixture(autouse=True)
def isolate_host_connection_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """单测与宿主机 .env 的连接类配置隔离：API Key / 端点 / 模型名一律清空。

    pm.llm 在模块级 load_dotenv() 会把真实 .env 灌进进程环境，而连接配置的解析
    全部走"角色级键优先"：_key_pool 角色键存在就不进全局池、get_llm 角色配置
    覆盖全局、preflight 表格按角色取 key——宿主机 .env 里的角色级 Key 一旦存在，
    测试 monkeypatch 的假值就被架空，单测结果随宿主机配置漂移
    （真实事故：key 池合并、anthropic 分支、preflight 掩码三个断言漏出真实 Key）。
    这里把连接类键全部清掉，测试从空白连接配置开始，需要时自己 setenv。
    行为开关（PM_JUDGES / PM_TIMEOUT 等）不在此夹具范围。
    """
    import os

    for k in list(os.environ):
        up = k.upper()
        if not up.startswith("PM_"):
            continue
        if (
            up.endswith("_API_KEY")
            or up.endswith("_BASE_URL")
            or up.endswith("_MODEL")
            or up in ("PM_API_KEYS", "PM_PROVIDER")
        ):
            monkeypatch.delenv(k, raising=False)
