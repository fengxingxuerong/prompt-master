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
