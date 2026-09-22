"""ModelHub：统一模型池网关。

把项目接进的商汤 SenseNova Token Plan 端点（主）与 AMD Radeon 开发者端点（兜底）
统一为一个 OpenAI 兼容的模型池：

- 12 个模型全部注册进池，按 priority 顺序作为主备链；
- 任一模型报错 / 超时 / 限流 / 空返回时自动切换下一个可用模型（断路器冷却自愈）；
- 角色设定（系统提示词）固定在 config/roles.json，不随模型切换漂移；
- 每次调用与切换都写 JSONL 台账（logs/modelhub_ledger.jsonl），可按条件检索；
- 任意智能体（LangChain / openai SDK / 原生 HTTP）按同一 OpenAI 兼容接口接入。
"""

from .ledger import ledger_stats, query_ledger
from .pool import (
    ConfigError,
    GatewayError,
    ModelEntry,
    ModelHub,
    ModelPoolExhaustedError,
)

__all__ = [
    "ConfigError",
    "GatewayError",
    "ModelEntry",
    "ModelHub",
    "ModelPoolExhaustedError",
    "ledger_stats",
    "query_ledger",
]
