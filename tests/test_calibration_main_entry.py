"""`pm/calibration/` 的 `main()` 尾段与 `__main__` 守卫回归（零出网，评委全桩）。

为什么单独一个文件：`operations.md` 薄弱点清单（2026-10-02 实测）记过
"calibration 的大块是文件末尾那段 CLI `__main__` 入口……能用假上游/桩补"。
2026-10-11 实测该文件 91%，miss 集中在 `main()` 的样本加载之后（1381-1412：
加载失败 / 空样本提示 / pending 出处披露 / 报告渲染 / `--repeat` 分支）与
`if __name__` 守卫两行（1418-1419）。现有测试只覆盖到 `--write-template` 路径。

桩的边界：`calibrate` / `repeatability` / `render_report` / `render_repeatability`
全部替身——真函数会打评委端点；`load_samples` 在加载失败那条用真函数打假路径，
其余用真 `Anchors`（保持 `.pending` 语义），`render_provenance` 保持真实现。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
from pm import calibration as C


def _run_main(
    monkeypatch,
    argv: list[str],
    *,
    samples: Any,
    analysis: dict | None = None,
    errors: list | None = None,
    repeat_report: dict | None = None,
) -> int:
    """替身注入后跑 main()：返回退出码，捕获 stdout/stderr 交给用例自行断言。

    `analysis=None` 时替身返回 {"n": len(samples)}；显式传 {} 可走"全部失败"路径。
    """
    monkeypatch.setattr(sys, "argv", ["pm.calibration", *argv])
    monkeypatch.setattr(C, "load_samples", lambda path: samples)
    monkeypatch.setattr(
        C,
        "calibrate",
        lambda s, judge, mode: ({"n": len(s)} if analysis is None else analysis, errors or []),
    )
    monkeypatch.setattr(C, "render_report", lambda judge, a: "REPORT")
    monkeypatch.setattr(C, "repeatability", lambda s, judge, n, mode: repeat_report or {})
    monkeypatch.setattr(C, "render_repeatability", lambda rep: "REPEAT")
    return C.main()


def test_load_failure_returns_2(monkeypatch, capsys) -> None:
    """样本加载失败：退出码 2，错误进 stderr 而不是 traceback。"""

    def boom(path: Path) -> Any:
        raise ValueError("顶层必须是数组")

    monkeypatch.setattr(sys, "argv", ["pm.calibration", "--samples", "x.json"])
    monkeypatch.setattr(C, "load_samples", boom)
    assert C.main() == 2
    assert "样本加载失败" in capsys.readouterr().err


def test_empty_samples_gets_a_hint(monkeypatch, capsys) -> None:
    """空样本：退出码 2，且 hint 指向 --write-template（没有 pending 时）。"""
    rc = _run_main(monkeypatch, ["--samples", "x.json"], samples=C.Anchors([]))
    assert rc == 2
    assert "--write-template" in capsys.readouterr().err


def test_pending_provenance_and_skip_errors_are_disclosed(monkeypatch, capsys) -> None:
    """有 pending：出处披露进 stderr；评估错误逐条 [SKIP]；正常渲染报告后 0 退出。"""
    samples = C.Anchors([{"id": "a1"}, {"id": "a2"}])
    samples.pending = [{"id": "p1", "confirmed": False}]
    rc = _run_main(
        monkeypatch,
        ["--samples", "x.json"],
        samples=samples,
        errors=[("a1", "评委超时")],
    )
    out = capsys.readouterr()
    assert rc == 0
    assert "1 条未经人工确认" in out.err  # 出处披露点到了 pending 的数量
    assert "[SKIP] a1：评委超时" in out.err
    assert "REPORT" in out.out


def test_repeat_branch_renders_repeatability(monkeypatch, capsys) -> None:
    """--repeat >= 2：复现性结果并入 analysis 并渲染（不渲染就是白打 N 倍调用）。"""
    samples = C.Anchors([{"id": "a1"}])
    samples.pending = []
    rc = _run_main(
        monkeypatch,
        ["--samples", "x.json", "--repeat", "2"],
        samples=samples,
        repeat_report={"n": 2, "range_mean": 0.4},
    )
    assert rc == 0
    assert "REPEAT" in capsys.readouterr().out


def test_all_samples_failed_returns_1(monkeypatch, capsys) -> None:
    """全部锚点评估失败：退出码 1，提示先跑 preflight，不渲染空报告。"""
    rc = _run_main(monkeypatch, ["--samples", "x.json"], samples=_samples(1), analysis={})
    assert rc == 1
    assert "全部锚点评估失败" in capsys.readouterr().err


def _samples(n: int) -> Any:
    samples = C.Anchors([{"id": f"a{i}"} for i in range(n)])
    samples.pending = []
    return samples


def test_main_guard_executes_under_main_name() -> None:
    """`if __name__ == "__main__"` 守卫两行：以 __main__ 之名 exec 本模块，
    ensure_utf8_stdio + SystemExit(main()) 都要走一遍。--help 退出 0，不碰端点。
    相对导入要靠 `__package__` 解析，否则 exec 出的名字空间不是包内成员。"""
    # 2026-10-19 拆包：守卫在 pm/calibration/__main__.py，`__package__` 也随之深一层。
    # 原来读 C.__file__（= __init__.py）并把 __package__ 设成 "pm"，拆包后
    # `from .history import` 会被解析成 `pm.history` 而 ModuleNotFoundError —— 这条用例
    # 正是"测试记住旧世界"的典型，随包结构一起改。
    main_file = Path(C.__file__).with_name("__main__.py")
    code = compile(main_file.read_text(encoding="utf-8"), str(main_file), "exec")
    globs: dict[str, Any] = {
        "__name__": "__main__",
        "__file__": str(main_file),
        "__package__": "pm.calibration",
    }
    monkey = pytest.MonkeyPatch()
    try:
        monkey.setattr(sys, "argv", ["pm.calibration", "--help"])
        with pytest.raises(SystemExit) as ei:
            exec(code, globs)
    finally:
        monkey.undo()
    assert ei.value.code == 0
