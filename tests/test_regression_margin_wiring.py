"""`REGRESSION_MARGIN` 必须真的参与回退判定（2026-10-21）。

## 缺陷（与 2026-10-17 那个 `_MAX_EVENTS` 同族：注释承诺了，代码没做）

`pm/schemas.py` 里：

    REGRESSION_MARGIN = 0.2  # 回退判定：当前轮 avg 低于历史最佳超过该值

而 `early_stop_reason()` 的 docstring 也明写：

    余量 = max(REGRESSION_MARGIN, 2×noise)：噪声大时不能拿 0.2 当"真实回退"的依据。

但实现第 403 行用的是 `margin = noise_margin(noise)`，而 `noise_margin()` 内部用的是
**`PLATEAU_MARGIN`**（第 378-379 行）—— `REGRESSION_MARGIN` 从头到尾**没有被读过**。

## 为什么它是真缺陷，而不是"反正值一样"

两个常量当前都是 `0.2`，所以**行为暂时没有差异** —— 这正是它危险的地方：
值相同把缺陷完全掩盖了。一旦有人按注释意图把 `REGRESSION_MARGIN` 调成 0.5
（"回退判定要比平台期更保守"是完全合理的诉求），回退判定**一点都不会变**，
而改的人会以为改生效了。本仓对这种"改了没反应"的静默失效有明确纪律
（`_MAX_EVENTS` 那轮：删承诺而不是补实现；这轮相反 —— 这个承诺**值得兑现**）。

## 判据（两条，都取行为而非字面量）

1. **回退用 `REGRESSION_MARGIN`**：把 `REGRESSION_MARGIN` 调大到一个能区分的值，
   回退判定必须跟着变（构造一条"按 0.2 会判回退、按 0.5 不判"的分数轨迹）。
2. **平台期仍用 `PLATEAU_MARGIN`**：不能因为统一余量就把平台期也一起改了 ——
   两者语义不同（回退是"降了就别修了"，平台期是"两轮没涨"），共用一个常量会让
   将来任何一侧的调整都牵动另一侧。

⚠️ 判据刻意**不**断言"实现里出现 `REGRESSION_MARGIN` 这个字符串"：那样改注释也能过。
"""

from __future__ import annotations

import ast

import pytest
from pm import schemas


def test_regression_margin_actually_drives_the_regression_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """调大 `REGRESSION_MARGIN` 必须让回退判定变宽松 —— 否则它就是死常量。"""
    # 8.6 → 8.3：比历史最佳低 0.3。
    # 余量 0.2 ⇒ 0.3 > 0.2 判"回退"；余量 0.5 ⇒ 0.3 < 0.5 不判回退。
    scores = [8.6, 8.3]

    monkeypatch.setattr(schemas, "REGRESSION_MARGIN", 0.2)
    strict = schemas.early_stop_reason(scores, noise=0.0)
    assert strict is not None and "回退" in strict, (
        f"余量 0.2 时 8.6→8.3（差 0.3）应判回退，实际：{strict!r}"
    )

    monkeypatch.setattr(schemas, "REGRESSION_MARGIN", 0.5)
    loose = schemas.early_stop_reason(scores, noise=0.0)
    assert loose is None, (
        f"把 REGRESSION_MARGIN 调到 0.5 后，差 0.3 不该再判回退，实际：{loose!r} —— "
        "说明回退判定根本没读这个常量（它被 PLATEAU_MARGIN 架空了）"
    )


def test_plateau_margin_still_drives_the_plateau_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """反向护栏：平台期仍归 `PLATEAU_MARGIN` 管，不许被"统一余量"顺手改掉。

    构造：8.50 → 8.45 → 8.44。三轮都没超过 8.50。
      · 余量 0.2：`cur(8.44) <= 8.50+0.2` 且 `8.45 <= 8.50+0.2` ⇒ **判平台期**
      · 余量 0.02：`cur(8.44) <= 8.50+0.02` 成立，但 `8.45 <= 8.52` 也成立 ⇒ 仍判平台期
      · 因此改余量看不出区别 —— 这正是 PLATEAU_MARGIN 的作用点不在"是否判"，
        而在"判出来的余量数值"。用**报错文案里的余量数字**做判据更稳。
    """
    scores = [8.5, 8.45, 8.44]

    monkeypatch.setattr(schemas, "PLATEAU_MARGIN", 0.2)
    monkeypatch.setattr(schemas, "REGRESSION_MARGIN", 0.2)
    msg = schemas.early_stop_reason(scores, noise=0.0)
    assert msg is not None and "平台期" in msg, f"三轮未提升应判平台期，实际：{msg!r}"
    assert "余量 0.2" in msg, f"平台期余量应来自 PLATEAU_MARGIN(0.2)，实际文案：{msg!r}"

    # 改 PLATEAU_MARGIN ⇒ 文案里的余量必须跟着变（若仍写 0.2 说明它没被读）
    monkeypatch.setattr(schemas, "PLATEAU_MARGIN", 0.35)
    msg2 = schemas.early_stop_reason(scores, noise=0.0)
    assert msg2 is not None and "余量 0.35" in msg2, (
        f"PLATEAU_MARGIN 改成 0.35 后平台期余量应为 0.35，实际：{msg2!r} —— "
        "平台期余量没被 PLATEAU_MARGIN 驱动"
    )

    # 而回退余量不许跟着变（两个常量各管一段）
    assert "余量 0.35" not in (schemas.early_stop_reason([8.6, 8.3], noise=0.0) or ""), (
        "回退判定的余量被 PLATEAU_MARGIN 影响了 —— 两个判定又共用一个常量了"
    )


def test_noise_margin_is_not_silently_shared_by_both_checks() -> None:
    """`noise_margin()` 的语义是"平台期余量"，不该被当成回退余量直接复用。

    这条钉的是**结构性**事实：两个判定各有各的常量，`noise_margin` 只服务平台期。
    它不涉及具体数值，只保证"将来有人改一侧不会静默牵动另一侧"。
    """
    src = (schemas.__file__ or "").replace(".pyc", ".py")
    with open(src, encoding="utf-8") as fh:
        text = fh.read()
    assert "REGRESSION_MARGIN" in text, "REGRESSION_MARGIN 不见了 —— 本文件的理由也跟着失效"

    tree = ast.parse(text)
    funcs = {n.name: n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert "regression_margin" in funcs, (
        "没有独立的 regression_margin() —— 回退余量只能又被别的函数顺带算掉"
    )

    # noise_margin 的**可执行体**（不含 docstring）里只许出现 PLATEAU_MARGIN / UNESTIMATED_MARGIN
    def _exec_body(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
        """取函数体源码但**剥掉首行 docstring** —— 说明文字里提历史不算违规。"""
        seg = ast.get_source_segment(text, fn) or ""
        doc = ast.get_docstring(fn, clean=False)
        if doc:
            # 去掉 docstring 的引号包裹部分（含三引号）
            for quote in ('"""', "'''"):
                wrapped = f"{quote}{doc}{quote}"
                if wrapped in seg:
                    return seg.replace(wrapped, "", 1)
        return seg

    body = _exec_body(funcs["noise_margin"])
    assert "REGRESSION_MARGIN" not in body, (
        "noise_margin() 里出现了 REGRESSION_MARGIN —— 两个判定的余量又混在一起了"
    )
    # 反向：regression_margin 里不许出现 PLATEAU_MARGIN
    reg_body = _exec_body(funcs["regression_margin"])
    assert "PLATEAU_MARGIN" not in reg_body, (
        "regression_margin() 里出现了 PLATEAU_MARGIN —— 又混回去了"
    )
