"""包内共用 logger：名字固定为 `pm.llm`（拆包不改日志名，否则按名字过滤的排障手段全失效）。"""

from __future__ import annotations

import logging

logger = logging.getLogger("pm.llm")
