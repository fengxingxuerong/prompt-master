"""
结构化输出降级通道测试。

背景：原文档只写"请输出严格 JSON"，一旦模型不配合（或端点不支持
function calling / JSON mode）整个流程就断。这里验证降级与重试确实生效。
"""

from __future__ import annotations

import pytest
from pm import llm
from pm.schemas import ClarificationResult

VALID = {
    "is_clear": True,
    "task_summary": "分析销售数据",
    "inferred_context": {
        "target_audience": "业务分析师",
        "domain": "商业分析",
        "output_format": "结构化报告",
        "tone": "专业",
        "constraints": [],
    },
    "clarifying_questions": [],
    "suggested_next_step": "proceed_to_optimizer",
}

import json  # noqa: E402


class _Resp:
    def __init__(self, content: str):
        self.content = content


class _FakeLLM:
    """模拟一个不支持 structured output 的端点。"""

    def __init__(self, responses: list[str]):
        self.responses = list(responses)
        self.calls = 0

    def with_structured_output(self, *a, **k):
        raise NotImplementedError("该端点不支持 structured output")

    def with_config(self, **k):
        return self

    def invoke(self, messages):
        idx = min(self.calls, len(self.responses) - 1)
        self.calls += 1
        return _Resp(self.responses[idx])


def _patch(monkeypatch, responses):
    fake = _FakeLLM(responses)
    monkeypatch.setattr(llm, "get_llm", lambda *a, **k: fake)
    return fake


def test_fallback_recovers_from_markdown_fence(monkeypatch):
    """首轮返回非 JSON，次轮返回带围栏的 JSON —— 应降级解析并成功。"""
    _patch(
        monkeypatch,
        ["我不太明白，能再说清楚点吗？", f"```json\n{json.dumps(VALID, ensure_ascii=False)}\n```"],
    )
    result, meta = llm.structured_call("clarifier", ClarificationResult, "sys", "usr")
    assert isinstance(result, ClarificationResult)
    assert result.is_clear is True
    assert meta["channel"] == "json_fallback"
    assert meta["attempts"] == 2


def test_fallback_strips_surrounding_text(monkeypatch):
    _patch(
        monkeypatch, [f"好的，结果如下：\n{json.dumps(VALID, ensure_ascii=False)}\n希望有帮助！"]
    )
    result, meta = llm.structured_call("clarifier", ClarificationResult, "sys", "usr")
    assert result.task_summary == "分析销售数据"
    assert meta["attempts"] == 1


def test_fallback_raises_after_exhausting_retries(monkeypatch):
    fake = _patch(monkeypatch, ["始终不是 JSON"])
    with pytest.raises(RuntimeError) as exc:
        llm.structured_call("clarifier", ClarificationResult, "sys", "usr", max_retries=3)
    assert "已重试 3 次" in str(exc.value)
    assert fake.calls == 3


def test_fallback_rejects_schema_violating_payload(monkeypatch):
    """JSON 合法但缺必填字段 —— 必须视为失败并重试，不能静默通过。"""
    bad = {"is_clear": "yes"}  # 缺 task_summary / inferred_context 等
    _patch(monkeypatch, [json.dumps(bad), json.dumps(VALID, ensure_ascii=False)])
    result, meta = llm.structured_call("clarifier", ClarificationResult, "sys", "usr")
    assert meta["attempts"] == 2
    assert result.is_clear is True
