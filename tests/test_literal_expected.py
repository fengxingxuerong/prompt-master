"""`"literal": true`：显式声明"这句是输出必须写出来的原话"。

实测缺陷（2026-10-31 审计）：合规类任务的期望片段本身会带规则词 ——
「本公司不得为关联方提供担保」是**输出必须出现的那句话**，但按标记词判定会被当成
需求规则：预检**直接拦住**（`--cases-file` 退 2），即使放进运行时也会被降级成
"仅提醒"，一票否决变装饰。于是这类用例**根本没法表达**。

为什么不加"短句豁免"之类的第二层启发式：真实语料里"规则句"是 25~38 字的多分句
（判得对），字面片段 ≤11 字，看着能一刀切；但「必须包含3条结论」（8 字）与
「本公司不得对外担保」（9 字）长度几乎一样、语义相反。猜错的代价是一票否决打错人。

所以给的是**显式开关**：`"literal": true`（或更啰嗦的 `"expected_is_literal": true`），
开了就照旧逐字比对、照旧有一票否决权；关着就是老行为（提醒）。
"""

from __future__ import annotations

import pytest
from pm.assertions import check_assertion, looks_like_rule, rule_like_expected_detail
from pm.casesaudit import is_advisory
from pm.nodes.execute import _attach_assertion

_RULE_SENTENCE = "本公司不得为关联方提供担保"


# ---------------------------------------------------------------- 判据本身
def test_rule_word_snippet_is_still_a_rule_by_default() -> None:
    """默认路径不变：含规则词就是"规则句"（老行为，别在这次改动里被动摇）。"""
    assert looks_like_rule(_RULE_SENTENCE) is True


def test_literal_flag_overrides_the_rule_heuristic() -> None:
    assert looks_like_rule(_RULE_SENTENCE, literal=True) is False


# ------------------------------------------------------------ 预检不再栏住它
def test_preflight_blocks_by_default() -> None:
    detail = rule_like_expected_detail(
        [{"input": "甲给乙担保", "expected": _RULE_SENTENCE}], origin="--cases-file"
    )
    assert detail is not None and "第 1 条" in detail


def test_preflight_lets_literal_cases_through() -> None:
    detail = rule_like_expected_detail(
        [{"input": "甲给乙担保", "expected": _RULE_SENTENCE, "literal": True}],
        origin="--cases-file",
    )
    assert detail is None, "显式声明 literal 之后预检不该再拦"


def test_preflight_message_teaches_the_escape_hatch() -> None:
    """报错话术必须给出出路 —— 否则用户只知道"被拦了"，不知道怎么写才合法。"""
    detail = rule_like_expected_detail([{"input": "x", "expected": "不得编造"}])
    assert detail and '"literal": true' in detail


def test_expected_is_literal_alias_also_works() -> None:
    detail = rule_like_expected_detail(
        [{"input": "x", "expected": _RULE_SENTENCE, "expected_is_literal": True}]
    )
    assert detail is None


# ------------------------------------------------------------ 运行时有一票否决
def test_literal_snippet_keeps_its_veto_power() -> None:
    """开着 literal：缺了这句就是**未通过**，而且不带 advisory（= 计入一票否决）。"""
    bad = check_assertion(_RULE_SENTENCE, "本公司可以担保。", "contains", literal=True)
    assert bad is not None
    assert bad.passed is False
    assert bad.advisory is False, "开着 literal 却被降级成提醒，等于否决权没恢复"

    good = check_assertion(_RULE_SENTENCE, f"结论：{_RULE_SENTENCE}。", "contains", literal=True)
    assert good is not None and good.passed is True and good.advisory is False


def test_without_literal_it_stays_advisory() -> None:
    """关着 literal：老行为 —— 只提醒不否决（打错人的代价更高）。"""
    res = check_assertion(_RULE_SENTENCE, "本公司可以担保。", "contains")
    assert res is not None and res.advisory is True and res.advisory_kind == "mode_mismatch"


def test_attach_assertion_threads_the_flag() -> None:
    """执行层要把 flag 传到断言结果里（否则运行时永远拿不到否决权）。"""
    from pm.schemas import TestRun

    run = TestRun(
        test_case_index=0,
        sample_index=0,
        test_input="i",
        prompt="p",
        output="什么都没有",
        target_model="m",
    )
    got = _attach_assertion(run, _RULE_SENTENCE, "contains", literal=True)
    assert got.assertion is not None
    assert got.assertion["passed"] is False
    assert got.assertion["advisory"] is False


# --------------------------------------------------------- 体检与运行时同口径
def test_cases_audit_counts_literal_cases_as_veto_capable() -> None:
    """体检说"没有否决权"而跑起来真的否决了，是最难解释的一类不一致。"""
    case = {"input": "i", "expected": _RULE_SENTENCE}
    assert is_advisory(case) is True
    assert is_advisory({**case, "literal": True}) is False
    assert is_advisory({**case, "expected_is_literal": "true"}) is False


@pytest.mark.parametrize("flag", ["1", "true", "YES", "on", True])
def test_literal_accepts_the_common_truthy_spellings(flag: object) -> None:
    case = {"input": "i", "expected": _RULE_SENTENCE, "literal": flag}
    assert is_advisory(case) is False


@pytest.mark.parametrize("flag", ["0", "false", "no", "", None, False])
def test_literal_falsey_spellings_keep_the_old_behaviour(flag: object) -> None:
    case = {"input": "i", "expected": _RULE_SENTENCE, "literal": flag}
    assert is_advisory(case) is True


# ------------------------------------------------------- CLI 入口：真的不再拦
def _run_dry(tmp_path, cases: str):
    """零成本跑一次 `--dry-run --cases-file`（不联网、不花钱），只看它拦不拦。"""
    import os
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    f = tmp_path / "cases.json"
    f.write_text(cases, encoding="utf-8")
    proc = subprocess.run(
        [
            sys.executable,
            str(root / "run.py"),
            "--dry-run",
            "--task",
            "分析销售数据并给出结论",
            "--cases-file",
            str(f),
        ],
        cwd=str(root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
        env={**os.environ, "PYTHONIOENCODING": "utf-8", "PM_LOG_DIR": str(tmp_path)},
    )
    return proc.returncode, proc.stdout, proc.stderr


def test_cli_accepts_a_literal_compliance_case(tmp_path) -> None:
    """同一份用例：不开 literal 被预检拦下；开了就正常进入预算阶段。"""
    import json

    plain = json.dumps([{"input": "甲为乙担保", "expected": _RULE_SENTENCE}], ensure_ascii=False)
    rc, _out, err = _run_dry(tmp_path, plain)
    assert rc == 2, f"默认应当拦下（否则这条 expected 永远命不中）：rc={rc}"
    assert "literal" in err, "拦截话术里没有给出 literal 这条出路"

    literal = json.dumps(
        [{"input": "甲为乙担保", "expected": _RULE_SENTENCE, "literal": True}], ensure_ascii=False
    )
    rc2, out2, err2 = _run_dry(tmp_path, literal)
    assert rc2 == 0, f"开了 literal 仍被拦：rc={rc2} err={err2[:300]}"
    assert json.loads(out2)["cases"] == 1
