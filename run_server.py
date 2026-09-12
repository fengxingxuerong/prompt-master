#!/usr/bin/env python3
"""PromptMaster 服务启动脚本。

用法：
    python run_server.py                  # 默认 127.0.0.1:8080，单 worker
    python run_server.py --port 9090
    python run_server.py --host 0.0.0.0   # 明确要放到局域网时再改
    python run_server.py --reload         # 开发模式（热重载）

启动后访问 http://127.0.0.1:8080/api/health 验证。
交互式 API 文档：http://127.0.0.1:8080/docs
设了环境变量 PM_API_TOKEN 时，POST 需带 `X-API-Key: <token>`。

要跑多 worker（`--workers 4`）必须先设 `PM_TASK_DB` 指向一个 SQLite 文件，
让各 worker 共享同一份任务记录；否则会强制回退为单 worker（status/report 会 404）。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from pm.bootstrap import ensure_utf8_stdio

# Windows 下 stdout 默认走系统码页（GBK），中文日志重定向到文件会乱码（L12）。
# 在任何打印之前统一 reconfigure 为 UTF-8，用户不再需要手动设 PYTHONIOENCODING。
ensure_utf8_stdio()


def main() -> None:
    import uvicorn

    ap = argparse.ArgumentParser(description="PromptMaster API 服务")
    ap.add_argument("--host", default="127.0.0.1", help="监听地址；要放到局域网显式传 0.0.0.0")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--reload", action="store_true", help="开发模式热重载")
    args = ap.parse_args()

    if args.workers > 1:
        # 多 worker 的前提是"记录能被所有进程看到"：任务表默认是进程内字典，
        # 那样 status/report 会随机 404。设了 PM_TASK_DB 走 SQLite 共享记录才放开。
        # 仍需注意：本地结果缓存（logs/*_cache.json）按进程各持一份 —— 多 worker 下
        # 缓存命中率下降（重复调用），但不会给出错误结论，属可接受代价。
        if not (os.getenv("PM_TASK_DB") or "").strip():
            print(
                "警告：--workers > 1 需要共享任务记录。请先设 PM_TASK_DB=<sqlite 路径>"
                "（各 worker 共享同一份记录），否则 status/report 会随机 404。"
                "已强制回退为 1 个 worker。",
                flush=True,
            )
            args.workers = 1
        else:
            print(
                f"多 worker 模式：任务记录走 {os.getenv('PM_TASK_DB')}；"
                "本地缓存按进程各持一份，命中率会下降。",
                flush=True,
            )

    uvicorn.run(
        "pm.server:app",
        host=args.host,
        port=args.port,
        workers=args.workers,
        reload=args.reload,
        log_level="info",
    )


if __name__ == "__main__":
    main()
