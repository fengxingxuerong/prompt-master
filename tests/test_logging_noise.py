"""默认日志的**降噪**判据（2026-10-22）。

真实事故形状：`.env` 里有 Key 的人敲 `run.py --task …`，首屏先是一条条
`INFO httpx2 HTTP Request: POST … 200 OK`（**每次调用一行**，一轮 45~102 行），
用户看到的不是结论而是流水账。

根因是名单**停在了旧的 HTTP 栈**：`setup_logging` 只静默 `httpx/httpcore/openai`，
而 `openai` / `langsmith` 早已迁到 **`httpx2`**（实测 `httpx2-2.12.0`，
Required-by: langsmith, openai）⇒ 一行没少。

三条判据：
1. 非 verbose 时，整条传输层栈都被压到 WARNING；
2. `-v` 时显式放回 NOTSET（继承 root=DEBUG）——否则"先跑过非 verbose 再开 -v"会哑火；
3. 名单必须**覆盖当场已安装**的 HTTP 客户端（换一代就红，逼着名单跟着栈走）。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from importlib.metadata import distributions

import pytest
from pm.cli.support import _QUIET_LOGGERS, setup_logging

_HTTP_DIST = re.compile(r"^(httpx\d*|httpcore\d*|urllib3)$")


@pytest.fixture(autouse=True)
def _restore_logger_levels() -> Iterator[None]:
    """setup_logging 改的是**全局** logger 级别，测完必须还回去（别污染别的用例）。"""
    root = logging.getLogger()
    saved_root = root.level
    saved = {name: logging.getLogger(name).level for name in _QUIET_LOGGERS}
    try:
        yield
    finally:
        root.setLevel(saved_root)
        for name, level in saved.items():
            logging.getLogger(name).setLevel(level)


def _installed_http_client_names() -> set[str]:
    out: set[str] = set()
    for dist in distributions():
        raw = (dist.metadata["Name"] or "").strip().lower().replace("_", "-")
        if raw and _HTTP_DIST.match(raw):
            out.add(raw)
    return out


def test_non_verbose_silences_the_transport_stack() -> None:
    setup_logging(verbose=False)
    for name in _QUIET_LOGGERS:
        assert logging.getLogger(name).level == logging.WARNING, (
            f"{name} 没被压到 WARNING —— 它会按每次 HTTP 调用打一行"
        )


def test_verbose_puts_the_transport_stack_back_to_root_level() -> None:
    """先跑非 verbose、再开 -v：必须放回 NOTSET，否则上一次的 WARNING 把 -v 钉死。"""
    setup_logging(verbose=False)
    setup_logging(verbose=True)
    for name in _QUIET_LOGGERS:
        logger = logging.getLogger(name)
        assert logger.level == logging.NOTSET, name
        assert logger.getEffectiveLevel() == logging.DEBUG, name


def test_the_quiet_list_covers_every_installed_http_client() -> None:
    """名单必须跟着**实际安装**的 HTTP 栈走。

    2026-10-22 的真实缺口：`openai` / `langsmith` 迁到 `httpx2`，而名单还停在 `httpx`。
    这条在"下一代客户端"装进来时立刻红 —— 不红的话，静默就只是看着像做了。
    """
    installed = _installed_http_client_names()
    assert installed, "一个 HTTP 客户端都没扫到：枚举方式变了，这条守卫会假绿"
    missing = sorted(installed - set(_QUIET_LOGGERS))
    assert not missing, (
        f"这些已安装的 HTTP 客户端不在 _QUIET_LOGGERS 里：{missing}。\n"
        "它们按每次调用打一行（一轮 45~102 行），漏一个就等于没静默。"
    )
