"""产物目录（logs/）解析的唯一实现。

为什么独立成模块：这份逻辑曾同时住在 `pm/scheduler._log_dir` 与
`pm/cli/support.log_dir` 两处——语义相同、默认路径各算各的，第三处迟早复现
`pm/schemas.py` 记过的那类"读死路径"事故。现在核心层与 CLI 层都从这里取；
`pm.cli.support` re-export 保持旧导入可用（schemas / calibration 的局部导入
不点名实现处，照常工作）。

晚绑定是有意的：CLI 的启动引导（`load_dotenv()`）与各测试的 env 覆盖都发生在
import 之后，import 期算出来的路径必然与运行期不一致（2026-09-25 实测：
产物落在 tmp，校准账本却写进仓库 logs，同一个变量两种命运）。
运行期取路径请用 `log_dir()`，不要拿 import 期冻结的常量再拼。
"""

from __future__ import annotations

import os
from pathlib import Path


def log_dir() -> Path:
    """产物目录的取法：每次调用都现读一遍 `PM_LOG_DIR`；没设时按运行形态分流。

    - **检出态**（包目录旁有 `run.py`）：仓库根/logs —— 与旧版逐字一致。
    - **安装态**（`pip install .`，包躺在 site-packages）：`<当前工作目录>/logs`
      —— site-packages 可能不可写，更不该当产物目录（2026-10-22 发布评审实测：
      装包后 logs 落进 site-packages）。与 console 的 `.env` 发现顺序同一哲学：
      装包态跟着调用方的工作目录走。
    """
    raw = os.getenv("PM_LOG_DIR", "").strip()
    if raw:
        return Path(raw)
    pkg_root = Path(__file__).resolve().parent.parent
    if (pkg_root / "run.py").is_file():
        return pkg_root / "logs"
    return Path.cwd() / "logs"
