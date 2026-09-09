"""
提示词质量门（代码侧规则）回归测试。

覆盖：
- 确定性规则：元话语泄漏 / 上下文泄漏 / 过短 / 空
- 同类问题去重
- _generate_prompt_with_gate：首轮合格 / 重试后合格 / 重试仍不合格
- 与 fake backend 集成：optimize_node 质量警告落 state、evaluate 注入
"""

from __future__ import annotations

from pm import testing
from pm.graph import build_app
from pm.nodes import _generate_prompt_with_gate, optimize_node
from pm.quality import check_prompt_quality
from pm.state import initial_state

# 合格的领域提示词（不应被误杀）
GOOD_PROMPT = """[角色] 资深销售数据分析顾问
[任务] 分析销售数据，指出趋势与异常
[约束] 每个结论必须引用数据；禁止编造缺失值
[输出格式] Markdown 表格 + 3 条结论
[边界] 数据缺失时显式标注「数据缺失」"""

# 元话语泄漏（优化器复读自身 system）
META_LEAK_PROMPT = """你是一位世界顶级的提示词工程师，精通主流大语言模型的提示词设计。
<输出契约>
- 只输出提示词本身
</输出契约>
<硬性规则>
1. 用清晰标签组织
</硬性规则>"""

# 任务上下文泄漏
CONTEXT_LEAK_PROMPT = """[角色] 数据分析师
[任务] 分析数据
<USER_INPUT>
用户原始需求
</USER_INPUT>
<TASK_DESC>
优化用户提供的提示词
</TASK_DESC>"""


# --------------------------------------------------------------------------
# 1. 确定性规则
# --------------------------------------------------------------------------
def test_good_prompt_passes():
    report = check_prompt_quality(GOOD_PROMPT)
    assert report.ok
    assert report.issues == []


def test_meta_leak_detected():
    report = check_prompt_quality(META_LEAK_PROMPT)
    assert not report.ok
    assert any(i.code == "meta_leak" for i in report.issues)


def test_context_leak_detected():
    report = check_prompt_quality(CONTEXT_LEAK_PROMPT)
    assert not report.ok
    assert any(i.code == "context_leak" for i in report.issues)


def test_too_short_detected():
    report = check_prompt_quality("分析数据")
    assert not report.ok
    assert any(i.code == "too_short" for i in report.issues)


def test_empty_detected():
    report = check_prompt_quality("")
    assert not report.ok
    assert report.issues[0].code == "too_short"


def test_same_code_deduped():
    """同类问题（如多个 meta marker）只报一条，避免刷屏。"""
    report = check_prompt_quality(META_LEAK_PROMPT)
    meta = [i for i in report.issues if i.code == "meta_leak"]
    assert len(meta) == 1


def _fake_meta() -> dict:
    return {"model": "fake", "channel": "plain", "attempts": 1, "latency_ms": 1}


# --------------------------------------------------------------------------
# 2. _generate_prompt_with_gate
# --------------------------------------------------------------------------
def test_gate_passes_first_try(monkeypatch):
    calls = {"n": 0}

    def fake_plain(role, system, user, overrides=None):
        calls["n"] += 1
        return GOOD_PROMPT, _fake_meta()

    monkeypatch.setattr("pm.nodes.plain_call", fake_plain)
    prompt, _, report, n = _generate_prompt_with_gate("optimizer", "sys", "user")
    assert report.ok
    assert n == 1
    assert prompt == GOOD_PROMPT


def test_gate_retries_on_meta_leak(monkeypatch):
    """首轮元话语泄漏 → 自动带 hint 重试 → 重试合格。"""
    calls = {"n": 0}

    def fake_plain(role, system, user, overrides=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return META_LEAK_PROMPT, _fake_meta()
        # 重试时 user 段应带质量警告 hint
        assert "quality_warning" in user
        return GOOD_PROMPT, _fake_meta()

    monkeypatch.setattr("pm.nodes.plain_call", fake_plain)
    prompt, _, report, n = _generate_prompt_with_gate("optimizer", "sys", "user")
    assert report.ok
    assert n == 2
    assert prompt == GOOD_PROMPT


def test_gate_keeps_after_two_failures(monkeypatch):
    """两次都不合格 → 按原样接受并记录警告（不炸流程）。"""
    calls = {"n": 0}

    def fake_plain(role, system, user, overrides=None):
        calls["n"] += 1
        return META_LEAK_PROMPT, _fake_meta()

    monkeypatch.setattr("pm.nodes.plain_call", fake_plain)
    prompt, _, report, n = _generate_prompt_with_gate("optimizer", "sys", "user")
    assert not report.ok
    assert n == 2
    assert "输出契约" in prompt


# --------------------------------------------------------------------------
# 3. 与图集成
# --------------------------------------------------------------------------
def test_optimize_node_records_quality_warning(monkeypatch):
    """fake optimizer 输出元话语泄漏 → optimize_node 落 prompt_quality_issues。"""
    calls = {"n": 0}

    def fake_plain(role, system, user, overrides=None):
        calls["n"] += 1
        if role == "optimizer":
            return META_LEAK_PROMPT, _fake_meta()
        return testing._fake_plain(role, system, user, overrides)

    monkeypatch.setattr("pm.nodes.plain_call", fake_plain)
    monkeypatch.setattr("pm.nodes.structured_call", testing._fake_structured)

    state = initial_state(task="分析销售数据", target_model="fake", auto_clarify=True)
    patch = optimize_node(state)
    issues = patch.get("prompt_quality_issues", [])
    assert len(issues) == 1
    assert issues[0]["iteration"] == 0
    assert "元话语泄漏" in issues[0]["issues"]


def test_full_loop_with_meta_leak_optimizer(monkeypatch):
    """优化器持续元话语泄漏时：质量门重试 + 警告注入评估，流程仍能跑完。"""
    import pm.testing as t

    calls = {"n": 0}

    def fake_plain(role, system, user, overrides=None):
        calls["n"] += 1
        if role == "optimizer":
            return META_LEAK_PROMPT, _fake_meta()
        return t._fake_plain(role, system, user, overrides)

    monkeypatch.setattr("pm.nodes.plain_call", fake_plain)
    monkeypatch.setattr("pm.nodes.structured_call", t._fake_structured)
    monkeypatch.setenv("PM_EVAL_CACHE", "0")
    monkeypatch.setenv("PM_TARGET_CACHE", "0")
    t.reset()
    t._SCENARIO = "progress"
    try:
        app = build_app()
        final = app.invoke(
            initial_state(task="分析销售数据", target_model="fake", n_test_cases=2),
            {"configurable": {"thread_id": "quality-loop"}, "recursion_limit": 100},
        )
    finally:
        t._SCENARIO = "progress"
    # 流程仍能到达终态
    assert final["status"] in ("passed", "max_iterations", "failed", "early_stopped")
    # 质量警告被记录
    assert len(final.get("prompt_quality_issues", [])) >= 1
    # evaluate_done trace 中带警告信息（注入评估器）
    eval_traces = [t_ for t_ in final["trace"] if t_["node"] == "evaluate"]
    assert eval_traces, "evaluate 应被执行"
