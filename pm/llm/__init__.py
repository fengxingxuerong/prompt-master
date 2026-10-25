"""LLM 客户端工厂 + 鲁棒的结构化输出调用。

设计要点：
1. **双通道结构化输出**：优先用 provider 原生 structured output（function calling / JSON schema）；
   一旦不可用（很多 OpenAI 兼容端点、国产模型不支持），自动降级为
   "注入 JSON Schema + 文本解析 + 校验重试"，保证流程不中断。
2. **格式错误重试**：解析失败自动重试，每次降温并附带上一次的错误信息（原文档提到的策略，这里落地）。
3. **按节点调温**：不同节点用不同 temperature，见 TEMPERATURES。
4. **全链路可观测**：每次调用记录 model / temperature / latency / tokens / 是否降级。

拆包后本文件只做重导出；实现按 `ledger / config / budget / retry / schema / calls` 分层。
"""

# 2026-10-19：1579 行单文件拆成包。这里重导出**全部顶层符号（含私有）**，
# 因为外部一律用 `from .. import llm` 后 `llm.plain_call(...)` 的属性访问，
# 且测试用 `monkeypatch.setattr("pm.llm.X", ...)` 做隔离 —— 少导出任何一个都是静默断裂。

import time as time  # 兼容 `pm.llm.time`（测试用它把 sleep / monotonic 换成假实现）

from ._log import (
    logger as logger,
)
from .budget import (
    _BUDGET_MIN_REMAINING as _BUDGET_MIN_REMAINING,
)
from .budget import (
    _DNS_RESOLVED as _DNS_RESOLVED,
)
from .budget import (
    _DNS_RESOLVED_LOCK as _DNS_RESOLVED_LOCK,
)
from .budget import (
    _TRANSIENT_CONN_HINTS as _TRANSIENT_CONN_HINTS,
)
from .budget import (
    _TRANSIENT_STATUS_CODES as _TRANSIENT_STATUS_CODES,
)
from .budget import (
    CallBudgetExceeded as CallBudgetExceeded,
)
from .budget import (
    DnsResolutionTimeout as DnsResolutionTimeout,
)
from .budget import (
    WallBudget as WallBudget,
)
from .budget import (
    _endpoint_host as _endpoint_host,
)
from .budget import (
    _is_rate_limit_error as _is_rate_limit_error,
)
from .budget import (
    _is_transient_conn_error as _is_transient_conn_error,
)
from .budget import (
    _preflight_dns as _preflight_dns,
)
from .budget import (
    _resolve_into_box as _resolve_into_box,
)
from .budget import (
    resolve_host_bounded as resolve_host_bounded,
)
from .calls import (
    T as T,
)
from .calls import (
    plain_call as plain_call,
)
from .calls import (
    structured_call as structured_call,
)
from .config import (
    _JUDGE_B_SAME_WARN_LOCK as _JUDGE_B_SAME_WARN_LOCK,
)
from .config import (
    _JUDGE_B_SAME_WARNED as _JUDGE_B_SAME_WARNED,
)
from .config import (
    _KEY_CURSOR as _KEY_CURSOR,
)
from .config import (
    _KEY_LOCK as _KEY_LOCK,
)
from .config import (
    _KEY_POOL as _KEY_POOL,
)
from .config import (
    CALL_BUDGET as CALL_BUDGET,
)
from .config import (
    DNS_BUDGET as DNS_BUDGET,
)
from .config import (
    MAX_TOKENS as MAX_TOKENS,
)
from .config import (
    RATE_LIMIT_BASE_SLEEP as RATE_LIMIT_BASE_SLEEP,
)
from .config import (
    RATE_LIMIT_MAX_WAIT as RATE_LIMIT_MAX_WAIT,
)
from .config import (
    RATE_LIMIT_RETRIES as RATE_LIMIT_RETRIES,
)
from .config import (
    RATE_LIMIT_TOTAL_WAIT as RATE_LIMIT_TOTAL_WAIT,
)
from .config import (
    SDK_RETRIES as SDK_RETRIES,
)
from .config import (
    TEMPERATURE_BOOST_ON_PARSE as TEMPERATURE_BOOST_ON_PARSE,
)
from .config import (
    TEMPERATURE_DECAY as TEMPERATURE_DECAY,
)
from .config import (
    TEMPERATURES as TEMPERATURES,
)
from .config import (
    TRANSIENT_CONN_BASE_SLEEP as TRANSIENT_CONN_BASE_SLEEP,
)
from .config import (
    TRANSIENT_CONN_RETRIES as TRANSIENT_CONN_RETRIES,
)
from .config import (
    LLMConfig as LLMConfig,
)
from .config import (
    _decorrelate_judge_b as _decorrelate_judge_b,
)
from .config import (
    _env as _env,
)
from .config import (
    _float_env as _float_env,
)
from .config import (
    _int_env as _int_env,
)
from .config import (
    _key_pool as _key_pool,
)
from .config import (
    _next_key as _next_key,
)
from .config import (
    build_config as build_config,
)
from .config import (
    call_fingerprint as call_fingerprint,
)
from .config import (
    get_llm as get_llm,
)
from .config import (
    int_env as int_env,
)
from .ledger import (
    _LEDGER_VAR as _LEDGER_VAR,
)
from .ledger import (
    UsageLedger as UsageLedger,
)
from .ledger import (
    _record_failure as _record_failure,
)
from .ledger import (
    _record_retry as _record_retry,
)
from .ledger import (
    _record_usage as _record_usage,
)
from .ledger import (
    _tokens_of as _tokens_of,
)
from .ledger import (
    current_ledger as current_ledger,
)
from .ledger import (
    usage_scope as usage_scope,
)
from .retry import (
    _invoke_with_conn_retry as _invoke_with_conn_retry,
)
from .retry import (
    _invoke_with_rate_limit_retry as _invoke_with_rate_limit_retry,
)
from .retry import (
    _llm_within_budget as _llm_within_budget,
)
from .schema import (
    _UNSUPPORTED_A as _UNSUPPORTED_A,
)
from .schema import (
    _UNSUPPORTED_A_LOCK as _UNSUPPORTED_A_LOCK,
)
from .schema import (
    STRUCTURED_METHODS as STRUCTURED_METHODS,
)
from .schema import (
    _capability_key as _capability_key,
)
from .schema import (
    _ChannelASkipped as _ChannelASkipped,
)
from .schema import (
    _is_length_finish_error as _is_length_finish_error,
)
from .schema import (
    _model_required_fields as _model_required_fields,
)
from .schema import (
    _nested_field_names as _nested_field_names,
)
from .schema import (
    _remember_channel_a_unsupported as _remember_channel_a_unsupported,
)
from .schema import (
    _repair_shape as _repair_shape,
)
from .schema import (
    _schema_hint as _schema_hint,
)
from .schema import (
    _schema_hint_block as _schema_hint_block,
)
from .schema import (
    _schema_skeleton as _schema_skeleton,
)
from .schema import (
    _skip_channel_a as _skip_channel_a,
)
from .schema import (
    _structured_method as _structured_method,
)
from .schema import (
    _unwrap_optional as _unwrap_optional,
)
from .schema import (
    _usage_int as _usage_int,
)
from .schema import (
    _warn_if_token_budget_exhausted as _warn_if_token_budget_exhausted,
)
from .schema import (
    extract_json_object as extract_json_object,
)
