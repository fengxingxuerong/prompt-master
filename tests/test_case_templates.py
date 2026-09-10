"""用例集模板库（case_templates/）守卫：模板格式漂移在 CI 拦住，而不是用户跑批时才炸。

模板必须满足 --cases-file 的加载契约：
1. JSON 可解析，且顶层是数组；
2. 每条用例有非空 input；
3. mode 只允许 exact/contains/regex/rule/custom:<name>（与 run.py assert_mode_arg 一致）；
4. contains/exact 模式的 expected 不能"长得像需求规则"（与 CLI 预检同口径，PM_RULE_VETO 相关
   的误配会被 run.py 当场拦住——模板里就不该出现）。
"""

from __future__ import annotations

import json
from pathlib import Path

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "case_templates"

VALID_MODES = {"exact", "contains", "regex", "rule"}


def _templates() -> list[Path]:
    paths = sorted(TEMPLATE_DIR.glob("*.json"))
    assert paths, "case_templates/ 下没有任何模板"
    return paths


def test_all_templates_parse_and_have_inputs():
    for path in _templates():
        data = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(data, list), f"{path.name}: 顶层必须是数组"
        cases = [c for c in data if "input" in c]
        assert cases, f"{path.name}: 至少一条用例"
        for i, c in enumerate(cases):
            assert str(c.get("input") or "").strip(), f"{path.name} 第 {i + 1} 条 input 为空"
            assert "expected" in c, (
                f"{path.name} 第 {i + 1} 条缺 expected（模板定位就是带 ground-truth）"
            )


def test_template_modes_are_valid():
    for path in _templates():
        data = json.loads(path.read_text(encoding="utf-8"))
        for c in data:
            mode = str(c.get("mode") or "contains")
            assert mode in VALID_MODES, f"{path.name}: 非法 mode {mode!r}"


def test_literal_modes_do_not_look_like_rules():
    """contains/exact 的 expected 必须是字面片段：写成需求规则永远命不中（CLI 会拦，模板先守）。"""
    from pm.assertions import looks_like_rule

    for path in _templates():
        data = json.loads(path.read_text(encoding="utf-8"))
        for c in data:
            mode = str(c.get("mode") or "contains")
            if mode in {"contains", "exact"}:
                exp = str(c.get("expected") or "")
                assert not looks_like_rule(exp), (
                    f"{path.name}: contains/exact 用例的 expected {exp!r} 读起来像需求规则，"
                    '请改字面片段或把该条 mode 设为 "rule"'
                )


def test_templates_mix_literal_and_semantic():
    """模板定位是示范「字面 + 规则混写」的真实口径：纯单模式模板视为退化。"""
    for path in _templates():
        data = json.loads(path.read_text(encoding="utf-8"))
        modes = {str(c.get("mode") or "contains") for c in data}
        assert modes >= {"contains", "rule"}, (
            f"{path.name}: 模板应同时示范 contains 与 rule 两种模式"
        )
