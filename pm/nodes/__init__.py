"""
6 个图节点。

流程：clarify → optimize → mock → test → evaluate → (revise → test → evaluate)* → END

关键设计决策（与原文档的差异）：
1. **测试用例只在首轮生成一次**。原文档每轮都重新生成 mock input，
   会导致迭代前后评估基准漂移，分数对比失去意义 —— 这在优化闭环里是致命的。
   这里首轮锁定测试集，后续轮次复用，保证 A/B 可比。
2. **目标模型调用失败不炸图**。单条用例失败记为 error 并在评估时判低分，
   全部失败才置 status=failed，保证部分失败也能产出可用结果。
3. **判定以代码计算的加权分为准**，模型自报分仅记录用于偏差监控。
4. **双评委交叉验证（P0）**：评估默认由两个评委模型独立打分（PM_JUDGES=2），
   分差在阈值内取各维度均值，分差过大触发第三评委仲裁（PM_ARBITER_*），
   避免单一 LLM 自评的系统性放水。
5. **目标模型调用并发化（P0）**：多用例用线程池并发调用 + 信号量限速
   （PM_TARGET_MAX_CONCURRENCY），把迭代延迟从 用例数 × 轮次 降下来。
6. **本地结果缓存（P1）**：目标输出与评估结果按内容哈希落盘，
   断点续跑（SQLite checkpoint 恢复）时命中即跳过，不重复消耗 API 预算。

模块布局（自单文件 nodes.py 拆分，导入面完全兼容）：
- common.py    节点公共设施（_apply / 围栏剥离 / 开关解析）
- profiles.py  目标模型家族档案
- clarify.py   Node 1/1b 需求澄清 + 向用户提问
- optimize.py  Node 2 提示词优化（生成质量门，revise 复用）
- execute.py   Node 3/4 模拟输入生成 + 测试执行（并发/缓存/断言）
- judge.py     Node 5 评估（双评委 + 仲裁 + 聚合）
- revise.py    Node 6 定向修订
- baseline.py  Node 6b/6c 基线对照 + 成对盲评
- report.py    Node 7 交付报告
"""

from .baseline import baseline_node, compare_node
from .clarify import ask_user_node, clarify_node
from .common import _apply, _feature_enabled, _strip_code_fence, dumps
from .execute import (
    TARGET_CONCURRENCY_CAP,
    _attach_assertion,
    _case_ground_truth,
    _injection_survival,
    _run_matrix,
    _run_one_target,
    _samples_per_case,
    _target_concurrency,
    mock_node,
    test_node,
)
from .judge import (
    _active_judges,
    _build_feedback,
    _call_evaluator,
    _collapse_samples,
    _collect_assertions,
    _dedupe,
    _evaluate_one,
    _evaluate_runs,
    _feedback_digest,
    _is_empty_output_eval,
    _judge_disagreement_threshold,
    _judge_spec,
    _merge_judge_results,
    _merge_rule_checks,
    _merge_rule_verdicts,
    _min_dims,
    _rule_veto_enabled,
    _rules_for_case,
    evaluate_node,
)
from .optimize import _generate_prompt_with_gate, optimize_node
from .profiles import _MODEL_PROFILES, _model_profile
from .report import report_node
from .revise import _attempted_text, revise_node

__all__ = [
    "TARGET_CONCURRENCY_CAP",
    "_MODEL_PROFILES",
    "_active_judges",
    "_apply",
    "_attach_assertion",
    "_attempted_text",
    "_build_feedback",
    "_call_evaluator",
    "_case_ground_truth",
    "_collapse_samples",
    "_collect_assertions",
    "_dedupe",
    "_evaluate_one",
    "_evaluate_runs",
    "_feature_enabled",
    "_feedback_digest",
    "_generate_prompt_with_gate",
    "_injection_survival",
    "_is_empty_output_eval",
    "_judge_disagreement_threshold",
    "_judge_spec",
    "_merge_judge_results",
    "_merge_rule_checks",
    "_merge_rule_verdicts",
    "_min_dims",
    "_model_profile",
    "_rule_veto_enabled",
    "_rules_for_case",
    "_run_matrix",
    "_run_one_target",
    "_samples_per_case",
    "_strip_code_fence",
    "_target_concurrency",
    "ask_user_node",
    "baseline_node",
    "clarify_node",
    "compare_node",
    "dumps",
    "evaluate_node",
    "mock_node",
    "optimize_node",
    "report_node",
    "revise_node",
    "test_node",
]
