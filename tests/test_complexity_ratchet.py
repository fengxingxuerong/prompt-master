"""复杂度只许降不许升：把每一轮拆小函数的收益钉进台账。

为什么不直接用 ruff 的 `max-complexity` 死线：那是一条"要么全拆完、要么整条规则不开"
的线。本轮实测有 33 个函数超过 CC 10，最狠的 28 —— 定死一条线要么今天就把 33 个函数
全拆掉（不现实），要么干脆不开（那"CI 绿"里就没有复杂度这一项）。
所以这里改成**逐函数的预算表**：超过地板值的每个函数都要在台账里登记它当前的圈复杂度，
台账数字必须与实测**逐字相等**。

四条判据（缺一不可）：

1. 新出现的复杂函数 → 红。想让它变绿只有两条路：拆，或者在台账里承认
   "这里就是这么复杂，并且说明为什么先留着"。
2. 某个函数变复杂了 → 红，同上。
3. 函数变简单了、或被改名/删掉了，而台账没跟着改 → 红。这条是防"台账变成许愿池"的：
   只记录不回收的台账两三轮之后就会漂成一份谁都不信的清单。
4. 任何函数超过 `_CEILING` → 红，即使台账里写了。台账不能给新的怪物发通行证。

判据来自 ruff 自己的 mccabe 计数（subprocess 调 `--select C901`），不另写一套 AST，
否则"这个函数到底几分复杂度"会出现第三口径。作用范围取自 ci.yml 的 `ruff check` 那一步：
CI 查哪儿这里就量哪儿，CI 改了范围这里自动跟着改。

重新生成台账（只在人手上执行；测试自己从不写被跟踪文件，CI 里那条
`git diff --exit-code` 才不会莫名变红）：
    python tests/test_complexity_ratchet.py --rewrite
"""

from __future__ import annotations

import functools
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEDGER = Path(__file__).with_name("complexity_budget.json")

# 地板：CC ≤ 10 的函数不进台账（与 ruff 默认 max-complexity 同一档位）。
_FLOOR = 10
# 硬顶：本轮实测最高值。**抬这个数需要一个单独的决定**——它的意思不是"以后允许这么复杂"，
# 而是"我们接受了一个更复杂的存量函数，并且愿意让新代码长到那个程度"。降它不需要理由。
_CEILING = 28

_LINE_RE = re.compile(
    r"^(?P<path>[^\s]+):\d+:\d+: C901 `(?P<name>[^`]+)` is too complex \((?P<cc>\d+)"
)


def _ci_lint_scope() -> list[str]:
    """从 ci.yml 的 `ruff check` 那一步取作用范围（与门禁同源，不另立口径）。"""
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    m = re.search(r"run:\s*ruff check\s+(.+)", ci)
    assert m, "ci.yml 里找不到 `ruff check` 那一步，台账的作用范围该跟着改"
    return [t for t in m.group(1).split() if not t.startswith("-")]


@functools.lru_cache(maxsize=1)
def _measure() -> dict[str, int]:
    """ruff 实测的 {`文件::函数名`: 圈复杂度}，只保留超过地板的。

    同一文件里出现同名函数（内层重名、条件定义）时取**较大值**：键里不放行号，
    因为行号会随任何一次编辑漂掉，那样台账每轮都得重抄一遍。
    """
    out = subprocess.run(
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--select",
            "C901",
            "--config",
            "lint.mccabe.max-complexity=1",
            "--output-format",
            "concise",
            "-q",
            *_ci_lint_scope(),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    # 命中就是 rc=1，所以这里不能用 rc 当判据（否则第一次有人把函数拆干净、
    # 输出为空，测试会把它读成"ruff 挂了"）。要防的是 ruff 自己没跑起来：
    # 那会在 stderr 里留下用法错误，而 stdout 一行都没有。
    if not (out.stdout or "").strip():
        raise AssertionError(
            f"ruff 没有输出任何复杂度行（rc={out.returncode}），台账无法核对："
            f"{(out.stderr or '')[:400]}"
        )
    budget: dict[str, int] = {}
    for raw in out.stdout.splitlines():
        m = _LINE_RE.match(raw.strip())
        if not m:
            continue
        cc = int(m.group("cc"))
        if cc <= _FLOOR:
            continue
        # 路径分隔符归一：Windows 上 ruff 打反斜杠、CI 的 linux 上打正斜杠，
        # 不归一的话同一份台账在两个平台上会各自"漂"一遍。
        key = m.group("path").replace("\\", "/") + "::" + m.group("name")
        budget[key] = max(budget.get(key, 0), cc)
    return budget


def _ledger() -> dict[str, int]:
    assert LEDGER.is_file(), (
        f"复杂度台账不存在：{LEDGER.relative_to(ROOT)}"
        "（先跑 python tests/test_complexity_ratchet.py --rewrite）"
    )
    data = json.loads(LEDGER.read_text(encoding="utf-8"))
    assert isinstance(data, dict), "台账必须是指向整数的 JSON 对象"
    return {str(k): int(v) for k, v in data.items()}


def test_complexity_never_rises_and_the_ledger_is_exact() -> None:
    live = _measure()
    booked = _ledger()

    worse = {k: (booked[k], v) for k, v in live.items() if k in booked and v > booked[k]}
    new = {k: v for k, v in live.items() if k not in booked}
    improved = {k: (booked[k], v) for k, v in live.items() if k in booked and v < booked[k]}
    gone = sorted(set(booked) - set(live))
    over = {k: v for k, v in live.items() if v > _CEILING}

    problems: list[str] = []
    if worse:
        problems.append(
            "这些函数变复杂了（台账 → 实测）：\n  "
            + "\n  ".join(f"{k}: {a} → {b}" for k, (a, b) in sorted(worse.items()))
            + "\n要么拆，要么在台账里承认并说明为什么先留着。"
        )
    if new:
        problems.append(
            "新出现的高复杂度函数（台账里没有）：\n  "
            + "\n  ".join(f"{k}: {v}" for k, v in sorted(new.items()))
            + f"\n超过 CC {_FLOOR} 的新函数要先登记——这一步就是要让人停下来想一下。"
        )
    if improved:
        problems.append(
            "这些函数已经变简单了，台账还停在旧值（台账 → 实测）：\n  "
            + "\n  ".join(f"{k}: {a} → {b}" for k, (a, b) in sorted(improved.items()))
            + "\n收益要当场钉住，否则台账会变成一份没人信的清单。"
        )
    if gone:
        problems.append(
            "台账里的这些函数已经不存在（改名或删除后请同步删掉这一条）：\n  " + "\n  ".join(gone)
        )
    if over:
        problems.append(
            f"超过硬顶 {_CEILING} 的函数：\n  "
            + "\n  ".join(f"{k}: {v}" for k, v in sorted(over.items()))
            + "\n抬硬顶是一个单独的决定：它等于宣布「以后新写的函数也可以长到这个程度」。"
        )
    assert not problems, "\n\n".join(problems)


def _rewrite() -> int:
    live = _measure()
    LEDGER.write_text(
        json.dumps(dict(sorted(live.items())), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(f"已写入 {len(live)} 条：{LEDGER}")
    return 0


if __name__ == "__main__":
    if "--rewrite" not in sys.argv:
        raise SystemExit("用法：python tests/test_complexity_ratchet.py --rewrite")
    raise SystemExit(_rewrite())
