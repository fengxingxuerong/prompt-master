"""测试通用夹具。

测量层（重复采样 / 基线 / 成对盲评）默认开启后会成倍增加 LLM 调用数，
而多数既有测试是在数调用次数、比排序、验证控制流。这里把它们固定回旧口径，
让新行为由 tests/test_measurement.py 专门覆盖 —— 免得回归测试变成"跟着实现改数字"。

需要在单测里启用新口径时，在测试体内 monkeypatch.setenv 覆盖即可（测试体晚于夹具执行）。
"""

from __future__ import annotations

import pytest


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
