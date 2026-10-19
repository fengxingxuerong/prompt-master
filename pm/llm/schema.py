"""结构化输出的形状修补：JSON 提取、schema 提示、字段修复、通道 A 能力探测。

2026-10-19 从 `pm/llm.py` 拆出。除 `_warn_if_token_budget_exhausted` 外不碰网络。
"""

from __future__ import annotations

import json
import re
import threading
import types
from typing import Any, Union, cast, get_args, get_origin

from pydantic import BaseModel

from ._log import logger
from .config import LLMConfig, _env, int_env


def extract_json_object(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    # 优先处理 ```json 围栏
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        try:
            return cast("dict[str, Any] | None", json.loads(fenced.group(1)))
        except json.JSONDecodeError:
            pass

    # 括号配平扫描，避免贪婪匹配截断嵌套对象
    start = text.find("{")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return cast("dict[str, Any] | None", json.loads(text[start : i + 1]))
                    except json.JSONDecodeError:
                        break
        start = text.find("{", start + 1)
    return None


def _schema_hint(model: type[BaseModel]) -> str:
    schema = model.model_json_schema()
    return json.dumps(schema, ensure_ascii=False, indent=2)


def _model_required_fields(model: type[BaseModel]) -> list[str]:
    """模型必须自己输出的顶层键（排除代码侧派生字段）。

    为什么要区分：`EvaluationResult` 里混着两类字段 ——
      ① 模型必须填的（`dimension_scores` / `model_reported_score` / `should_revise` …），
      ② 代码侧派生或采样式元信息（`weighted_score` / `judge` / `sample_*` / `cache_hit` …），
         它们有 `default`，**本就不该出现在提示里**。
    第 9 轮实测：旧的 `need` 把 12 个键全列（含 5~6 个派生字段），既噪声大又误导模型。
    """
    required: list[str] = []
    for name, fld in (getattr(model, "model_fields", None) or {}).items():
        if fld.is_required():
            required.append(name)
    # 声明顺序而非字母序：骨架是模型「照抄的样例」，其键序就是产出的先后顺序。
    # 排序会让评委骨架把 dimension_scores 顶到 issues 前面，等于用样例否定了
    # 「先取证、后打分」——EvaluationResult 的字段序是有意设计的。
    return required


def _unwrap_optional(anno: Any) -> Any:
    """解包 `X | None` / `Optional[X]` 的联合注解，取其中非 None 的那个。

    为什么需要：Pydantic 里 `X | None = Field(...)` 可以「必填但可空」，
    此时 `fld.annotation` 是 Union——`_schema_skeleton` / `_nested_field_names` /
    `_repair_shape` 三处只认裸 `BaseModel` 子类，会把可空嵌套字段当成普通标量
    （骨架里渲染成 `""`、形状修复不收拢、重试提示不点名），模型照抄后必挂。
    统一在这里解包，三处共用同一套语义。

    注意两种 Union 形态都要覆盖：`Optional[X]` 的 origin 是 `typing.Union`，
    而 PEP 604 语法 `X | None` 的 origin 是 `types.UnionType`（该对象**没有**
    `__origin__` 属性，只认 `typing.Union` 会漏掉它）。统一走
    `typing.get_origin` 判型最稳，Pydantic v2 的 annotation 正是 `X | None` 形态。
    """
    if get_origin(anno) not in (Union, types.UnionType):
        return anno
    args = [a for a in get_args(anno) if a is not type(None)]
    if len(args) == 1:
        return args[0]
    return anno


def _is_length_finish_error(e: Exception) -> bool:
    """是否因触达 max_tokens 而被截断（不是普通降级）。"""
    if type(e).__name__ == "LengthFinishReasonError":
        return True
    text = f"{type(e).__name__}: {e}".lower()
    return "length limit was reached" in text or ("max_tokens" in text and "reached" in text)


def _warn_if_token_budget_exhausted(role: str, e: Exception, cfg: Any) -> None:
    """额度耗尽时给出**可执行建议**：区分"推理吃掉预算"与"输出真的太长"。

    为什么必须单独诊断：思考型模型把 reasoning 计入 max_tokens，
    于是 completion_tokens 里绝大部分是 reasoning_tokens、正文几乎没有额度。
    这个失败伪装成普通的"结构化输出不可用"，在日志里与普通降级毫无区别 ——
    第 9 轮 13 次、第 11 轮 5 次都被这一行掩盖，直到专门查 usage 才暴露。

    对照组（第 11 轮，同模型/同端点/同任务/同样 15 次调用）：
        evaluator   max_tokens=6000 → 5 次额度耗尽（reasoning 5477~6000）
        evaluator_b max_tokens=8000 → 0 次（配置来自 .env）
    """
    if not _is_length_finish_error(e):
        return

    budget = getattr(cfg, "max_tokens", 0) or 0
    # langchain 的 LengthFinishReasonError 把 usage 写在 message 里；
    # 端点也可能把 usage 挂在 response 上，两种都取一遍。
    completion = _usage_int(e, "completion_tokens")
    reasoning = _usage_int(e, "reasoning_tokens")

    if completion and budget and completion >= budget * 0.98:
        if reasoning and reasoning >= completion * 0.8:
            logger.warning(
                "[%s] ⚠️ 额度被**推理**耗尽：reasoning_tokens=%d / completion_tokens=%d "
                "（max_tokens=%d）。思考型模型把推理计入预算，正文已无额度 → "
                "请调大 PM_%s_MAX_TOKENS（参考：第 11 轮对照，同条件下 6000 失败率 33%%，"
                "8000 为 0%%）",
                role,
                reasoning,
                completion,
                budget,
                role.upper(),
            )
        else:
            logger.warning(
                "[%s] ⚠️ 输出**本身**触达额度：completion_tokens=%d = max_tokens=%d"
                "（reasoning=%d）。要么输出确实很长，要么 %s 的输出契约需要收敛。",
                role,
                completion,
                budget,
                reasoning or 0,
                role,
            )
    else:
        logger.warning(
            "[%s] 触达 token 额度上限（max_tokens=%d, completion=%s, reasoning=%s）；"
            "若反复出现请调大 PM_%s_MAX_TOKENS",
            role,
            budget,
            completion or "?",
            reasoning or "?",
            role.upper(),
        )


def _usage_int(e: Exception, key: str) -> int | None:
    """从异常里尽力取出 usage 字段（不同端点挂的位置不同，取不到就 None）。"""
    for obj in (getattr(e, "completion", None), getattr(e, "llm_output", None), e):
        usage = getattr(obj, "usage", None) or getattr(obj, "token_usage", None)
        if usage is None and isinstance(obj, dict):
            usage = obj.get("usage") or obj.get("token_usage")
        if usage is None:
            continue
        for container in (usage, getattr(usage, "completion_tokens_details", None)):
            if container is None:
                continue
            val = (
                container.get(key) if isinstance(container, dict) else getattr(container, key, None)
            )
            if isinstance(val, int):
                return val
    text = str(e)
    for token in text.split(","):
        if f"{key}=" in token:
            digits = "".join(c for c in token.split("=")[-1] if c.isdigit())
            if digits:
                return int(digits)
    return None


def _repair_shape(model: type[BaseModel], data: dict[str, Any]) -> dict[str, Any]:
    """修"形状误解"类 schema 失败（不修内容错误）。

    已知真实形态（第 9 轮 3 次实测）：
        {"task_completion": 5, "format_adherence": 6, ..., "quality": 9}
    模型把嵌套字段 `dimension_scores` 的**子键平铺到了顶层**，于是
    `dimension_scores` / `model_reported_score` / `should_revise` 全部 missing。

    只做**有唯一确定答案**的修复：若某个嵌套模型的全部必填子键都平铺在顶层，
    就收拢成嵌套对象。歧义情况（部分子键、顶层已有同名字段）一律不动 ——
    这类"半形状"应该让模型重写，靠代码猜只会把错误埋得更深。
    """
    if not isinstance(data, dict):
        return data
    out = dict(data)
    for name, fld in (getattr(model, "model_fields", None) or {}).items():
        anno = _unwrap_optional(fld.annotation)
        if not (isinstance(anno, type) and issubclass(anno, BaseModel)):
            continue
        if name in out:
            continue
        inner_required = _model_required_fields(anno)
        if not inner_required:
            continue
        # 全部必填子键都在顶层平铺着 → 唯一确定的收拢
        if all(k in out for k in inner_required):
            nested: dict[str, Any] = {}
            for k in inner_required:
                nested[k] = out.pop(k)
            out[name] = nested
            logger.info("[schema] 形状修复：把平铺的子键收拢进嵌套字段 %s", name)
    return out


def _nested_field_names(model: type[BaseModel]) -> str:
    """列出模型里**必填的嵌套对象字段**（用于重试提示点名"别平铺"）。

    返回形如 "dimension_scores 的 task_completion/format_adherence/…"，
    没有嵌套必填字段时返回空串（此时不往提示里加噪声）。
    """
    parts: list[str] = []
    for name, fld in (getattr(model, "model_fields", None) or {}).items():
        if not fld.is_required():
            continue
        anno = _unwrap_optional(fld.annotation)
        if isinstance(anno, type) and issubclass(anno, BaseModel):
            kids = "/".join(_model_required_fields(anno))
            if kids:
                parts.append(f"{name} 的 {kids}")
    return "、".join(parts)


def _schema_skeleton(model: type[BaseModel], indent: int = 0) -> str:
    """生成**可照抄的 JSON 骨架**（只含必填键，给出占位值）。

    第 9 轮根因：模型把 `dimension_scores` 的 5 个维度**直接平铺到顶层**
    （顶层出现 task_completion/quality… 而 dimension_scores 缺失）→ 3 个字段 missing。
    这类"扁平 vs 嵌套"的形状误解，靠机器生成的 JSON Schema（$defs / $ref）表达不直观；
    给一个照抄实例是最直接的解药。故意不做通用递归渲染 —— 只展开一层嵌套对象，
    足够覆盖本项目的 schema，且不会引入难以维护的泛型逻辑。
    """
    pad = " " * indent
    child_pad = " " * (indent + 2)
    lines: list[str] = []
    for name in _model_required_fields(model):
        fld = model.model_fields[name]
        anno = _unwrap_optional(fld.annotation)
        origin = getattr(anno, "__origin__", None)
        if origin in (list, tuple):
            lines.append(f'{child_pad}"{name}": []')
        elif isinstance(anno, type) and issubclass(anno, BaseModel):
            inner = _schema_skeleton(anno, indent + 2)
            lines.append(f'{child_pad}"{name}": {inner}')
        elif anno is bool:
            lines.append(f'{child_pad}"{name}": false')
        elif anno is int or anno is float:
            lines.append(f'{child_pad}"{name}": 0')
        else:
            lines.append(f'{child_pad}"{name}": ""')
    body = ",\n".join(lines)
    return "{\n" + body + "\n" + pad + "}"


def _schema_hint_block(model: type[BaseModel]) -> str:
    """注入给模型的 schema 说明：**可照抄骨架**在前，完整 JSON Schema 在后。

    顺序有意为之：模型对"照抄示例"的遵循度显著高于对"读 $defs/$ref"的遵循度，
    完整 schema 留给它查细节（约束范围如 1~10）。
    """
    skeleton = _schema_skeleton(model)
    return (
        "\n\n<json_schema>\n"
        "你必须严格输出一个符合以下 JSON Schema 的 JSON 对象，且只输出该 JSON 对象，"
        "不要任何解释、不要 Markdown 围栏：\n"
        "先按下面的骨架把字段填满（键名必须完全一致，尤其注意嵌套对象**不要平铺到顶层**）：\n"
        f"{skeleton}\n\n"
        "完整 JSON Schema（用于查约束范围）：\n"
        f"{_schema_hint(model)}\n"
        "</json_schema>"
    )


class _ChannelASkipped(RuntimeError):
    """主动跳过原生结构化输出通道（不是故障，INFO 级留痕，不走告警日志）。"""


_UNSUPPORTED_A: set[str] = set()


_UNSUPPORTED_A_LOCK = threading.Lock()


def _capability_key(cfg: LLMConfig) -> str:
    return f"{cfg.provider}|{cfg.base_url}|{cfg.model}"


STRUCTURED_METHODS = ("", "json_schema", "function_calling")


def _structured_method() -> str:
    """读取 PM_STRUCT_METHOD（容错：非法值告警并忽略，保持默认行为）。"""
    raw = _env("PM_STRUCT_METHOD").lower()
    if raw not in STRUCTURED_METHODS:
        logger.warning(
            "PM_STRUCT_METHOD=%r 无效（可选：json_schema / function_calling），忽略并使用默认方法",
            raw,
        )
        return ""
    return raw


def _skip_channel_a(cfg: LLMConfig) -> str | None:
    """该端点是否应直接走文本通道。返回跳过原因（None = 照常尝试）。"""
    if int_env("PM_FORCE_JSON_CHANNEL", 0) > 0:
        return "PM_FORCE_JSON_CHANNEL=1 已强制使用文本通道"
    key = _capability_key(cfg)
    with _UNSUPPORTED_A_LOCK:
        if key in _UNSUPPORTED_A:
            return f"端点 {key} 此前已报不兼容结构化输出，本次直接降级"
    return None


def _remember_channel_a_unsupported(cfg: LLMConfig, err: str) -> None:
    """记住「这个端点不支持结构化输出」，避免每次调用先白打一个 400 请求（M7）。

    只记确定性的不兼容错语；网络抖动 / 限流 / 5xx 不能记，
    否则一次偶发错误会把原生通道永久关掉。
    """
    low = err.lower()
    if not any(
        k in low for k in ("does not support", "not supported", "unsupported", "json_schema")
    ):
        return
    if not any(k in low for k in ("400", "invalid_request", "bad request")):
        return
    key = _capability_key(cfg)
    with _UNSUPPORTED_A_LOCK:
        fresh = key not in _UNSUPPORTED_A
        if fresh:
            _UNSUPPORTED_A.add(key)
    if fresh:
        logger.info("已记住端点 %s 不支持原生结构化输出，后续调用直接走文本通道", key)
