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
    reasons = _reasons(mod_name)
    assert reasons, f"{mod_name}.py 里找不到「空内容」类 early_stop_reason"
    for r in reasons:
        assert "未观测到" in r or "根因待查" in r, (
            f"{mod_name}.py 的空产出早停文案没说明「原因未知」：{r!r}"
        )


@pytest.mark.parametrize("mod_name", ["optimize", "revise"])
def test_empty_stop_reason_asserts_no_cause(mod_name: str) -> None:
    for r in _reasons(mod_name):
        stripped = NEGATED.sub("", r)
        bad = [w for w in CAUSAL_WORDS if w in stripped]
        assert not bad, f"{mod_name}.py 的空产出早停文案断言了未经观测的成因 {bad}：{r!r}"


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
