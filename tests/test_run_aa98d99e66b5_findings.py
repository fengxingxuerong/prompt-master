"""这一轮的四个发现需要守卫，否则下轮又看不见。

① 失败调用占整轮 68% 的墙钟（§三十·二十二·二）
② 墙钟预算 600s 实际耗了 10581s（§三十·二十二·四）
③ §三十·二十一 的保护在真跑里被证明过（optimizer + reviser 各一次）
④ passed=True 但仍有评委缺席时，报告必须披露（§三十·二十二·一）

⚠️ 判据设计记录（两处被自己的输出打脸）：
  a) 墙钟那条第一版用 re.search 取**第一个**匹配，拿到的是 302s 那次
     正常耗尽（302 < 600），于是判"没有超预算"。真实情况是另一条
     10581s。⇒ 必须用 re.findall 取**最大**倍数。
  b) 披露那条第一版 import 了 `_judge_health`，但 report.py 里**没有**
     这个函数（降级披露写在主渲染里）。⇒ 改为渲染整份报告再断言。
"""

import json
import re
from pathlib import Path

import pytest

LOGS = Path(__file__).resolve().parents[1] / "logs"
RUN_ID = "aa98d99e66b5"


def _d() -> dict:
    p = LOGS / f"run_{RUN_ID}.json"
    if not p.exists():
        pytest.skip("归档不在本机")
    return json.loads(p.read_text(encoding="utf-8"))


def test_failed_calls_dominated_the_run() -> None:
    """失败耗时占整轮 68%——§三十·十八 修好之前这个数**不可见**。

    判据钉的是**比例**：不同轮次规模不同，但只要失败仍是大头，
    就说明"账本记了但没人看"这个坑还在。
    """
    u = _d()["llm_usage"]
    fail_ms = sum(int(v.get("failed_latency_ms") or 0) for v in u.values())
    ok_ms = sum(int(v.get("latency_ms") or 0) for v in u.values())
    assert fail_ms > ok_ms, (
        f"这轮失败耗时 {fail_ms} 反而少于成功耗时 {ok_ms}，§三十·二十二·二 要重写"
    )
    ratio = fail_ms / (fail_ms + ok_ms)
    assert ratio > 0.5, f"失败占比 {ratio:.0%}，不再是大头，结论需重新评估"


def test_wall_clock_budget_was_overrun_17x() -> None:
    """墙钟预算 600s，实际 10581s——预算**没有**兜住连接层挂起。

    ⚠️ 必须取**最大**倍数：同轮还有两次"正常"耗尽（302s/520s，预算 600s），
    取第一个匹配会拿到 302 < 600，误判成"没超"。
    """
    errs = " ".join(str(e) for e in (_d().get("errors") or []))
    hits = re.findall(r"墙钟预算 (\d+)s 用尽（实际 (\d+)s", errs)
    assert hits, f"没有墙钟耗尽记录：{errs[:200]}"

    worst = max((int(a) / int(b), int(b), int(a)) for b, a in hits)
    ratio, budget, actual = worst
    assert ratio > 10, (
        f"最大超预算只有 {ratio:.1f} 倍（{actual}s / {budget}s），若真修好了请更新本条判据"
    )


def test_empty_retry_actually_fired_in_a_real_run() -> None:
    """§三十·二十一 的保护在真跑里被证明过（optimizer 与 reviser 各一次）。

    这是"工程性保护值不值"的唯一真答案：救回了整轮。
    没有 optimizer 那次重试，run_41a833fb9b41 那种结局就会重演。
    """
    d = _d()
    u = d.get("llm_usage") or {}
    assert d.get("status") == "passed", "这轮不是 passed，§三十·二十一 的结论要重看"
    assert int((u.get("optimizer") or {}).get("calls") or 0) >= 2, (
        "optimizer 只调了 1 次，说明没触发重试，§三十·二十一 未被证明"
    )
    assert int((u.get("reviser") or {}).get("calls") or 0) >= 2, (
        "reviser 没触发 §三十·二十一 的新保护"
    )


def test_passed_score_is_disclosed_as_degraded() -> None:
    """passed=True 时若仍有评委缺席，**必须**在报告里披露。

    这轮 avg 8.37 但 3/4 例评委缺席——不披露就等于骗人。
    """
    from pm.report import render_report

    d = _d()
    errs = " ".join(str(e) for e in (d.get("errors") or []))
    assert "墙钟" in errs or "empty_output" in errs, "这轮没有缺席记录，判据前提没了"

    # render_report 返回 (markdown, 附加数据) 二元组，不是纯 markdown 列表
    rendered = render_report(d)
    text = rendered[0] if isinstance(rendered, tuple) else "\n".join(rendered)
    assert "降级" in text, "有评委缺席却没披露降级"
    assert "不是" in text and "共识" in text, "没明说这些分不是双评委共识"
