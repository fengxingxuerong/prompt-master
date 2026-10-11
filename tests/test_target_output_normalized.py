"""目标输出入库前必须抹掉端点自带的前导/尾随空白。

实测（2026-10-10）：SenseNova 端点对**每一次** completion 都前缀两个换行 ——
连"只回答一个数字、第一个字符必须是数字"这种极小强约束也返回 "\\n\\n4"。
这属于端点/模板级行为、提示词改不动，但评委每次都据此记一条「前导空行」轻微
格式瑕疵，噪声重复出现在每一份报告里。修复点：`pm/nodes/execute.py` 的
`_normalize_output`，在入库与缓存读取两处统一生效。
"""

from __future__ import annotations

import pm.llm as L
import pm.nodes.execute as EX


def _patch_plain(monkeypatch, text: str) -> None:
    """让目标调用返回固定文本，并切断真实缓存（绝不写进仓库 logs/）。"""
    monkeypatch.setattr(L, "plain_call", lambda *a, **k: (text, {"model": "m", "latency_ms": 1}))
    monkeypatch.setattr(EX.llm, "plain_call", L.plain_call)
    monkeypatch.setattr(EX, "target_cache", lambda: None)


def test_leading_blank_lines_are_stripped(monkeypatch):
    """端点前缀的 ``\\n\\n`` 必须在入库前被抹掉。"""
    _patch_plain(monkeypatch, "\n\n类别：物流\n依据：x\n缺失信息：无\n\n")
    run = EX._run_one_target(0, "case", "prompt", "m", sample=0)

    assert run.output == "类别：物流\n依据：x\n缺失信息：无"
    assert run.error is None
    assert not run.output[:1].isspace(), "输出首字符仍是空白，前导空行没抹干净"


def test_whitespace_only_output_still_flagged_empty(monkeypatch):
    """strip 不能把空输出误判成非空：纯空白仍须标记 empty_output。"""
    _patch_plain(monkeypatch, "\n\n  \t\n")
    run = EX._run_one_target(0, "case", "prompt", "m", sample=0)

    assert run.output == ""
    assert run.error == "empty_output", "纯空白输出必须被标记，否则会被静默计入均分"
