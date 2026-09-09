"""事实断言（ground-truth 指标）：用确定性校验替代 / 校准 LLM 评委。

为什么需要它（对齐竞品评审结论）：
本系统的评估信号原本只有 LLM 评委打分。评委可能被长输出说服（放水）、
可能同源同倾向，分数再高也只是"模型觉得好"。给用例附上**期望输出**后，
exact / contains / regex / 自定义函数这类确定性校验可以做到 LLM 做不到的事：
- 一票否决：事实不符时无论评委打多高都不判达标（veto）；
- 放水检测：评委给高分但断言失败 → 在报告里明确标记"评委分数与事实不符"。

断言是廉价、稳定、可复现的；LLM 评委负责断言覆盖不到的软性质量（文风、结构）。
两者组合 = "事实做底线，评委管上限"。

用法：
    # 程序化注册自定义断言
    from pm import assertions
    assertions.register_assertion("no_apology", lambda expected, out: "抱歉" not in out)
    # mode 传 "custom:no_apology"

    # CLI: --cases-file cases.json --assert-mode contains
    #   cases.json: [{"input": "...", "expected": "..."}]
    # API: POST /api/optimize {"task": ..., "test_cases": [{"input":..., "expected":...}]}
"""

from __future__ import annotations

import re
import threading
from collections.abc import Callable

from pydantic import BaseModel, Field

# 自定义断言注册表：name -> fn(expected, output) -> bool
_CUSTOM_ASSERTIONS: dict[str, Callable[[str, str], bool]] = {}
_REGISTRY_LOCK = threading.Lock()


def register_assertion(name: str, fn: Callable[[str, str], bool]) -> None:
    """注册自定义断言函数 fn(expected, output) -> bool。同名覆盖（便于测试）。"""
    if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]*", name):
        raise ValueError(f"断言名 {name!r} 非法（仅允许字母开头，字母/数字/下划线）")
    with _REGISTRY_LOCK:
        _CUSTOM_ASSERTIONS[name] = fn


def _get_custom(name: str) -> Callable[[str, str], bool] | None:
    with _REGISTRY_LOCK:
        return _CUSTOM_ASSERTIONS.get(name)


class AssertionResult(BaseModel):
    mode: str = Field(description="断言模式：exact / contains / regex / custom:<name>")
    passed: bool
    detail: str = Field(default="", description="失败原因或校验说明")
    score: float = Field(description="1.0 通过 / 0.0 失败")


def check_assertion(expected: str, output: str, mode: str) -> AssertionResult | None:
    """执行确定性断言。

    返回 None 表示"该用例没有可执行的断言"，调用方应跳过而不是当作失败。

    自定义断言（custom:*）不需要 expected：判定完全交由注册的断言函数；
    其余模式必须以非空 expected 为前提（exact/contains/regex 才有"比什么"的问题）。
    """
    out = output or ""

    if mode.startswith("custom:"):
        name = mode[len("custom:") :]
        fn = _get_custom(name)
        if fn is None:
            return AssertionResult(
                mode=mode, passed=False, detail=f"未注册的自定义断言：{name}", score=0.0
            )
        try:
            passed = bool(fn(expected or "", out))
        except Exception as e:  # noqa: BLE001 - 用户断言函数抛错不能炸图
            return AssertionResult(mode=mode, passed=False, detail=f"断言函数异常：{e}", score=0.0)
        return AssertionResult(
            mode=mode,
            passed=passed,
            detail="" if passed else "自定义断言未通过",
            score=1.0 if passed else 0.0,
        )

    exp = (expected or "").strip()
    if not exp:
        return None

    if mode == "exact":
        passed = out.strip() == exp
        return AssertionResult(
            mode=mode,
            passed=passed,
            detail="" if passed else f"期望与输出完全一致（strip 后），实际输出 {len(out)} 字符",
            score=1.0 if passed else 0.0,
        )

    if mode == "contains":
        passed = exp in out
        return AssertionResult(
            mode=mode,
            passed=passed,
            detail="" if passed else f"输出未包含期望片段（前 60 字：{exp[:60]}）",
            score=1.0 if passed else 0.0,
        )

    if mode == "regex":
        try:
            matched = re.search(exp, out, re.DOTALL) is not None
        except re.error as e:
            return AssertionResult(mode=mode, passed=False, detail=f"正则非法：{e}", score=0.0)
        return AssertionResult(
            mode=mode,
            passed=matched,
            detail="" if matched else f"正则未命中：{exp[:60]}",
            score=1.0 if matched else 0.0,
        )

    return AssertionResult(mode=mode, passed=False, detail=f"未知断言模式：{mode}", score=0.0)
