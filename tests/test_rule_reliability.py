"""规则层可靠性表的守卫（PE-5）：语料合同、文档抄件同步、关键行为钉死。

承重的是第 2 条。`docs/rule-reliability.md` 里的表是
`scripts/measure_rule_reliability.py` 输出的**抄件**——抄件与实现漂移的后果和
「MCP 工具数抄件」那次一样：文档安静地说谎。所以表区块逐字比对；
语料合同单独守（新规则上线必须先补语料标注，否则 measurement 直接拒绝运行）。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "rule_reliability" / "rule_corpus.json"
DOC = ROOT / "docs" / "rule-reliability.md"
SCRIPT = ROOT / "scripts" / "measure_rule_reliability.py"

MARK_BEGIN = "<!-- measure:begin -->"
MARK_END = "<!-- measure:end -->"


def _load_script() -> Any:
    spec = importlib.util.spec_from_file_location("measure_rule_reliability", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def meas() -> Any:
    return _load_script()


@pytest.fixture(scope="module")
def corpus() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(CORPUS.read_text(encoding="utf-8"))
    return data


@pytest.fixture(scope="module")
def result(meas: Any, corpus: dict[str, Any]) -> dict[str, Any]:
    return meas.compute(corpus)


# ---- 语料合同：标注与身份的最低完整性 ----


def test_corpus_contract(corpus: dict[str, Any]) -> None:
    assert corpus["schema"] == "rule-reliability-corpus/1"
    codes: list[str] = corpus["codes"]
    assert len(codes) == len(set(codes))
    items: list[dict[str, Any]] = corpus["items"]
    ids = [it["id"] for it in items]
    assert len(ids) == len(set(ids)), "语料 id 必须唯一"
    real = [it for it in items if it["provenance"] == "run-log"]
    crafted = [it for it in items if it["provenance"] == "crafted"]
    assert {it["provenance"] for it in items} <= {"run-log", "crafted"}
    assert len(real) == 72 and len(crafted) >= 20, (
        "真实 72 条全量 + 对抗变体；变了要先改本文档的说法"
    )
    for it in items:
        assert set(it["labels"]) == set(codes), f"{it['id']} 的标注必须覆盖全部规则"
        assert all(isinstance(v, bool) for v in it["labels"].values()), f"{it['id']} 标注必须是布尔"
        assert it["text"].strip(), f"{it['id']} 正文为空"
    for it in crafted:
        assert it["variant"], f"{it['id']} 缺 variant（变体覆盖表靠它计数）"
    for pair in corpus["pairs"]:
        assert isinstance(pair["expect_regressed"], bool), f"{pair['id']} 期望缺失"


def test_every_code_has_positives(corpus: dict[str, Any]) -> None:
    """召回的分母：每条规则在语料里必须有正例，否则那一行是「没测」不是「满分」。"""
    for code in corpus["codes"]:
        pos = [it["id"] for it in corpus["items"] if it["labels"].get(code)]
        assert pos, f"规则 {code} 在语料里没有正例，召回率无意义"


# ---- 文档抄件同步 ----


def test_doc_tables_are_verbatim_copies(meas: Any, corpus: dict[str, Any]) -> None:
    doc = DOC.read_text(encoding="utf-8")
    assert MARK_BEGIN in doc and MARK_END in doc, "文档缺 measure 区块标记"
    block = doc.split(MARK_BEGIN, 1)[1].split(MARK_END, 1)[0].strip()
    expected = meas.render_markdown(meas.compute(corpus), corpus).strip()
    assert block == expected, "文档里的表与 measurement 输出不一致——跑一遍脚本把抄件更新掉"


def test_measurement_is_deterministic(meas: Any, corpus: dict[str, Any]) -> None:
    assert json.dumps(meas.compute(corpus)) == json.dumps(meas.compute(corpus))


# ---- 行为钉死：表上每个非满分格子都要有一条语料撑着，改实现先改这里 ----


def _by_id(corpus: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {it["id"]: it for it in corpus["items"]}


def test_real_false_positives_are_still_fired(meas: Any, corpus: dict[str, Any]) -> None:
    """R38/R42 是真实交付物的两例误报（行中开 + 行尾闭）。修掉这个误报前，
    这两条会一直红着提醒你：先更新语料标注与文档表，再合实现。"""
    items = _by_id(corpus)
    for rid in ("R38", "R42"):
        fired = meas.fired_codes(items[rid]["text"], corpus["codes"])
        assert fired == {"delimiter_unbalanced"}, (
            f"{rid} 预期命中 delimiter_unbalanced，实际 {fired}"
        )
        assert not items[rid]["labels"]["delimiter_unbalanced"]


def test_known_miss_is_still_missed(meas: Any, corpus: dict[str, Any]) -> None:
    """C14（同行撇号吞句）是计 FN 的已知漏判。哪天有人把它修了，这条会红——
    那是好事：把语料标注改成命中、重跑脚本、更新文档，召回率就该涨。"""
    items = _by_id(corpus)
    assert meas.fired_codes(items["C14"]["text"], corpus["codes"]) == set()
    assert items["C14"]["labels"]["suppressive_rule"]


def test_gate_blind_spot_pair_is_pinned(meas: Any, corpus: dict[str, Any]) -> None:
    """P08：已命中 code 内约束 10→12，regressed 判决按合同仍为 False。
    若有人给 diff/gate 加了计数通道，这条会红——更新 P08 期望与文档第四节。"""
    from pm.promptdiff import diff_prompts

    pair = next(p for p in corpus["pairs"] if p["id"] == "P08")
    delta = diff_prompts(pair["before"], pair["after"])
    assert delta["regressed"] is False
    assert delta["structure"]["constraints"]["delta"] == 2, "盲区的前提（+2 条）变了"


def test_measurement_agrees_with_doc_claims(result: dict[str, Any]) -> None:
    """文档第四节引用的三个读数，实现一动就要对数。"""
    delim = result["per_code"]["delimiter_unbalanced"]["overall"]
    assert delim["fp"] == 3 and delim["precision"] == 0.5
    supp = result["per_code"]["suppressive_rule"]["overall"]
    assert supp["fn"] == 1 and supp["recall"] == pytest.approx(2 / 3)
    assert result["pairs_match"] == len(result["pairs"])
