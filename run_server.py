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
"""

from __future__ import annotations

import argparse
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
        # 任务表（TaskManager）与本地缓存都是**进内**单例：多 worker 时任务只存在于
        # 接到 POST 的那个进内，其他 worker 查 run_id 会 404；多进程各自整文件覆盖
        # 也会丢缓存更新。所以默认单 worker，需要扩容请改用外部队列/存储。
        print(
            "警告：--workers > 1 会把任务状态分散到多个进程（status/report 会随机 404，"
            "缓存互相覆盖），仅适用于你已把调度器换成共享存储的情况。已强制回退为 1 个 worker。",
            flush=True,
        )
        args.workers = 1

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
