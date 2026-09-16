"""
LangGraph 状态定义。

针对原文档的修复：
原骨架里 `lambda state: "optimize" if state["clarification"]["is_clear"] else END`
引用了 `clarification`，但 State 中根本没有这个键 —— 复制即 KeyError。
这里把每个节点会读写的键都显式声明，并对 `errors` / `trace` 用
Annotated reducer 声明累加语义（LangGraph 默认是覆盖，不声明就会丢日志）。
"""

from __future__ import annotations

import operator
import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import Annotated, Any, TypedDict


def new_run_id() -> str:
    return uuid.uuid4().hex[:12]


# trace 重置标记（L7）：纯累加 reducer 在用同一 thread_id 重新提交时会把
# 上一轮的 trace 全部带进本轮（实测 20 条历史记录混进新报告）。
# 新一轮运行以该标记开头提交 trace，即丢弃历史重新计数。
TRACE_RESET: dict = {"__trace_reset__": True}


def trace_reducer(old: list[dict] | None, new: list[dict] | None) -> list[dict]:
    """trace 累加 reducer：普通更新逐条累加，以 TRACE_RESET 开头则丢弃历史。"""
    if not new:
        return list(old or [])
    if isinstance(new[0], dict) and new[0].get("__trace_reset__"):
        return list(new[1:])
    return list(old or []) + list(new)


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class State(TypedDict, total=False):
    """图状态。total=False 便于节点返回 partial update。"""

    # --- 输入 ---
    run_id: str
    task: str
    context: str
    target_model: str
    n_test_cases: int
    max_iterations: int
    auto_clarify: bool
    # 事实断言（ground-truth）：用户提供的测试集 [{"input":..., "expected":...}]
    # 非空时 mockgen 不再生成用例（省一次调用），expected 参与确定性校验
    seed_cases: list[dict]
    assertion_mode: str  # exact | contains | regex | custom:<name>；空 = contains

    # --- 测量层（基线 / 重复采样 / 成对盲评）---
    baseline_runs: list[dict]  # 原始需求直喂 target 的输出（含多采样）
    baseline_aggregate: dict | None  # 与主路同口径的基线聚合分，用于算 Δ
    pairwise: dict | None  # 成对盲评结果：{verdict, votes, details, conflict}
    revision_history: list[dict]  # 每轮“针对什么反馈→改出什么分”，供修订器避重复

    # --- Node 1 澄清 ---
    clarification: dict | None  # ClarificationResult -> dict
    clarification_answers: str  # 用户回答（交互模式）
    clarify_round: int
    # 已向用户提问的轮数（M8）：与 clarify_round 分开计数。
    # clarify_round 是"clarify 分析次数"（每次分析 +1，含带答案重分析），
    # 用它当提问上限会把"最多 2 轮提问"缩水成"最多 1 轮"——
    # 一轮交互 = clarify + ask_user + clarify = 3 次分析，第二轮问题永远问不出去。
    clarify_questions_asked: int
    needs_reanalysis: bool  # 收到用户回答后需重新分析
    unresolved_questions: list[str]

    # --- Node 2 优化 ---
    prompt: str
    prompt_versions: list[dict]  # list[PromptVersion -> dict]
    prompt_quality_issues: list[dict]  # list[{iteration, issues}] 质量门警告（元话语泄漏等）

    # --- Node 3 模拟输入 ---
    test_cases: list[str]
    # 场景类型与 test_cases 逐条对齐（mockgen 产出；用户种子用例时为空列表）
    case_scenarios: list[str]
    # 注入用例的「被注入指令点名的输出短语」，与 test_cases 逐条对齐，非注入条为空串；
    # 代码侧拿它做确定性劫持检测（assertions.injection_hijacked）
    hijack_markers: list[str]

    # --- Node 4 测试执行 ---
    test_runs: list[dict]  # list[TestRun -> dict]

    # --- Node 5 评估 ---
    evaluations: list[dict]  # list[EvaluationResult -> dict]
    aggregate: dict | None  # AggregateScore -> dict
    # 注入存活统计（test_node 计算）：{total, hijacked, details}；无 injection 用例为 None
    injection_survival: dict | None
    # 注入门禁（evaluate_node 计算）：{blocked, total, hijacked}；未触发拦截时为 None。
    # blocked=True 表示达标结论已被门禁压制：安全缺陷不能用评委分数赎回。
    injection_gate: dict | None

    # --- Node 6 修订 / 控制 ---
    iteration: int
    revision_feedback: str
    should_revise: bool
    status: str  # running|passed|max_iterations|failed|early_stopped|needs_clarification
    early_stop_reason: str  # P3: 提前终止原因（回退/平台期）
    final_report: str

    # --- 可观测（累加字段，靠 reducer 生效；trace 支持重置标记，见 trace_reducer）---
    errors: Annotated[list[str], operator.add]
    trace: Annotated[list[dict], trace_reducer]
    llm_calls: int
    # 按角色的 token/调用/耗时台账（llm.usage_scope 记账，run_pipeline / scheduler 收快照）
    # 覆盖语义：执行入口一次性写入汇总值，不需要 reducer 累加
    llm_usage: dict


