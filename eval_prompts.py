#!/usr/bin/env python3
"""节点提示词回归评测（提示词工程升级项）。

为什么需要它：本项目给"被优化的提示词"建了完整评估闭环，但 `pm/prompts.py` 里
6 个节点提示词自身长期靠经验判断升级 —— 这是双重标准。本工具把节点提示词也纳入
数据驱动的回归体系：

- **离线模式（默认，零成本，CI 可跑）**：结构契约校验。
  每条用例用真实变量渲染节点模板，校验：无未渲染占位符残留、system/user 切分、
  安全约束块齐全、输出自检块存在等 —— 提示词改动破坏结构时立刻红。
- **真实模式（--live，消耗真实 API）**：执行节点提示词并用**确定性代码侧校验**
  打分（不信任 LLM 自评，与本项目的防放水立场一致）：
  clarifier 看提问预算与 is_clear、optimizer/reviser 过质量门、
  mockgen 看场景覆盖（main_path+boundary）、evaluator 看劣质输出是否被压分。

用法：
    python eval_prompts.py                 # 离线全量（结构契约）
    python eval_prompts.py --node clarifier
    python eval_prompts.py --live          # 真实调用（花真钱，手动跑）
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

from pm.bootstrap import ensure_utf8_stdio

ensure_utf8_stdio()

from pm.llm import plain_call, structured_call  # noqa: E402
from pm.prompts import (  # noqa: E402
    CLARIFIER_SYSTEM,
    CLARIFIER_USER,
    EVALUATOR_SYSTEM,
    EVALUATOR_USER,
    MOCKGEN_SYSTEM,
    MOCKGEN_USER,
    OPTIMIZER_SYSTEM,
    OPTIMIZER_USER,
    REVISER_SYSTEM,
    REVISER_USER,
    render,
)
from pm.quality import check_prompt_quality  # noqa: E402  # 需先完成 sys.path 注入
from pm.schemas import (  # noqa: E402
    ClarificationResult,
    EvaluationResult,
    MockInputSet,
)

CASES_PATH = Path(__file__).parent / "prompt_eval" / "cases.json"

# Reviser 净增量阈值（--live 首轮验证的教训）：纯相对阈值 30% 对短基准过严
# （65 字基准只有约 20 字余量，模型补全合法结构就必然超），加绝对下限兜底
REVISER_GROWTH_RATIO = 0.3
REVISER_GROWTH_ABS_FLOOR = 200  # 字符

TEMPLATES: dict[str, tuple[str, str]] = {
    "clarifier": (CLARIFIER_SYSTEM, CLARIFIER_USER),
    "optimizer": (OPTIMIZER_SYSTEM, OPTIMIZER_USER),
    "mockgen": (MOCKGEN_SYSTEM, MOCKGEN_USER),
    "evaluator": (EVALUATOR_SYSTEM, EVALUATOR_USER),
    "reviser": (REVISER_SYSTEM, REVISER_USER),
}

# 各节点 schema（真实模式解析用；optimizer/reviser 是纯文本输出不在其列）
NODE_SCHEMAS: dict[str, type] = {
    "clarifier": ClarificationResult,
    "mockgen": MockInputSet,
    "evaluator": EvaluationResult,
}


def load_cases() -> dict[str, list[dict[str, Any]]]:
    return json.loads(CASES_PATH.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# 离线：结构契约校验
# --------------------------------------------------------------------------
def check_structure(node: str, case: dict[str, Any]) -> list[str]:
    """渲染模板并做结构契约校验。返回问题列表（空 = 通过）。"""
    issues: list[str] = []
    system_tpl, user_tpl = TEMPLATES[node]
    render_vars = dict(case["vars"])
    # reviser 模板带注入式字符预算占位符（prev_len/max_len）：
    # 与 check_live / revise_node 同口径注入，离线渲染才不残留占位符
    if node == "reviser" and "previous_prompt" in render_vars:
        prev = render_vars["previous_prompt"]
        render_vars.setdefault("prev_len", len(prev))
        render_vars.setdefault("max_len", int(len(prev) * 1.3) + 1)
    system = render(system_tpl, **render_vars)
    user = render(user_tpl, **render_vars)

    # 1) 占位符必须全部渲染：残留 <<VAR>> 说明用例缺变量或模板笔误
    for label, text in (("system", system), ("user", user)):
        leaked = re.findall(r"<<[A-Za-z_]\w*>>", text)
        if leaked:
            issues.append(f"{label} 段有未渲染占位符：{leaked}")

    # 2) 安全约束块：所有节点的 system 都必须声明数据/指令边界
    if "安全约束" not in system:
        issues.append("system 缺少 <安全约束> 块（注入防御声明）")

    # 3) 节点专项结构
    if node == "optimizer":
        if "输出前自检" not in system:
            issues.append("optimizer system 缺少 <输出前自检> 清单")
        if "边界语义显式化" not in system:
            issues.append("optimizer system 缺少边界语义显式化规则")
    if node == "mockgen":
        if "scenario" not in system:
            issues.append("mockgen system 未要求 scenario 场景标注（覆盖校验依赖它）")
    if node == "evaluator":
        if "先取证" not in system and "取证" not in system:
            issues.append("evaluator system 缺少「先取证、后打分」流程")
    if node == "reviser":
        if not ("已验证" in system and "弱化" in system):
            issues.append("reviser system 缺少「已验证约束不得删除/弱化」规则")

    # 4) expect 里声明的结构要求（用例自定义，当前仅 comment，预留）
    return issues


# --------------------------------------------------------------------------
# 真实：执行 + 确定性代码侧校验
# --------------------------------------------------------------------------
def check_live(node: str, case: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    system_tpl, user_tpl = TEMPLATES[node]
    render_vars = dict(case["vars"])
    # reviser：注入字符预算（与主管道 revise_node 同口径）——抽象的"净增量≤30%"
    # 在真实端点上执行不稳，必须翻译成具体数字模型才可执行
    if node == "reviser" and "previous_prompt" in render_vars:
        prev = render_vars["previous_prompt"]
        render_vars.setdefault("prev_len", len(prev))
        render_vars.setdefault("max_len", int(len(prev) * 1.3) + 1)
    system = render(system_tpl, **render_vars)
    user = render(user_tpl, **render_vars)
    expect = case.get("expect", {})

    if node == "optimizer" or node == "reviser":
        text, _meta = plain_call(node if node == "optimizer" else "reviser", system, user)
        text = text.strip()
        if not text:
            issues.append("输出为空")
            return issues
        report = check_prompt_quality(text)
        if not report.ok:
            issues.append(f"质量门未通过：{report.describe()}")
        if node == "reviser":
            prev = case["vars"].get("previous_prompt", "")
            # 净增量阈值 = max(200 字, 30%)：--live 首轮验证发现纯相对阈值对短基准
            # 过严（65 字基准只有约 20 字余量，模型补全合法结构就必然超）；
            # 绝对下限保证短提示词有合理的补全空间，长提示词仍守 30% 不臃肿
            margin = max(REVISER_GROWTH_ABS_FLOOR, len(prev) * REVISER_GROWTH_RATIO)
            if prev and len(text) > len(prev) + margin:
                issues.append(
                    f"净增量超限（{len(prev)} → {len(text)} 字符，余量 {margin:.0f}），"
                    "修订越改越臃肿"
                )
        return issues

    schema = NODE_SCHEMAS[node]
    result, _meta = structured_call(node, schema, system, user)

    if node == "clarifier":
        mq = expect.get("max_questions")
        if mq is not None and len(result.clarifying_questions) > mq:
            issues.append(f"提问数 {len(result.clarifying_questions)} 超预算 {mq}")
        if expect.get("is_clear") is True and not result.is_clear:
            issues.append("三要素齐全的需求被误判不清晰")
        inferred = result.inferred_context
        if not (inferred.target_audience.strip() and inferred.output_format.strip()):
            issues.append("推断上下文关键字段为空")
    elif node == "mockgen":
        min_cases = int(expect.get("min_cases", 1))
        if len(result.test_cases) < min_cases:
            issues.append(f"用例数 {len(result.test_cases)} < 要求 {min_cases}")
        kinds = [s.lower() for s in result.scenario]
        if expect.get("require_main_path") and "main_path" not in kinds:
            issues.append("缺少 main_path 场景用例")
        if expect.get("require_boundary") and "boundary" not in kinds:
            issues.append("缺少 boundary 场景用例")
    elif node == "evaluator":
        result.finalize()
        if (
            expect.get("max_weighted") is not None
            and result.weighted_score >= expect["max_weighted"]
        ):
            issues.append(
                f"劣质输出未被打压：加权分 {result.weighted_score} ≥ {expect['max_weighted']}"
            )
        if expect.get("require_issues") and not result.issues:
            issues.append("issues 为空：未给出可定位问题（先取证后打分未执行）")
    return issues


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="节点提示词回归评测")
    ap.add_argument("--node", choices=sorted(TEMPLATES), help="只评一个节点")
    ap.add_argument("--live", action="store_true", help="真实调用 LLM（消耗 API 额度）")
    args = ap.parse_args()

    cases = load_cases()
    nodes = [args.node] if args.node else sorted(TEMPLATES)
    mode = "真实调用" if args.live else "离线结构契约"
    print(f"节点提示词回归评测（{mode}）")

    total = failed = 0
    for node in nodes:
        for case in cases.get(node, []):
            total += 1
            name = f"{node}/{case['name']}"
            try:
                issues = check_live(node, case) if args.live else check_structure(node, case)
            except Exception as e:  # noqa: BLE001 - 单例失败不中断整体
                issues = [f"执行异常：{type(e).__name__}: {e}"]
            if issues:
                failed += 1
                print(f"  [FAIL] {name}")
                for i in issues:
                    print(f"         - {i}")
            else:
                print(f"  [PASS] {name}")

    print(f"\n结果：{total - failed}/{total} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
