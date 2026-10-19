"""对外两个调用口：`structured_call`（结构化）与 `plain_call`（纯文本）。

2026-10-19 从 `pm/llm.py` 拆出。这是本包的边界层 —— 重试/配置/账本都在下面。
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any, TypeVar, cast

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, ValidationError

from .. import backend
from ._log import logger
from .budget import (
    CallBudgetExceeded,
    WallBudget,
    _is_rate_limit_error,
    _is_transient_conn_error,
)
from .config import build_config
from .ledger import _record_failure, _tokens_of
from .schema import (
    _ChannelASkipped,
    _model_required_fields,
    _nested_field_names,
    _remember_channel_a_unsupported,
    _repair_shape,
    _schema_hint_block,
    _skip_channel_a,
    _structured_method,
    _warn_if_token_budget_exhausted,
    extract_json_object,
)

T = TypeVar("T", bound=BaseModel)


def structured_call(
    role: str,
    model_cls: type[T],
    system: str,
    user: str,
    max_retries: int = 3,
    overrides: dict[str, Any] | None = None,
) -> tuple[T, dict[str, Any]]:
    """调用 LLM 并强制拿到 model_cls 实例。

    通道 A：provider 原生 structured output
    通道 B：Schema 注入 + 文本解析（自动降级）

    返回 (实例, 调用元信息)。失败抛 RuntimeError。
    """
    # 从包命名空间取：测试用 monkeypatch 覆盖这些开关（见 __init__.py 的兼容说明）。
    # 顶层 import 会把值焊死在本模块，包上的补丁静默失效。
    from . import (
        CALL_BUDGET,
        RATE_LIMIT_TOTAL_WAIT,
        TEMPERATURE_BOOST_ON_PARSE,
        TEMPERATURE_DECAY,
        _invoke_with_rate_limit_retry,
    )

    # 演示模式 / 自测注入的钩子：只影响当前线程或当前异步任务，不改动模块属性（C4）
    hook = backend.current()
    if hook is not None and hook.structured is not None:
        # hook 来自 `backend.current()`，其类型在 follow_imports=skip 下是 Any；
        # 这里显式收窄，别把 Any 直接当返回值（mypy strict 的 no-any-return）。
        return cast(
            "tuple[T, dict[str, Any]]",
            hook.structured(
                role, model_cls, system, user, max_retries=max_retries, overrides=overrides
            ),
        )

    cfg = build_config(role, overrides)
    # 真实网络请求计数（与 attempts 语义不同：attempts 是通道 B 的解析尝试次数，
    # 这里记录所有实际打到端点的请求，含通道 A 调用、429 退避重试、Key 轮换与连接重试）
    requests_counter: list[int] = [0]
    meta: dict[str, Any] = {
        "role": role,
        "model": cfg.model,
        "channel": None,
        "attempts": 0,
        "requests": 0,
        "latency_ms": None,
        "temperature": cfg.temperature,
    }

    t0 = time.time()
    last_err: str | None = None

    # ③ 解耦：整个 structured_call（通道 A + 通道 B 的全部解析重试）共享一个限流总预算。
    # 旧版 B 的每次解析重试各自从满额 MAX_WAIT 退避（最坏 4×60s），且限流上抛会被
    # except 大兜底当成"解析失败"消耗一次重试名额。现在解析重试次数（质量层）与
    # 限流等待（传输层）独立计数，总阻塞时长有硬上限。
    limit_deadline = time.monotonic() + max(0.0, RATE_LIMIT_TOTAL_WAIT)
    # 墙钟预算（`PM_CALL_BUDGET`）：那条 `limit_deadline` 只约束"还要不要睡这一觉"，
    # 一次挂住不返回的调用它管不到。这里才是"一次调用最多占多久"的那条线。
    budget = WallBudget(role, CALL_BUDGET) if CALL_BUDGET > 0 else None

    # ---- 通道 A ----
    # method 只有在显式配置时才传：不同 provider 的 with_structured_output
    # 签名不一致（如 ChatAnthropic 没有 method 形参），默认路径不能多传参。
    method = _structured_method()
    channel_a = "function_calling" if method == "function_calling" else "structured_output"
    so_kwargs: dict[str, Any] = {"include_raw": False}
    if method:
        so_kwargs["method"] = method
    try:
        skip_reason = _skip_channel_a(cfg)
        if skip_reason:
            raise _ChannelASkipped(skip_reason)
        result = _invoke_with_rate_limit_retry(
            role,
            lambda llm: llm.with_structured_output(model_cls, **so_kwargs).invoke(
                [SystemMessage(content=system), HumanMessage(content=user)]
            ),
            overrides,
            deadline=limit_deadline,
            counter=requests_counter,
            budget=budget,
        )
        if isinstance(result, model_cls):
            meta.update(
                channel=channel_a,
                attempts=1,
                requests=requests_counter[0],
                latency_ms=int((time.time() - t0) * 1000),
            )
            return result, meta
        if isinstance(result, dict):
            validated = model_cls.model_validate(result)
            meta.update(
                channel=channel_a,
                attempts=1,
                requests=requests_counter[0],
                latency_ms=int((time.time() - t0) * 1000),
            )
            return validated, meta
        # 端点"支持"结构化输出却返回了意外类型（常见：None / 空内容）。
        # 必须显式记录再降级，否则故障原因会在日志里彻底消失。
        last_err = (
            f"原生结构化输出返回非预期类型: {type(result).__name__}"
            f"（期望 {model_cls.__name__} 或 dict）"
        )
        logger.warning("[%s] %s，降级为 JSON 文本解析", role, last_err)
    except _ChannelASkipped as e:
        last_err = f"跳过原生结构化输出：{e}"
        # INFO 而非 DEBUG：跳过不打 WARNING 也不打 INFO 的话，"为什么没走原生通道"
        # 在默认日志里完全不可见 —— e2e 三场景 FAIL 时定位主要耗在这条隐形路径上（2026-10-12）
        logger.info("[%s] %s", role, last_err)
    except CallBudgetExceeded:
        # 预算用尽不是"这个通道不支持"，不能记进能力缓存（那会让后续调用永久跳过通道 A），
        # 也不必再进通道 B——同一个预算已经见底了。直接上抛，保留现场。
        raise
    except Exception as e:  # noqa: BLE001
        last_err = f"{type(e).__name__}: {e}"
        logger.warning("[%s] 原生结构化输出不可用，降级为 JSON 文本解析：%s", role, last_err)
        # 2026-09-14：LengthFinishReasonError（额度耗尽）必须**单独诊断**，不能只当普通降级。
        # 它被当成噪声瞒了 9 轮 —— 第 9 轮 13 次、第 11 轮 5 次都躺在同一行警告里，
        # 直到把「25% 成功率」当作谜题才挖出来。真正的成因是思考型模型把 reasoning
        # 全部计入 max_tokens，正文额度归零。这里按 usage 给出可直接执行的建议。
        _warn_if_token_budget_exhausted(role, e, cfg)
        _remember_channel_a_unsupported(cfg, last_err)

    # ---- 通道 B：注入 Schema + 解析重试 ----
    schema_block = _schema_hint_block(model_cls)
    temp = cfg.temperature

    for attempt in range(1, max_retries + 1):
        if budget is not None and budget.remaining() <= 0:
            # 名额还剩、预算没了 ⇒ 停。旧版这里会把剩下的解析重试名额各再烧一个 timeout
            raise budget.exceeded(requests_counter[0], last_err or "通道 A 未拿到结果")
        retry_hint = ""
        if last_err:
            retry_hint = (
                f"\n\n<previous_error>\n上一次输出解析失败：{last_err}\n"
                "请修正后重新输出，只输出 JSON 对象本身。\n</previous_error>"
            )
        messages = [
            SystemMessage(content=system + schema_block),
            HumanMessage(content=user + retry_hint),
        ]
        try:
            # 闭包按默认参数绑定当前轮的 temp/messages，避免 B023 循环变量捕获
            resp = _invoke_with_rate_limit_retry(
                role,
                # mypy 无法对带默认参数的 lambda 完成推断，显式 cast 到目标签名
                cast(
                    "Callable[[Any], Any]",
                    lambda llm, _t=temp, _msgs=messages: llm.with_config(
                        temperature=max(_t, 0.0)
                    ).invoke(_msgs),
                ),
                overrides,
                deadline=limit_deadline,
                counter=requests_counter,
                budget=budget,
            )
            raw = resp.content if isinstance(resp.content, str) else str(resp.content)
            data = extract_json_object(raw)
            if data is None:
                # 只报形状事实，不回显模型文本：这段错误信息会被拼进下一次重试的
                # user 段（`<previous_error>`），把被测内容原样搬回去等于自增注入面。
                # 而"没有 JSON"有三种截然不同的成因，不区分就会让人去改提示词：
                stripped = (raw or "").strip()
                if not stripped:
                    why = "空内容——通常是 reasoning 耗尽 max_tokens（调大 PM_<ROLE>_MAX_TOKENS）"
                elif not stripped.startswith("{") and stripped.count("{") == 0:
                    why = f"输出是散文/解释，完全没有 JSON（{len(stripped)} 字符）"
                elif stripped.count("{") > stripped.count("}"):
                    why = f"JSON 未闭合，疑似被 max_tokens 截断（{len(stripped)} 字符）"
                else:
                    why = f"有花括号但解析失败（{len(stripped)} 字符，可能是围栏/前后缀噪声）"
                raise ValueError(f"未在输出中找到合法 JSON 对象：{why}")
            # 先修"形状误解"（平铺 → 嵌套），再校验；修不了就照旧抛 ValidationError
            data = _repair_shape(model_cls, data)
            validated = model_cls.model_validate(data)
            meta.update(
                channel="json_fallback",
                attempts=attempt,
                requests=requests_counter[0],
                latency_ms=int((time.time() - t0) * 1000),
            )
            return validated, meta
        except (ValidationError, ValueError, json.JSONDecodeError) as e:
            # ①把缺哪些**必填**顶层键写进下一次提示 —— 与温度无关，保留。
            #   只列必填键：把 judge/sample_*/cache_hit 这类代码侧派生字段列进去
            #   既噪声大，还会诱导模型去编造它们（第 9 轮实测的 12 键提示）。
            # ①b 若上次是"平铺子键"形状误解，额外点明嵌套要求（比泛泛列键有效得多）。
            # ②温度：默认沿用降温。升温已被对照实验证伪（见 TEMPERATURE_BOOST_ON_PARSE 注释）。
            need = ", ".join(_model_required_fields(model_cls))
            last_err = f"{type(e).__name__}: {e}"
            if need:
                last_err += f"。输出必须包含这些顶层键（缺一不可）：{need}"
            nested_hint = _nested_field_names(model_cls)
            if nested_hint:
                last_err += (
                    f"。注意 {nested_hint} 是**嵌套对象**，"
                    '必须写成 {"' + nested_hint.split(" 的 ")[0] + '": {...}} 的形式，'
                    "不要把它的子键平铺到顶层"
                )
            logger.warning("[%s] 第 %d/%d 次解析失败：%s", role, attempt, max_retries, last_err)
            temp = max(0.0, min(1.0, temp - TEMPERATURE_DECAY + TEMPERATURE_BOOST_ON_PARSE))
        except Exception as e:
            # ③ 解耦：限流与解析失败是两类问题。限流在 _invoke_with_rate_limit_retry
            # 内部消化（退避 + Key 轮换，共享 deadline 预算）；总预算耗尽上抛到这里时，
            # 继续循环只会立刻再 429 —— 立即终止并上抛，**不消耗解析重试名额**，
            # 也不把「RateLimitError」写进 retry_hint 去误导下一次提示。
            if _is_rate_limit_error(e):
                raise
            if isinstance(e, CallBudgetExceeded):
                raise
            if _is_transient_conn_error(e):
                # 传输层已经在我方重试里试过满 (1+PM_CONN_RETRIES) 发，解析重试名额救不了网络。
                # 预算同时见底时报预算（那条更可执行），否则原样上抛——旧版这里是
                # "再烧两次、每次再吃满一整条重试链"，那是 45 分钟的另一半来源。
                if budget is not None and budget.expired():
                    raise budget.exceeded(requests_counter[0], f"{type(e).__name__}: {e}") from e
                raise
            last_err = f"{type(e).__name__}: {e}"
            logger.warning("[%s] 第 %d/%d 次调用异常：%s", role, attempt, max_retries, last_err)
            # 端点/网络类异常沿用降温：这类失败与采样随机性无关，保守重试即可
            temp = max(0.0, temp - TEMPERATURE_DECAY)

    meta.update(
        channel="failed",
        attempts=max_retries,
        requests=requests_counter[0],
        latency_ms=int((time.time() - t0) * 1000),
    )
    # §三十·十八：解析重试耗尽也是"没拿到结果"，也要留痕。
    # 这条路径正是历史上 `结构化输出失败…空内容` 那些 errors 的出处，
    # 以前在台账上完全看不到（§三十·十三·三 记的就是这个缺口）。
    _record_failure(role, int((time.time() - t0) * 1000))
    raise RuntimeError(f"[{role}] 结构化输出失败，已重试 {max_retries} 次。最后错误：{last_err}")


def plain_call(
    role: str, system: str, user: str, overrides: dict[str, Any] | None = None
) -> tuple[str, dict[str, Any]]:
    """纯文本调用，用于 Optimizer / Reviser / 被测目标模型。"""
    # 同 structured_call：从包命名空间取，保住 pm.llm.* 的 monkeypatch 面。
    from . import CALL_BUDGET, _invoke_with_rate_limit_retry

    hook = backend.current()
    if hook is not None and hook.plain is not None:
        # 显式标注而不是 cast：全仓 mypy 认为它冗余，单查本包时 hook 字段又是 Any
        result: tuple[str, dict[str, Any]] = hook.plain(role, system, user, overrides=overrides)
        return result

    cfg = build_config(role, overrides)
    requests_counter: list[int] = [0]
    t0 = time.time()
    budget = WallBudget(role, CALL_BUDGET) if CALL_BUDGET > 0 else None
    resp = _invoke_with_rate_limit_retry(
        role,
        lambda llm: llm.invoke([SystemMessage(content=system), HumanMessage(content=user)]),
        overrides,
        counter=requests_counter,
        budget=budget,
    )
    text = resp.content if isinstance(resp.content, str) else str(resp.content)
    in_tok, out_tok = _tokens_of(resp)
    meta = {
        "role": role,
        "model": cfg.model,
        "channel": "plain",
        "attempts": 1,
        "requests": requests_counter[0],
        "latency_ms": int((time.time() - t0) * 1000),
        "temperature": cfg.temperature,
        # 下面三个是"空产出到底有没有额度证据"的唯一依据。2026-10-15 真端点一轮
        # （`5253290b3fb5`）修订器 3 次调用、每次 `output_tokens` 恰好等于
        # `PM_REVISER_MAX_TOKENS=24000` 却返回空正文 —— 而节点只能写"未观测到截断/额度证据"，
        # 因为 meta 里从来没带过这两个数。带上了，归因才有出处（见 §三十·十九·六）。
        "max_tokens": cfg.max_tokens,
        "input_tokens": in_tok,
        "output_tokens": out_tok,
    }
    return text, meta
