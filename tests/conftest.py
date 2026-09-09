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
