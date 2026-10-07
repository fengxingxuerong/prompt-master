"""空产出要多给重试机会，但质量问题不给（§三十·二十一）。

实测背景（§三十·二十）：空产出率 **19.7%**（66 轮里 13 轮），
而 `optimize` 拿到空串就早停 —— 那 13 轮整轮作废、3 万多 token 打水漂。

现状（实测，不是猜）：`check_prompt_quality("")` → ok=False / "提示词为空"，
所以空串**确实会触发**既有那次带 hint 重试（实测 calls=2）。
问题只在于**第二次还空就放弃**。

这一节加的：空产出额外最多 2 次机会。
判据是**分别钉住两边**——空产出要多试，质量问题仍只试一次。
"""

from __future__ import annotations

from typing import Any

from pm import backend
from pm.nodes.optimize import _generate_prompt_with_gate, _is_empty_prompt


def _hook(seq: list[str]):
    """按 seq 依次返回内容的 plain 钩子（真走 _generate_prompt_with_gate）。"""

    def _plain(role: str, system: str, user: str, overrides: Any = None):
        i = min(len(calls), len(seq) - 1)
        calls.append(role)
        return seq[i], {"role": role, "model": f"m{len(calls)}", "channel": "plain"}

    calls: list[str] = []
    return _plain, calls


GOOD = "你是一位资深的数据分析专家。请分析销售数据，给出趋势结论、异常点与数据质量说明。" * 3


def test_empty_prompt_predicate_covers_whitespace() -> None:
    """空 = strip 后为空。实测纯空白串同样被判「提示词为空」。"""
    assert _is_empty_prompt("")
    assert _is_empty_prompt("   \n\t ")
    assert not _is_empty_prompt("x")
    assert not _is_empty_prompt("  有内容  ")


def test_empty_first_call_gets_extra_retries() -> None:
    """第 1 次空、第 2 次空、第 3 次好 → 必须救回来（这是本次的核心价值）。"""
    plain, calls = _hook(["", "", GOOD])
    with backend.use(backend.CallHook(plain=plain)):
        prompt, _meta, rep, n = _generate_prompt_with_gate("optimizer", "s", "u")

    assert prompt.strip(), "两次空之后应当救回来"
    assert rep.ok
    assert len(calls) == 3, f"应调用 3 次，实际 {len(calls)}"
    assert n == 3, "回报的 llm_calls 要与实际调用数一致"


def test_empty_meta_follows_the_successful_call() -> None:
    """成功后返回的 meta 必须是**那次成功调用**的，不能是第一次空产出的。

    ⚠️ 第一版漏了 `meta` 赋值，于是重试成功却报着第一次空产出的元信息
    （"哪个模型/花了多久"全对不上）。mypy 的 no-any-return 顺带把它揪出来，
    但那条判据看不出语义错，所以这里显式钉。
    """
    plain, _calls = _hook(["", "", GOOD])
    with backend.use(backend.CallHook(plain=plain)):
        _prompt, meta, _rep, _n = _generate_prompt_with_gate("optimizer", "s", "u")

    assert meta["model"] == "m3", f"meta 应来自第 3 次调用，实际 {meta}"


def test_empty_retries_are_bounded() -> None:
    """一直空也不能无限试——成本上限 2 次额外重试，总计 3 次。"""
    plain, calls = _hook([""])
    with backend.use(backend.CallHook(plain=plain)):
        prompt, _meta, rep, n = _generate_prompt_with_gate("optimizer", "s", "u")

    assert prompt == ""
    assert not rep.ok
    assert len(calls) == 3, f"应停在 3 次（1 次初始 + 2 次额外），实际 {len(calls)}"
    assert n == 3


def test_quality_problems_still_get_only_one_retry() -> None:
    """**非空**的质量问题仍只重试一次——反复重试会让提示词越改越偏。

    判据用"太短但非空"：check_prompt_quality 对 <100 字符判「过短」，
    它非空 ⇒ 走质量问题那条路，不该拿到额外机会。
    """
    short = "太短"  # 非空，但 <100 字符
    plain, calls = _hook([short, short, GOOD])
    with backend.use(backend.CallHook(plain=plain)):
        _prompt, _meta, rep, _n = _generate_prompt_with_gate("optimizer", "s", "u")

    assert len(calls) == 2, f"质量问题只该重试 1 次（总 2 次调用），实际 {len(calls)}"
    assert not rep.ok, "两次都短，不该判通过"


def test_first_call_ok_makes_no_extra_call() -> None:
    """第一次就好的正常路径**一次都不能多调**（这是 99% 的轮次）。"""
    plain, calls = _hook([GOOD])
    with backend.use(backend.CallHook(plain=plain)):
        prompt, _meta, rep, n = _generate_prompt_with_gate("optimizer", "s", "u")

    assert prompt == GOOD
    assert rep.ok
    assert len(calls) == 1
    assert n == 1
