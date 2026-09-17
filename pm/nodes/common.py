"""节点公共设施：state 合并助手、代码围栏剥离、开关解析与 JSON 序列化。

（自原 pm/nodes.py「工具函数」段拆出，供全部节点族共用。）
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from ..state import State, merge_state, trace_event

logger = logging.getLogger("pm.nodes")


def _apply(
    state: State, node: str, patch: dict[str, Any], event: str, **payload: Any
) -> dict[str, Any]:
    """把节点产出与日志事件合并成一份返回值。"""
    log = trace_event(state, node, event, **payload)
    return merge_state(patch, log)


def _strip_code_fence(text: str) -> str:
    """模型常把提示词包在 ``` 里，去掉外层围栏但保留内容中的代码块。"""
    t = text.strip()
    if t.startswith("```") and t.endswith("```") and t.count("```") == 2:
        first_nl = t.find("\n")
        if first_nl != -1:
            return t[first_nl + 1 : t.rfind("```")].strip()
    return t


def _feature_enabled(key: str, default: bool = True) -> bool:
    """开关型环境变量：1/true/yes 为开，0/false/no 为关。"""
    raw = os.getenv(key, "1" if default else "0").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off", ""):
        return False
    logger.warning("%s=%r 不是可识别的开关值，按默认 %s 处理", key, raw, default)
    return default


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, default=str)
