"""不得凭空归因：空产出的早停文案不能编造根因（§三十·十九·六）。

这条是被 §三十·十九·二 自己的错误逼出来的：

那节我写「干净复现了 §三十·十六·三 那个机制 / reasoning 烧完预算」，
而 `early_stop_reason` 里早就写死的「（端点截断/思考耗尽）」恰好长这样，
于是它被我当成了"证据"。随后 token 分布把它推翻——**空产出横跨
1493~16425 全分布**，若真是额度烧光，不可能两端都有。

⇒ 归档里写死一个**没有证据的归因**，比不写更坏：
  它会让人（包括我自己）顺着错误前提往下推。

⚠️ 判据范围（第一版写太宽，被自己的输出打脸）：
  只管「返回空内容」这一类早停。
  `revise.py:70` 的「墙钟预算用尽」**不算**——那是代码自己观测到的事实
  （预算计数器归零），有据可依，不该被要求写「根因待查」。
  第一版把全仓因果词都禁掉，误伤了它，也误伤了我自己写的
  「未观测到截断/额度证据」——那句里的「截断」是否定用法。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "pm" / "nodes"
EMPTY_REASON = re.compile(r'"early_stop_reason":\s*"([^"]*空内容[^"]*)"')

# 只在**断言成因**时才是问题；「未观测到 X」是本节要求的正确写法，
# 所以先剥掉否定式，再看还剩什么成因词。
NEGATED = re.compile(r"未观测到[^，）]*[，）]?")
CAUSAL_WORDS = ("截断", "思考耗尽", "额度耗尽", "耗尽预算", "推理耗尽", "reasoning")


def _reasons(mod_name: str) -> list[str]:
    src = (SRC / f"{mod_name}.py").read_text(encoding="utf-8")
    return EMPTY_REASON.findall(src)


@pytest.mark.parametrize("mod_name", ["optimize", "revise"])
def test_empty_stop_reason_is_present_and_says_unknown(mod_name: str) -> None:
    """两个节点都必须**走同一个归因函数**，且"没证据"那一支仍写未知。

    2026-10-15 之前这里查的是节点里的字面量；字面量改成 `empty_output_reason()` 之后，
    只查字面量就等于"文案消失了"（红），而真正该钉的是
    ①节点确实接在这个函数上、②函数在无证据时不许编成因。两条都在这里。
    """
    src = (SRC / f"{mod_name}.py").read_text(encoding="utf-8")
    assert "empty_output_reason(" in src, f"{mod_name}.py 的空产出文案没走共用归因函数"
    assert not _reasons(mod_name), f"{mod_name}.py 里又出现了写死的空产出 early_stop_reason 字面量"

    from pm.nodes.optimize import EMPTY_UNKNOWN, empty_output_reason

    no_evidence = empty_output_reason("修订器", {"role": "reviser"}, 3)
    assert no_evidence.endswith(EMPTY_UNKNOWN), no_evidence
    assert "未观测到" in no_evidence and "根因待查" in no_evidence


@pytest.mark.parametrize("mod_name", ["optimize", "revise"])
def test_empty_stop_reason_asserts_no_cause(mod_name: str) -> None:
    """无证据路径不许出现任何断言性成因词（含函数里的默认文案）。"""
    from pm.nodes.optimize import empty_output_reason

    for meta in ({}, {"output_tokens": 1500, "max_tokens": 24000}, {"max_tokens": 0}):
        r = empty_output_reason("修订器", {**meta, "role": "reviser"}, 3)
        stripped = NEGATED.sub("", r)
        bad = [w for w in CAUSAL_WORDS if w in stripped]
        assert not bad, f"够不到 max_tokens 却断言了成因 {bad}：{r!r}"


def test_budget_exhaustion_attribution_is_left_alone() -> None:
    """墙钟预算用尽**有**代码观测为据，不该被本节判据波及。

    这是判据范围的钉子：防止将来有人把范围扩大到所有 early_stop_reason，
    把有据的归因也改成「根因待查」——那会把确定的结论也抹掉。
    """
    src = (SRC / "revise.py").read_text(encoding="utf-8")
    assert "墙钟预算用尽" in src, "预算耗尽的早停文案不见了"
    hit = re.search(r'"early_stop_reason":\s*"([^"]*墙钟预算用尽[^"]*)"', src)
    assert hit, "预算类早停文案没找到"
    # 它不该被要求写「根因待查」
    assert "根因待查" not in hit.group(1), "有据的归因不该改成未知"


def test_measured_diagnostics_in_llm_py_are_not_flagged() -> None:
    """`pm/llm.py` 的额度诊断是**有实测数据**的，不在本节范围。"""
    llm_src = (SRC.parent / "llm.py").read_text(encoding="utf-8")
    assert "reasoning_tokens" in llm_src
    assert "额度被**推理**耗尽" in llm_src, "llm.py 的实测诊断被误伤了"


# ---------------------------------------------------------------------------
# 2026-10-15 追加：空产出的归因从"写死未知"升级成"有证据就说数，没证据仍说未知"。
# 本节原意不是"永远不许归因"，而是"不许写没有证据的归因"——所以补三条：
# 无证据路径必须仍是未知、有证据路径必须带当场实测的两个数、证据必须真能从
# plain_call 走到文案（否则前两条只是纸面）。
# ---------------------------------------------------------------------------


def test_no_evidence_path_still_says_unknown() -> None:
    from pm.nodes.optimize import empty_output_reason

    reason = empty_output_reason(
        "修订器", {"role": "reviser", "output_tokens": 1500, "max_tokens": 24000}, 3
    )
    assert "未观测到" in reason and "根因待查" in reason, reason
    stripped = NEGATED.sub("", reason)
    bad = [w for w in CAUSAL_WORDS if w in stripped]
    assert not bad, f"够不到上限却断言了成因 {bad}：{reason!r}"


def test_evidence_path_reports_the_measured_numbers() -> None:
    """`5253290b3fb5` 的实况：修订器 3 发全部 output_tokens=24000=max_tokens 而正文为空。"""
    from pm.nodes.optimize import empty_output_reason

    meta = {"role": "reviser", "output_tokens": 24000, "max_tokens": 24000}
    reason = empty_output_reason("修订器", meta, 3)
    assert "output_tokens=24000" in reason and "max_tokens=24000" in reason, reason
    assert "reviser" in reason, "归因要点名是哪个角色的上限"
    assert "max_tokens" in reason
    assert "根因待查" not in reason, "有证据时不该再写未知"


def test_plain_call_meta_actually_carries_the_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """证据链的中间一环：`plain_call` 的 meta 必须带 max_tokens 与 output_tokens。

    摘掉这两个键，上面那条"有证据"分支就永远不会触发，文案退回"根因待查"——
    而那正是本轮真端点跑出来的处境（节点手里有数，meta 里没数）。
    """
    from types import SimpleNamespace

    from pm import llm
    from pm.nodes.optimize import empty_output_reason

    fake_resp = SimpleNamespace(
        content="",
        usage_metadata={"input_tokens": 1201, "output_tokens": 24000},
        response_metadata={},
    )
    fake_llm = SimpleNamespace(invoke=lambda _msgs: fake_resp)
    monkeypatch.setattr(
        llm,
        "_invoke_with_rate_limit_retry",
        lambda role, fn, overrides=None, counter=None, budget=None: fn(fake_llm),
    )
    _text, meta = llm.plain_call("reviser", "sys", "user")
    assert meta.get("max_tokens"), f"meta 没带 max_tokens：{sorted(meta)}"
    assert meta.get("output_tokens") == 24000, meta
    assert "output_tokens=24000" in empty_output_reason("修订器", meta, 3), meta
