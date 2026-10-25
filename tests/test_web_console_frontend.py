"""控制台两个"看不见"的前端缺陷：历史面板的注入面、版本曲线的 null 崩溃。

两条都是**Python 侧全绿时也成立**的缺陷（2026-10-31 前端审计）：

1. `renderHistory` 里有 4 处裸拼 `innerHTML`，而 `runs[].task` 就是**任务原文**。
   其余 30 处都走 `esc()`，只有这里没走 —— 一条形如 `<b>✅ 显著 Δ+2.00</b>`
   或 `</td></tr><tr><td>…` 的任务名就能伪造结论、插假行。CSP 的 `script-src`
   只挡"执行脚本"，挡不住"让页面说谎"。
2. `drawCurve` 直接 `scores[i].toFixed(1)`，而 `revise` 刚 append 的那一版
   `avg_score` 是 null ⇒ `TypeError`，被 `poll` 的 `catch(_){}` 静默吞掉：
   症状是"图表空白 + 左栏停止刷新 + 零提示"。

第 1 条用源码级判据（这个仓库有同类先例：`pm/web/_js.py` 的 XSS 防线就是按写法钉的），
第 2 条用 node 真跑（有 canvas 上下文桩，不需要 DOM）。
"""

from __future__ import annotations

import re
import shutil
import subprocess

import pytest
from pm import web as web_mod

WEB_SRC = web_mod.WEB_CONSOLE_HTML

# 历史面板会碰到的"外部字符串"字段。它们要么来自任务原文，要么来自 run 归档。
_UNTRUSTED_HISTORY_FIELDS = (
    "g.task",
    "g.note",
    "g.judge_gen",
    "r.task",
    "r.run_id",
    "r.mtime",
    "r.status",
    "r.llm_calls",
)


def _history_js() -> str:
    m = re.search(r"^async function renderHistory\(\)\{[\s\S]*?^\}", WEB_SRC, re.M)
    assert m, "没找到 renderHistory（pm/web/_js.py 结构变了，本用例需同步更新）"
    return m.group(0)


@pytest.mark.parametrize("field", _UNTRUSTED_HISTORY_FIELDS)
def test_history_escapes_every_untrusted_field(field: str) -> None:
    """每个外部字段都必须经 `esc(...)` 进 DOM：把 esc 调用剥掉后，它不该还留在源码里。

    不要求写成 `esc(field)` 这一种形态（可能是 `esc(a ? b : "")`），只要求
    **它至少出现在一处 esc() 调用里**，且剥掉所有 esc() 后不再有裸用法。
    """
    js = _history_js()
    esc_calls = re.findall(r"esc\(([^()]*)\)", js)
    assert any(field in call for call in esc_calls), f"历史面板里 {field} 没有任何一处经过 esc()"
    without_esc = re.sub(r"esc\([^()]*\)", "", js)
    assert field not in without_esc, f"历史面板里 {field} 有一处没包 esc()：注入面"


def test_history_has_no_raw_interpolation_left() -> None:
    """兜底：剥掉 `esc(...)` / `Number(...)` 之后，历史面板里不许再剩下未转义的 `${{...}}`。

    允许多种写法（三元、拼接、局部量），判据是"这段插值里除了已转义的内容和局部量，
    没有别的表达式"。
    """
    js = _history_js()
    allowed_locals = {
        "mark",
        "lo",
        "hi",
        "dm",
        "ba",
        "oa",
        "dl",
        "gTask",
        "gNote",
        "gGen",
        "gN",
        "",
    }
    bad = []
    for expr in re.findall(r"\$\{([^{}]*)\}", js):
        residue = re.sub(r"(?:esc|Number)\([^()]*\)", "", expr)
        residue = re.sub(r'"[^"]*"', "", residue)  # 字面量（如三元的 "，" 分支）
        residue = re.sub(r"[?:+()\s]|==|!=|>=|<=|&&|\|\|", "", residue)
        if residue in allowed_locals or residue == "":
            continue
        bad.append(expr.strip())
    assert not bad, f"历史面板里还有未转义的插值：{bad}"


def test_chat_card_shows_the_failure_reason() -> None:
    """失败时控制台必须给出原因，而不是只有一块红 badge。

    实测缺陷（2026-10-31 审计）：卡片只渲染 status/iteration/llm_calls/均分/早停，
    `StatusResponse.error` 一个字都不读——无 Key / 配额打满 / 端点点挂时用户只能去翻 logs/，
    而异常路径没有 final_report，「加载完整报告」只会 404。
    """
    m = re.search(r"^function renderChat\(r\)\{[\s\S]*?^\}", WEB_SRC, re.M)
    assert m, "没找到 renderChat"
    js = m.group(0)
    assert "esc(r.error)" in js, "失败的 error 字段没被渲染"
    assert "esc((r.errors||[])" in js, "节点级错误明细没被渲染"


# ------------------------------------------------------------------ drawCurve
_NODE = shutil.which("node")

_CURVE_DRIVER = r"""
const ctxStub = new Proxy({}, { get: () => () => {}, set: () => true });
const canvasStub = { width: 0, height: 0, style: {}, getContext: () => ctxStub };
globalThis.$ = () => canvasStub;
globalThis.window = { devicePixelRatio: 1 };

__JS__

const cases = [
  [8.5, null, 8.2],     // revise 刚 append 的版本还没分数
  [null, null],         // 全都没有分数
  [],                   // 空列表
  [8.5, undefined, 8.2, NaN],
];
for (const c of cases) {
  drawCurve("curve", c);
}
console.log("drawCurve-ok");
"""


@pytest.mark.skipif(_NODE is None, reason="本机没有 node，跳过前端行为检查")
def test_draw_curve_survives_missing_scores() -> None:
    """`avg_score` 为 null 的版本不许让曲线抛错（旧版会 TypeError 并被静默吞掉）。"""
    m = re.search(r'^JS_CANVAS = r"""(.*?)^"""', _source(), re.S | re.M)
    assert m, "没找到 JS_CANVAS 字面量"
    driver = _CURVE_DRIVER.replace("__JS__", m.group(1))
    proc = subprocess.run(
        [_NODE, "-e", driver],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert proc.returncode == 0, f"drawCurve 在缺分数时抛错：{proc.stderr[:600]}"
    assert "drawCurve-ok" in proc.stdout


def _source() -> str:
    """读 pm/web/_js.py 原文（要常量字面量本身，不是组装后的页面）。"""
    from pathlib import Path

    import pm.web._js as js_mod

    return Path(js_mod.__file__).read_text(encoding="utf-8")
