#!/usr/bin/env python3
"""ModelHub 网关启动脚本。

用法：
    python run_modelhub.py                  # 默认 127.0.0.1:8687
    python run_modelhub.py --port 9000
    python run_modelhub.py --host 0.0.0.0   # 局域网访问（建议同时设 PMH_GATEWAY_TOKEN）

启动后：
    http://127.0.0.1:8687/v1/models          模型池清单
    http://127.0.0.1:8687/v1/pool/status     断路器/冷却状态
    http://127.0.0.1:8687/api/health         健康检查
    http://127.0.0.1:8687/docs               交互式 API 文档

智能体接入（OpenAI 兼容）：
    base_url = http://127.0.0.1:8687/v1
    api_key  = PMH_GATEWAY_TOKEN 的值（未设则任意）
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from pm.bootstrap import ensure_utf8_stdio

ensure_utf8_stdio()


def main() -> None:
    import uvicorn

    ap = argparse.ArgumentParser(description="ModelHub 统一模型池网关")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8687)
    ap.add_argument("--reload", action="store_true", help="开发模式热重载")
    args = ap.parse_args()

    uvicorn.run("pm.modelhub.server:app", host=args.host, port=args.port, reload=args.reload, log_level="info")


if __name__ == "__main__":
    main()
