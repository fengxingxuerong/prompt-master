"""Key 覆盖判定（CLI 闸门与 doctor 共用一份，2026-10-23）。

只读环境变量，不构造客户端、不出网。三条规则：
1. 全局 `PM_API_KEY` 或 `PM_API_KEYS` 多 Key 池覆盖所有角色；
2. 否则每个角色必须有自己的 `PM_<ROLE>_API_KEY`；
3. 占位符（`sk-xxxx` / `your-key` / `changeme` …）按"没配"看待 —— 它长得像 Key，
   真跑起来只会得到 401，比空值更难排查。
"""

from __future__ import annotations

import os
from typing import Any

KEY_ROLES: tuple[str, ...] = (
    "clarifier",
    "optimizer",
    "mockgen",
    "evaluator",
    "evaluator_b",
    "arbiter",
    "reviser",
    "comparator",
    "target",
)

_ENV_PREFIX = "PM_"
_ENV_SUFFIX = "_API_KEY"
_PLACEHOLDER_MARKERS = (
    "xxxx",
    "your",
    "changeme",
    "placeholder",
    "replace-me",
    "replace_me",
    "sk-...",
    "<key>",
)


def is_placeholder_key(value: str | None) -> bool:
    """占位符不是 Key：返回 True 时调用方应按"缺失/需替换"处理。"""
    raw = (value or "").strip()
    if not raw:
        return False
    low = raw.lower()
    return any(marker in low for marker in _PLACEHOLDER_MARKERS)


def _role_env(role: str) -> str:
    return f"{_ENV_PREFIX}{role.upper()}{_ENV_SUFFIX}"


def _nonempty(value: str | None) -> bool:
    return bool((value or "").strip())


def key_status() -> dict[str, Any]:
    """当前进程的 Key 覆盖情况（只给布尔/数量/角色名与尾号，不回显完整 Key）。"""
    global_key = (os.getenv("PM_API_KEY") or "").strip()
    pool = [k.strip() for k in (os.getenv("PM_API_KEYS") or "").split(",") if k.strip()]
    role_keys = {
        role: (os.getenv(_role_env(role)) or "").strip()
        for role in KEY_ROLES
        if _nonempty(os.getenv(_role_env(role)))
    }
    placeholders = [
        name
        for name, value in (
            [("PM_API_KEY", global_key)]
            + [(f"PM_{role.upper()}_API_KEY", v) for role, v in role_keys.items()]
        )
        if is_placeholder_key(value)
    ]
    # 占位符不算可用 Key
    global_ok = _nonempty(global_key) and not is_placeholder_key(global_key)
    pool_ok = any(not is_placeholder_key(k) for k in pool)
    if global_ok or pool_ok:
        missing: list[str] = []
    else:
        missing = [
            role
            for role in KEY_ROLES
            if not (_nonempty(role_keys.get(role)) and not is_placeholder_key(role_keys.get(role)))
        ]
    return {
        "global": global_ok,
        "pool_count": len([k for k in pool if not is_placeholder_key(k)]),
        "role_keys": sorted(role_keys),
        "missing_roles": missing,
        "placeholders": placeholders,
    }


def missing_key_roles() -> list[str]:
    """还缺 Key 的角色；空列表 = 全局/多 Key 池覆盖，或每个角色都有独立有效 Key。"""
    return list(key_status()["missing_roles"])


def mask_key(value: str | None) -> str:
    """只给尾号：诊断输出里不允许出现完整 Key。"""
    raw = (value or "").strip()
    if not raw:
        return "(无)"
    return "…" + raw[-4:] if len(raw) > 4 else "(已设置)"


def placeholder_label(value: str | None) -> str:
    """占位符的展示名；正常 Key 返回空串。刻意不回显原文，避免误伤形似的真 Key。"""
    return "(疑似占位符)" if is_placeholder_key(value) else ""


__all__ = [
    "KEY_ROLES",
    "is_placeholder_key",
    "key_status",
    "mask_key",
    "missing_key_roles",
    "placeholder_label",
]
