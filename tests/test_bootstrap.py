"""Windows 编码兜底（L12）回归。

旧版靠 README 提醒 `$env:PYTHONIOENCODING="utf-8"`，忘了设时中文日志重定向到文件
直接 UnicodeEncodeError。现在两个入口在打印前调用 `ensure_utf8_stdio()`。
子进程测试模拟真实场景：Windows 上以 cp936（GBK）默认码页起 Python，重定向 stdout，
验证入口脚本不再因中文输出崩掉。
"""

from __future__ import annotations

import os
import subprocess
import sys

from pm.bootstrap import ensure_utf8_stdio


def test_already_utf8_streams_are_left_alone():
    """判据是"这条流能不能装下要印的东西"，不是操作系统。

    原来这条叫 `noop_on_non_windows`，配的是 `if os.name != "nt": return` —— 那句
    等于假设"非 Windows 一定是 UTF-8"，而 `scripts/logs_usage.py` 那次 CI 绿、本机红
    正是这个假设的产物（GH 的 windows runner 缺省码页就是 UTF-8，没人替用户崩过）。
    """

    class _Stream:
        def __init__(self, encoding):
            self.encoding = encoding
            self.calls = []

        def reconfigure(self, **kw):
            self.calls.append(kw)

    out, err = _Stream("utf-8"), _Stream("UTF8")
    orig = (sys.stdout, sys.stderr)
    sys.stdout, sys.stderr = out, err
    try:
        ensure_utf8_stdio()
    finally:
        sys.stdout, sys.stderr = orig
    assert out.calls == [] and err.calls == [], f"已经是 UTF-8 的流被改了：{out.calls}{err.calls}"

    gbk_out = _Stream("gbk")
    sys.stdout, sys.stderr = gbk_out, _Stream("gbk")
    try:
        ensure_utf8_stdio()
    finally:
        sys.stdout, sys.stderr = orig
    assert gbk_out.calls == [{"encoding": "utf-8"}], "非 UTF-8 的流没被纠正，cp936 事故会重演"


def test_run_entry_survives_gbk_redirect(tmp_path):
    """真实场景：默认码页下跑 run.py --mermaid，stdout 重定向到文件不炸。

    仅在 Windows 上执行断言（非 Windows 默认码页本就是 UTF-8，没有区分度）。
    """
    if os.name != "nt":
        return
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out_file = tmp_path / "out.txt"
    env = dict(os.environ)
    env.pop("PYTHONIOENCODING", None)  # 模拟用户忘了设
    env.pop("PYTHONUTF8", None)
    proc = subprocess.run(
        [sys.executable, os.path.join(repo, "run.py"), "--mermaid"],
        capture_output=True,
        env=env,
        cwd=repo,
        timeout=120,
    )
    out_file.write_bytes(proc.stdout)
    assert proc.returncode == 0, (
        f"入口在默认码页下崩了：{proc.stderr.decode('utf-8', 'replace')[-500:]}"
    )
    assert "graph TD" in out_file.read_text(encoding="utf-8")


def test_server_entry_prints_chinese_to_redirect():
    """run_server.py 的启动横幅/警告输出重定向到文件不炸。

    不真正起服务：用一个非法端口让它立刻退出，但在此之前模块级中文告警
    （未设 PM_API_TOKEN 的 warning 走 logging → stderr）已经产生。
    """
    if os.name != "nt":
        return
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = dict(os.environ)
    env.pop("PYTHONIOENCODING", None)
    env.pop("PYTHONUTF8", None)
    proc = subprocess.run(
        [sys.executable, os.path.join(repo, "run_server.py"), "--port", "not-a-port"],
        capture_output=True,
        env=env,
        cwd=repo,
        timeout=120,
    )
    # 端口解析失败会非零退出，但失败原因必须是可读的用法错误，而不是 UnicodeEncodeError
    err = proc.stderr.decode("utf-8", "replace")
    assert "UnicodeEncodeError" not in err, f"编码兜底未生效：{err[-500:]}"
