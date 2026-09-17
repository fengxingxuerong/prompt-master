"""--selftest：拓扑与控制流自检（假后端，不联网）。（自 run.py 拆出，逻辑逐字保留）"""

from __future__ import annotations

from pm.graph import build_app
from pm.state import initial_state

from .support import recursion_budget


# --------------------------------------------------------------------------
# 自检
# --------------------------------------------------------------------------
def selftest() -> int:
    """拓扑与控制流自检。

    说明：这里用**假后端**替换 LLM 调用，只验证图能正确流转、状态能正确累加、
    评分聚合与迭代终止逻辑正确。**它不验证提示词优化的实际效果**——
    效果必须由配置真实 API Key 后的 e2e 运行来验证。
    """
    from pm import testing
    from pm.nodes import _samples_per_case

    n_samples = _samples_per_case()
    print("=" * 60)
    print("自检模式：验证图拓扑与控制流（不验证优化效果）")
    print("=" * 60)
    print(f"  采样口径：每条用例重复 {n_samples} 次（PM_SAMPLES_PER_CASE）")

    with testing.fake_backend(scenario="progress"):
        app = build_app()
        init = initial_state(
            task="让 AI 分析销售数据",
            target_model="fake-target",
            n_test_cases=3,
            max_iterations=3,
            auto_clarify=True,
        )
        config = {"configurable": {"thread_id": "selftest"}, "recursion_limit": recursion_budget(3)}
        final = app.invoke(init, config)

    checks = [
        ("图执行完成且到达 report", final.get("final_report") != ""),
        ("生成了 3 条测试用例", len(final.get("test_cases", [])) == 3),
        (
            "每条用例按采样数重复执行",
            len(final.get("test_runs", [])) == 3 * n_samples,
        ),
        ("评估产生结果", len(final.get("evaluations", [])) == 3),
        ("trace 已累加（未被覆盖）", len(final.get("trace", [])) >= 6),
        ("触发了修订迭代", final.get("iteration", 0) >= 1),
        ("迭代未超过上限", final.get("iteration", 0) <= 3),
        ("状态为终态", final.get("status") in ("passed", "max_iterations", "failed")),
        ("记录了多个提示词版本", len(final.get("prompt_versions", [])) >= 2),
    ]

    ok = True
    for name, passed in checks:
        mark = "PASS" if passed else "FAIL"
        print(f"  [{mark}] {name}")
        ok = ok and passed

    print("\n关键运行指标：")
    print(f"  iteration   = {final.get('iteration')}")
    print(f"  status      = {final.get('status')}")
    print(f"  llm_calls   = {final.get('llm_calls')}")
    print(f"  trace 条数  = {len(final.get('trace', []))}")
    agg = final.get("aggregate") or {}
    print(f"  avg/min     = {agg.get('avg_score')} / {agg.get('min_score')}")
    print(f"  版本数      = {len(final.get('prompt_versions', []))}")

    print("\n节点执行顺序：")
    for t in final.get("trace", []):
        print(f"  iter{t.get('iteration')} {t.get('node'):<10} {t.get('event')}")

    # ---- 场景二：始终不达标，验证迭代上限兜底 ----
    print("\n" + "=" * 60)
    print("场景二：始终不达标 —— 验证迭代上限后的兜底交付")
    print("=" * 60)
    with testing.fake_backend(scenario="stall"):
        app2 = build_app()
        init2 = initial_state(
            task="让 AI 分析销售数据",
            target_model="fake-target",
            n_test_cases=3,
            max_iterations=2,
            auto_clarify=True,
        )
        cfg2 = {"configurable": {"thread_id": "selftest-stall"}, "recursion_limit": 100}
        final2 = app2.invoke(init2, cfg2)

    stall_checks = [
        ("达到上限后状态为 max_iterations", final2.get("status") == "max_iterations"),
        ("迭代次数被限制在上限内", final2.get("iteration", 0) == 2),
        ("仍然生成了交付报告", bool(final2.get("final_report"))),
        ("报告中如实标注未达标", "未达标" in (final2.get("final_report") or "")),
        ("记录了全部版本", len(final2.get("prompt_versions", [])) == 3),
    ]
    for name, passed in stall_checks:
        mark = "PASS" if passed else "FAIL"
        print(f"  [{mark}] {name}")
        ok = ok and passed

    print(f"\n  iteration={final2.get('iteration')}  status={final2.get('status')}")

    print("\n" + "=" * 60)
    print("场景三：双评委分歧 —— 验证仲裁路径与如实交付")
    print("=" * 60)
    with testing.fake_backend(scenario="dispute"):
        app3 = build_app()
        init3 = initial_state(
            task="分析销售数据",
            target_model="fake-target",
            n_test_cases=2,
            max_iterations=1,
            auto_clarify=True,
        )
        cfg3 = {"configurable": {"thread_id": "selftest-dispute"}, "recursion_limit": 100}
        final3 = app3.invoke(init3, cfg3)

    dispute_evals = [e for e in final3.get("evaluations", []) if e.get("judge") == "arbiter"]
    dispute_checks = [
        (
            "状态为终态",
            final3.get("status") in ("passed", "max_iterations", "failed", "early_stopped"),
        ),
        ("双评委分歧触发了仲裁", len(dispute_evals) > 0),
        (
            "仲裁结果保留了分差信息",
            all(e.get("judge_disagreement") is not None for e in dispute_evals),
        ),
        ("报告如实标注未达标", "未达标" in (final3.get("final_report") or "")),
    ]
    for name, passed in dispute_checks:
        mark = "PASS" if passed else "FAIL"
        print(f"  [{mark}] {name}")
        ok = ok and passed
    print(
        f"\n  仲裁用例数 = {len(dispute_evals)}  分差 = "
        f"{dispute_evals[0].get('judge_disagreement') if dispute_evals else '-'}"
    )

    print("\n" + ("自检通过" if ok else "自检失败"))
    return 0 if ok else 1
