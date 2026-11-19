"""W7 收口（2026-11-20）后的测试环境：业务用例显式打开放行开关。

为什么不把 `TASKBOARD_ALLOW_OPEN=1` 写进各处夹具：taskboard 的测试原本是
「自包含」的（无 conftest，各文件按路径 importlib 加载 app.py），这条约定是为了
避开与 lobster 同进程合跑时的 import mismatch（D-011）。本文件**不 import 任何 app**，
只设环境变量与清理，因此不破坏那条约定。

`test_auth.py` 例外：它断言的就是「缺配时拒绝」，这里替它设开关等于把断言方向改掉，
所以按文件名跳过，交给那个文件自己 delenv。
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _legacy_open_for_business_tests(monkeypatch, request):
    if request.path.name == "test_auth.py":
        return
    monkeypatch.setenv("TASKBOARD_ALLOW_OPEN", "1")
    monkeypatch.delenv("TASKBOARD_TOKEN", raising=False)
