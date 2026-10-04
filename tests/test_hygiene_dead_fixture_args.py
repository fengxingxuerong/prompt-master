"""用例不许再要一个它不用的内置夹具（`tmp_path` / `capsys` / `monkeypatch`）。

为什么钉这条（2026-10-04）：这类形参在测试里读起来像"这个用例依赖临时目录/改过 env/
捕获了输出"，而它其实什么都没做。三处代价都是实的：
① 误导读者 —— 看签名会以为用例在隔离文件系统，实际它可能在写真实 `data/`；
② 白付开销 —— `tmp_path` 每条都要建目录，几百条累积成秒级；
③ 掩盖真问题 —— 一个"要了 monkeypatch 却没用"的用例，常常是夹具挪走了、
   断言忘了跟着改，而这正是需要被看见的那一刻。

判据取自 ruff 自己的 ARG001（与 CI 同一口径，不另写一套 AST）。
**只判白名单里的内置夹具**：`isolated_data` / `isolated` / `ledger_path` 这类自定义夹具
即使形参没被引用也是**在干活的**（副作用就是用例的前提），删掉会把用例打到真实数据上。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 内置夹具：这些名字的"没用到"是确定的死依赖，因为 pytest 只在被调用时才产生效果
_BUILTIN_FIXTURES = {
    "tmp_path",
    "tmp_path_factory",
    "capsys",
    "capsysbinary",
    "monkeypatch",
    "recwarn",
}


def test_no_test_asks_for_a_builtin_fixture_it_never_uses() -> None:
    out = subprocess.run(
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--select",
            "ARG001",
            "--output-format",
            "json",
            "-q",
            "tests/",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert out.returncode in (0, 1), f"ruff 没跑起来（rc={out.returncode}）：{out.stderr[:300]}"
    try:
        rows = json.loads(out.stdout or "[]")
    except json.JSONDecodeError as e:
        raise AssertionError(f"ruff 的输出不是 JSON，判据失效：{e}") from e

    dead = sorted(
        {
            f"{r['filename']}:{r['location']['row']} `{r['message'].split('`')[1]}`"
            for r in rows
            if "Unused function argument" in r.get("message", "")
            and r["message"].split("`")[1] in _BUILTIN_FIXTURES
        }
    )
    assert not dead, "这些用例要了一个它根本不用的内置夹具（删掉形参即可）：\n  " + "\n  ".join(
        dead
    )
