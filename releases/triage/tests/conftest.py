"""W7 收口（2026-11-20）后的测试环境：业务用例显式打开放行开关。

本文件**不 import 任何 app**（triage 的测试按路径 importlib 加载 app.py，避开与
lobster 同进程合跑时的 import mismatch，D-011），只设环境变量，因此不破坏那条约定。

`test_auth.py` 例外：它断言的就是「缺配时拒绝」，按文件名跳过。
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _legacy_open_for_business_tests(monkeypatch, request):
    if request.path.name == "test_auth.py":
        return
    monkeypatch.setenv("TRIAGE_ALLOW_OPEN", "1")
    monkeypatch.delenv("TRIAGE_TOKEN", raising=False)