def initial_state(
    task: str,
    context: str = "",
    target_model: str = "未指定",
    n_test_cases: int = 3,
    max_iterations: int = 3,
    auto_clarify: bool = True,
    seed_cases: list[dict] | None = None,
    assertion_mode: str = "",
) -> dict[str, Any]:
    """构造初始状态（普通 dict，LangGraph 会按 State 的 reducer 合并更新）。"""
    return {
        "run_id": new_run_id(),
        "task": task,
        "context": context,
        "target_model": target_model,
        "n_test_cases": n_test_cases,
        "max_iterations": max_iterations,
        "auto_clarify": auto_clarify,
        "seed_cases": list(seed_cases or []),
        "assertion_mode": assertion_mode or "",
        "baseline_runs": [],
        "baseline_aggregate": None,
        "pairwise": None,
        "revision_history": [],
        "clarification": None,
        "clarification_answers": "",
        "clarify_round": 0,
        "clarify_questions_asked": 0,
        "needs_reanalysis": False,
        "unresolved_questions": [],
        "prompt": "",
        "prompt_versions": [],
        "prompt_quality_issues": [],
        "test_cases": [],
        "case_scenarios": [],
        "hijack_markers": [],
        "test_runs": [],
        "evaluations": [],
        "aggregate": None,
        "injection_survival": None,
        "injection_gate": None,
        "iteration": 0,
        "revision_feedback": "",
        "should_revise": False,
        "status": "running",
        "early_stop_reason": "",
        "final_report": "",
        "errors": [],
        # 以重置标记开头（L7）：同一 thread_id 重新提交时，历史 trace 不会经
        # reducer 累加进本轮。全新线程下该标记是无操作（old 为空）。
        "trace": [dict(TRACE_RESET)],
        "llm_calls": 0,
        # 按角色用量台账（llm.usage_scope 记账）：空 dict 占位保证键始终存在，
        # 无台账（如绕过执行入口的测试直调）时报告不渲染用量表
        "llm_usage": {},
    }


ACCUMULATING_KEYS = {"errors", "trace"}


def merge_state(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """合并两个 partial state（用于脱离 LangGraph 的单元测试与手动编排）。"""
    merged = dict(old)
    for k, v in new.items():
        if k == "trace" and isinstance(v, list):
            # 与图内 reducer 同语义（支持重置标记），否则两处合并行为漂移
            merged[k] = trace_reducer(merged.get(k), v)
        elif k in ACCUMULATING_KEYS and isinstance(v, list):
            merged[k] = list(merged.get(k, [])) + v
        else:
            merged[k] = v
    return merged


def trace_event(state: Mapping[str, Any], node: str, event: str, **payload: Any) -> dict:
    """构造一条结构化日志事件（只含新增项，由 reducer 累加）。"""
    return {
        "trace": [
            {
                "run_id": state.get("run_id"),
                "iteration": state.get("iteration", 0),
                "node": node,
                "event": event,
                "ts": _now(),
                **payload,
            }
        ]
    }
