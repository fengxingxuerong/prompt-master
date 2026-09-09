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

可调环境变量：
    PM_ASSERT_NORMALIZE=1      contains 忽略大小写、全/半角（NFKC）与空白差异
    PM_ASSERT_REGEX_TIMEOUT=2  正则匹配的时间预算（秒）：有“量词包住分组”这种回溯风险的
                               模式会在子进程里跑，超预算直接杀掉并按未通过处理
"""

from __future__ import annotations

import os
import re
import threading
import unicodedata
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, Field

# 自定义断言注册表：name -> fn(expected, output) -> bool
_CUSTOM_ASSERTIONS: dict[str, Callable[[str, str], bool]] = {}
_REGISTRY_LOCK = threading.Lock()

# 送给反馈/报告的文本一律先压成单行再截断：报告是 Markdown 表格，
# 一个换行就能把整行结构弄散；反馈太长则会挤掉真正有用的问题清单。
_EXPECTED_CLIP = 600
_OUTPUT_CLIP = 240
# 正则最多扫描这么长：断言跑在评估主链路上，不该被 10 万字的输出拖住
_MAX_SCANNED = 20000
# 嵌套量词（(a+)+ / (a|b)* 这类）是灾难性回溯的典型形状。命中即拒绝执行，
# 而不是"跑起来再祈祷"：模式串可以来自 API 请求体，等于把 CPU 交给调用方。
_NESTED_QUANTIFIER = re.compile(r"\((?:[^()\\]|\\.)*[+*][^()]*\)\s*[+*]")


def _flag(name: str, default: bool = True) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _seconds(name: str, default: float) -> float:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        val = float(raw)
    except ValueError:
        return default
    return val if val > 0 else default


def _clip(text: str, limit: int) -> str:
    flat = " ⏎ ".join((text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _fold(text: str) -> str:
    """NFKC 归一 + casefold + 去空白：contains 用的模糊比较。

    为什么连空白也去：全角转半角后“：”后面的窄空格会一并消失（“ＩＤ：４２” vs “ID: 42”），
    不归一空白等于“有排版差异就判死”，在 veto 语义下是误杀。
    需要逐字比对（含空白与标点）请用 exact。
    """
    folded = unicodedata.normalize("NFKC", text or "").casefold()
    return re.sub(r"\s+", "", folded)


# 组被量词包住 = 有回溯爆炸的余地。守卫只拦最典型的嵌套量词，其余这类模式走子进程隔离。
_QUANTIFIED_GROUP = re.compile(r"\((?:[^()\\]|\\.)*\)\s*[+*]")


def _child_match(pattern: str, text: str, q: Any) -> None:
    """子进程入口（必须是模块级函数，spawn 要能 import 到）。"""
    try:
        q.put((True, re.search(pattern, text, re.DOTALL) is not None, ""))
    except re.error as exc:
        q.put((False, False, f"正则非法：{exc}"))
    except Exception as exc:  # noqa: BLE001 - 子进程里的异常要变成确定结论
        q.put((False, False, f"匹配执行失败：{exc}"))


def _match_isolated(pattern: str, text: str, timeout: float) -> tuple[str, bool, str]:
    """在可杀的子进程里跑一次匹配，返回 (status, matched, error)；status ∈ ok/timeout/error。

    为什么不是线程：`re` 在 C 层匹配期间不释放 GIL，超时主线程根本抢不到运行机会
    （实测 22ms 的匹配把 `Event.wait(0.001)` 也拖到 22ms 才返回）。线程只能“等它跑完”，
    进程才能 `terminate()`。这直接对应一个真实入口：`/api/optimize` 的请求体可以带 regex 模式。
    """
    import multiprocessing as mp

    ctx: Any
    try:
        ctx = mp.get_context("spawn")
    except ValueError:  # 平台不提供 spawn
        ctx = mp.get_context()
    try:
        q = ctx.Queue()
        proc = ctx.Process(target=_child_match, args=(pattern, text, q), daemon=True)
        proc.start()
    except Exception:  # noqa: BLE001 - 起不了进程就退回内联，至少不改变结论
        try:
            return "ok", re.search(pattern, text, re.DOTALL) is not None, ""
        except re.error as exc:
            return "error", False, f"正则非法：{exc}"

    proc.join(timeout)
    if proc.is_alive():
        proc.terminate()
        proc.join(1)
        return "timeout", False, ""
    try:
        ok, matched, err = q.get(timeout=1)
    except Exception:  # noqa: BLE001 - 子进程没来得及写回（被杀或崩溃）
        return "error", False, "匹配子进程未返回结果"
    return ("ok" if ok else "error"), bool(matched), err


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
    # 反馈要能"照着改"，所以把比什么、实际拿到什么都带上（各自截断）。
    # 只有计数的否决会让修订器瞎猜，同一处错误反复修不掉。
    expected: str = Field(default="", description="期望片段（单行截断版，供修订器定位）")
    output_excerpt: str = Field(default="", description="被测输出摘要（单行截断版）")


def check_assertion(expected: str, output: str, mode: str) -> AssertionResult | None:
    """执行确定性断言。

    返回 None 表示"该用例没有可执行的断言"，调用方应跳过而不是当作失败。

    自定义断言（custom:*）不需要 expected：判定完全交由注册的断言函数；
    其余模式必须以非空 expected 为前提（exact/contains/regex 才有"比什么"的问题）。
    """
    out = output or ""
    out_scan = out[:_MAX_SCANNED]
    exp_full = _clip((expected or "").strip(), _EXPECTED_CLIP)
    excerpt = _clip(out_scan, _OUTPUT_CLIP)

    if mode.startswith("custom:"):
        name = mode[len("custom:") :]
        fn = _get_custom(name)
        if fn is None:
            return AssertionResult(
                mode=mode,
                passed=False,
                detail=f"未注册的自定义断言：{name}（需要先 pm.assertions.register_assertion）",
                score=0.0,
                expected=exp_full,
                output_excerpt=excerpt,
            )
        try:
            passed = bool(fn(expected or "", out))
        except Exception as e:  # noqa: BLE001 - 用户断言函数抛错不能炸图
            return AssertionResult(
                mode=mode,
                passed=False,
                detail=f"断言函数异常：{e}",
                score=0.0,
                expected=exp_full,
                output_excerpt=excerpt,
            )
        return AssertionResult(
            mode=mode,
            passed=passed,
            detail="" if passed else f"自定义断言 {name} 未通过",
            score=1.0 if passed else 0.0,
            expected=exp_full,
            output_excerpt=excerpt,
        )

    exp = (expected or "").strip()
    if not exp:
        return None

    if mode == "exact":
        # exact 保持字面严格：它的用途就是"逐字一致"，归一化会悄悄放宽语义。
        passed = out.strip() == exp
        return AssertionResult(
            mode=mode,
            passed=passed,
            detail=(
                ""
                if passed
                else f"要求逐字一致：期望 {len(exp)} 字符，实际输出 {len(out.strip())} 字符"
            ),
            score=1.0 if passed else 0.0,
            expected=exp_full,
            output_excerpt=excerpt,
        )

    if mode == "contains":
        # veto 语义下过于苛刻会误杀：全角冒号「：」与半角「:」、大小写不同都只是排版差异，
        # 却会让一条本该通过的确定性检查把整轮优化判死。默认忽略这些差异（PM_ASSERT_NORMALIZE=0 可关）。
        # 匹配必须用**原始** expected：`exp_full` 是给人看的单行截断版，拿它去比就永远好不了一致。
        normalized = _flag("PM_ASSERT_NORMALIZE", True)
        if normalized:
            passed = _fold(exp) in _fold(out_scan)
        else:
            passed = exp in out_scan
        note = (
            "（已忽略大小写、全/半角与空白差异）"
            if normalized
            else "（严格字面匹配，PM_ASSERT_NORMALIZE=0）"
        )
        return AssertionResult(
            mode=mode,
            passed=passed,
            detail="" if passed else f"输出未包含期望片段{note}",
            score=1.0 if passed else 0.0,
            expected=exp_full,
            output_excerpt=excerpt,
        )

    if mode == "regex":
        if len(exp) > 500:
            return AssertionResult(
                mode=mode,
                passed=False,
                detail=f"正则过长（{len(exp)} 字符），拒绝执行；需要复杂校验请改用 custom:<name>",
                score=0.0,
                expected=exp_full,
                output_excerpt=excerpt,
            )
        if _NESTED_QUANTIFIER.search(exp):
            return AssertionResult(
                mode=mode,
                passed=False,
                detail="正则含嵌套量词，存在灾难性回溯风险，已拒绝执行（改写为无嵌套量词或用 custom:*）",
                score=0.0,
                expected=exp_full,
                output_excerpt=excerpt,
            )
        try:
            re.compile(exp)
        except re.error as e:
            return AssertionResult(
                mode=mode,
                passed=False,
                detail=f"正则非法：{e}",
                score=0.0,
                expected=exp_full,
                output_excerpt=excerpt,
            )
        budget = _seconds("PM_ASSERT_REGEX_TIMEOUT", 2.0)
        if _QUANTIFIED_GROUP.search(exp):
            status, matched, err = _match_isolated(exp, out_scan, budget)
            if status == "error":
                return AssertionResult(
                    mode=mode,
                    passed=False,
                    detail=err or "正则匹配执行失败",
                    score=0.0,
                    expected=exp_full,
                    output_excerpt=excerpt,
                )
            if status == "timeout":
                return AssertionResult(
                    mode=mode,
                    passed=False,
                    detail=(
                        f"正则匹配超过 {budget:g}s 预算，已杀掉子进程并按未通过处理："
                        "模式或输出触发了回溯爆炸"
                    ),
                    score=0.0,
                    expected=exp_full,
                    output_excerpt=excerpt,
                )
        else:
            # 没有量词包住的结构，内联跑：给每个正则断言加 0.5s 进程启动代价不值得
            try:
                matched = re.search(exp, out_scan, re.DOTALL) is not None
            except re.error as e:
                return AssertionResult(
                    mode=mode,
                    passed=False,
                    detail=f"正则非法：{e}",
                    score=0.0,
                    expected=exp_full,
                    output_excerpt=excerpt,
                )
        return AssertionResult(
            mode=mode,
            passed=matched,
            detail="" if matched else "正则未命中",
            score=1.0 if matched else 0.0,
            expected=exp_full,
            output_excerpt=excerpt,
        )

    return AssertionResult(
        mode=mode,
        passed=False,
        detail=f"未知断言模式：{mode}",
        score=0.0,
        expected=exp_full,
        output_excerpt=excerpt,
    )
