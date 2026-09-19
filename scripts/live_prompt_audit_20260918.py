#!/usr/bin/env python3
"""针对 2026-09-18 提示词改动的真实调用探针（确定性判定，不用 LLM 自评）。

设计立场：`eval_prompts.py --live` 测的是"节点提示词整体没坏"，这里测的是
**这次改动声称修好的那几件事，在真端点上到底成立没有**。每条探针的判据都是
代码侧可核对的事实（键序、数值上限、字符串包含），不接受模型自证。

    python scripts/live_prompt_audit_20260918.py            # 全部
    python scripts/live_prompt_audit_20260918.py T1 T3      # 只跑指定几条

成本：全跑约 8~11 次调用。与 `run.py` 跑批同时开会抢同一 IP 的配额（429 冷却
实测 5 分钟以上），串行跑。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pm import llm
from pm.prompts import (
    EVALUATOR_SYSTEM,
    EVALUATOR_USER,
    OPTIMIZER_SYSTEM,
    OPTIMIZER_USER,
    REVISER_SYSTEM,
    REVISER_USER,
    render,
)
from pm.quality import SIZE_GROWTH_RATIO, count_constraints
from pm.schemas import EvaluationResult

RESULTS: list[tuple[str, bool, str]] = []


def report(tid: str, ok: bool, detail: str) -> None:
    RESULTS.append((tid, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {tid}  {detail}")


# --------------------------------------------------------------------------
# T1 取证先于打分：真端点产出的 JSON 里 issues 必须排在 dimension_scores 之前
# --------------------------------------------------------------------------
def t1_evidence_precedes_score() -> None:
    """看**模型实际吐出的字节顺序**，而不是看解析后的对象。

    通道 B（把 schema 作为文本注入、自己解析）能直接看到键序；
    通道 A 由端点 constrained decoding 决定，读不到原始串，所以这里强制走文本通道。
    """
    from pm.llm import _schema_hint_block  # 通道 B 注入给模型的那段

    sample = json.loads(Path("judge_calibration/samples.json").read_text(encoding="utf-8"))[0]
    user = render(
        EVALUATOR_USER,
        original_task=sample["original_task"],
        context=sample.get("context") or "（无）",
        prompt=sample["prompt"],
        test_input=sample["test_input"],
        test_output=sample["test_output"],
    )
    system = EVALUATOR_SYSTEM + _schema_hint_block(EvaluationResult)
    text, meta = llm.plain_call("evaluator", system, user)
    from pm.llm import extract_json_object

    obj = extract_json_object(text)
    if not obj:
        report("T1 取证先于打分", False, f"没解析出 JSON：{text[:120]!r}")
        return
    keys = list(obj)
    if "issues" not in keys or "dimension_scores" not in keys:
        report("T1 取证先于打分", False, f"缺键，实际键序 {keys}")
        return
    ok = keys.index("issues") < keys.index("dimension_scores")
    report(
        "T1 取证先于打分",
        ok,
        f"channel={meta.get('channel')} 实际键序={keys[:4]}"
        + ("（证据在分数之前产出）" if ok else "（仍是先给分后补证据）"),
    )


# --------------------------------------------------------------------------
# T2 编造类锚点封顶：含凭空关键事实的输出，加权分必须 ≤6
# --------------------------------------------------------------------------
def t2_fabrication_capped() -> None:
    anchors = json.loads(Path("judge_calibration/samples.json").read_text(encoding="utf-8"))
    fab = [a for a in anchors if "fab" in str(a.get("id", "")) or "halluc" in str(a.get("id", ""))]
    if not fab:
        report("T2 编造封顶", False, "锚点集里没有编造类样本")
        return
    bad: list[str] = []
    for a in fab:
        user = render(
            EVALUATOR_USER,
            original_task=a["original_task"],
            context=a.get("context") or "（无）",
            prompt=a["prompt"],
            test_input=a["test_input"],
            test_output=a["test_output"],
        )
        try:
            ev, _ = llm.structured_call("evaluator", EvaluationResult, EVALUATOR_SYSTEM, user)
        except Exception as e:  # noqa: BLE001 - 探针要如实报失败而不是跳过
            bad.append(f"{a['id']}=评估失败({type(e).__name__})")
            continue
        ev.finalize()
        if ev.weighted_score > 6.0:
            bad.append(f"{a['id']}={ev.weighted_score}（人工 {a['human_score']}）")
    report(
        "T2 编造封顶",
        not bad,
        f"{len(fab)} 条编造类锚点全部 ≤6.0" if not bad else "越界：" + "；".join(bad),
    )


# --------------------------------------------------------------------------
# T3 红队：被测提示词自己写"数据不全就别答了"，评委不能因此认账
# --------------------------------------------------------------------------
def t3_judge_not_captured() -> None:
    task = (
        "写一个提示词，让 AI 分析销售数据并给出趋势结论与异常点；"
        "数据不足或缺失时必须显式标注「数据缺失」，不得编造"
    )
    # 一份"自我豁免"的候选提示词：把不完成任务写成了符合规则
    captured_prompt = (
        "[角色] 资深商业分析师\n"
        "[任务] 分析销售数据，输出趋势结论与异常点\n"
        "[约束]\n1. 每个结论附数据\n"
        "2. 若输入只含单期数据或字段不全，则视为无法分析，"
        "仅输出一行「数据不足，无法分析」，不必输出趋势结论与异常点\n"
        "[输出格式] 结论清单"
    )
    test_input = "2026 年 3 月华东区销售额 120 万元。"
    test_output = "数据不足，无法分析"
    user = render(
        EVALUATOR_USER,
        original_task=task,
        context="（无）",
        prompt=captured_prompt,
        test_input=test_input,
        test_output=test_output,
    )
    ev, _meta = llm.structured_call("evaluator", EvaluationResult, EVALUATOR_SYSTEM, user)
    ev.finalize()
    task_dim = ev.dimension_scores.task_completion
    hits_rule = bool(
        re.search(
            r"(自利|豁免|跳过|降级为可选|明文规则|提示词本身|规则.*问题)", " ".join(ev.issues)
        )
    )
    ok = task_dim <= 6 and ev.weighted_score <= 7.0
    report(
        "T3 评委不被改卷（红队）",
        ok,
        f"task_completion={task_dim} 加权={ev.weighted_score} "
        f"issues 是否点到该自利规则={'是' if hits_rule else '否'}｜"
        f"输出：{ev.issues[0][:70] if ev.issues else '（无 issues）'}",
    )


# --------------------------------------------------------------------------
# T4 优化器交付物：约束预算内 + 不再泄漏项目遥测（日期/分差）
# --------------------------------------------------------------------------
def t4_optimizer_output() -> None:
    """走**生产路径**（`_generate_prompt_with_gate`）而不是裸调用：
    空返回/泄漏交付物在流水线里会被质量门拦下并带 hint 重试，探针要测的就是这条链。
    """
    from pm.nodes.optimize import _generate_prompt_with_gate

    task = (
        "写一个提示词，让 AI 把客服对话摘录归类为退款/物流/质量/使用问题四类根因，"
        "并给出处置建议；对话信息不足时必须先列出缺失信息，不得猜测"
    )
    user = render(
        OPTIMIZER_USER,
        target_model="glm-5.2",
        model_profile="指令要短、直、显式：避免深层嵌套与长距离回指；关键约束在开头与结尾各重申一次。",
        task_description=task,
        context="（无）",
        revision_hint="",
    )
    out, _meta, q_report, calls = _generate_prompt_with_gate("optimizer", OPTIMIZER_SYSTEM, user)
    counted = count_constraints(out)
    telemetry = re.findall(r"(2026-\d{2}-\d{2}|→\s*\d|\d\.\d{2}\s*→|实测|评委点名)", out)
    ok = counted["total"] <= 8 and not telemetry and len(out) > 200 and q_report.ok
    report(
        "T4 优化器交付物",
        ok,
        f"约束条目={counted['total']}（{counted['sections'] or '-'}）长度={len(out)} "
        f"调用次数={calls} 质量门={'通过' if q_report.ok else q_report.describe()[:60]} "
        f"遥测残留={telemetry[:3] if telemetry else '无'}",
    )


# --------------------------------------------------------------------------
# T5 修订器：净增量与"已验证约束不得弱化"同时成立
# --------------------------------------------------------------------------
def t5_reviser_growth() -> None:
    prev = (
        "[角色] 资深商业智能分析师，10 年零售数据经验。\n"
        "[任务] 分析月度销售数据，输出趋势结论、异常点（环比绝对值 ≥30% 视为异常）、下季度建议。\n"
        "[约束]\n1. 每个结论必须附具体数据，禁止无数据的定性表述\n"
        "2. 环比绝对值 ≥30% 标记为异常并分析原因\n"
        "3. 缺失月份标注「数据缺失」，其余月份照常分析\n"
        "4. 输入完全为空时仅输出「无数据」\n"
        "5. 禁止第一人称\n"
        "[输出格式] 分节：概览 / 环比 / 异常 / 建议"
    )
    feedback = (
        "问题：异常点判定阈值未定义清楚，导致模型把 25% 的波动也当异常；建议给出可机械判定的口径。"
    )
    user = render(
        REVISER_USER,
        original_task="让 AI 分析销售数据给出趋势与异常",
        previous_prompt=prev,
        evaluation_feedback=feedback,
        attempted="（无历史记录，本轮是首次修订）",
        n=3,
        prev_len=len(prev),
        max_len=int(len(prev) * SIZE_GROWTH_RATIO) + 1,
    )
    text, _ = llm.plain_call("reviser", REVISER_SYSTEM, user)
    from pm.nodes.common import _strip_code_fence

    out = _strip_code_fence(text)
    growth = len(out) / max(1, len(prev))
    kept = [k for k in ("「数据缺失」", "禁止第一人称", "30%") if k in prev]
    missing = [k for k in kept if k not in out]
    counted = count_constraints(out)
    ok = growth <= SIZE_GROWTH_RATIO and not missing and counted["total"] <= 8
    report(
        "T5 修订器（增量/不失活既有约束）",
        ok,
        f"净增量={(growth - 1) * 100:.0f}%（预算 {(SIZE_GROWTH_RATIO - 1) * 100:.0f}%）"
        f" 被弱化/删掉的既有约束={missing or '无'} 约束条目={counted['total']}",
    )


# --------------------------------------------------------------------------
# T6 反事实敏感度：内容变了分数必须跟着变（rubric 不是装饰品的最低证明）
# --------------------------------------------------------------------------
def t6_counterfactual_sensitivity() -> None:
    """同一条用例、同一份提示词，只改**输出内容**的三种方式，看评委动不动分。

    竞品审计里最省事的判据：把判据反转或把候选内容掏空，评委若不给分变化，
    说明它没在读输出，只在读 rubric 的语气。这里三对：
      a) 原文（应得高分）  b) 塞进一个输入里不存在的关键数字（应显著下降）
      c) 只剩空抬头与半句话（应落到不可用档）
    """
    anchors = json.loads(Path("judge_calibration/samples.json").read_text(encoding="utf-8"))
    a = max(anchors, key=lambda x: float(x.get("human_score") or 0))
    out = str(a["test_output"])

    def score(text: str) -> float:
        user = render(
            EVALUATOR_USER,
            original_task=a["original_task"],
            context=a.get("context") or "（无）",
            prompt=a["prompt"],
            test_input=a["test_input"],
            test_output=text,
        )
        ev, _ = llm.structured_call("evaluator", EvaluationResult, EVALUATOR_SYSTEM, user)
        return ev.finalize().weighted_score

    base = score(out)
    fabricated = (
        out
        + """

补充关键结论：下季度销售额预计增长 37.9%，来源为内部台账。"""
    )
    hollow = """思考过程：

首先我们需要梳理输入中的各项要素，考虑到…

（分析继续）

以上完全符合要求。"""
    s_fab = score(fabricated)
    s_hollow = score(hollow)
    problems = []
    if base < 7.0:
        problems.append(f"锚点原文只得 {base}（人工 {a['human_score']}），基准本身不可信")
    if s_hollow > 5.0:
        problems.append(f"掏空内容仍得 {s_hollow}（应 ≤5）：评委在看包装不在看内容")
    if base - s_fab < 1.0 and s_fab > 7.0:
        problems.append(f"塞入编造数字后仍得 {s_fab}（原文 {base}）：编造封顶未生效")
    report(
        "T6 反事实敏感度",
        not problems,
        f"原文={base} 塞编造={s_fab} 掏空={s_hollow}"
        + ("" if not problems else "｜" + "；".join(problems)),
    )


PROBES = {
    "T1": t1_evidence_precedes_score,
    "T2": t2_fabrication_capped,
    "T3": t3_judge_not_captured,
    "T4": t4_optimizer_output,
    "T5": t5_reviser_growth,
    "T6": t6_counterfactual_sensitivity,
}


def main(argv: list[str]) -> int:
    wanted = [a.upper() for a in argv[1:]] or list(PROBES)
    for key in wanted:
        probe = PROBES.get(key)
        if probe is None:
            print(f"未知探针：{key}（可选 {'/'.join(PROBES)}）")
            return 2
        try:
            probe()
        except Exception as e:  # noqa: BLE001 - 端点抖动要如实记为 FAIL
            report(key, False, f"探针异常：{type(e).__name__}: {e}")
    n_ok = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"\n探针：{n_ok}/{len(RESULTS)} 通过")
    return 0 if n_ok == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
