"""pre-commit 与 CI 的**门禁集合**必须对齐（本模块负责"集合"，不是"路径"）。

## 与已有守卫的分工

`tests/test_packaging.py::test_precommit_hooks_cover_the_same_scope_as_ci`
比的是**范围**（ruff / format / mypy 各检查哪些路径）。
本模块问的是另一个问题：**这些门禁各自存在吗**。

## 起因（2026-10-05 实测，不是推测）

把 CI 的 12 条门禁逐条对到 `.pre-commit-config.yaml`：

```
gate                        CI     pre-commit
lint / format / types       True   True
pytest                      True   True
prompt structure            True   True
prompt gate                 True   False   ←
selftest                    True   False   ←
releases suites             True   False   ←
data clean                  True   False   ←
modelhub coverage           True   False   ←
stub e2e                    True   False   ←
```

**pre-commit 只有 4 条钩子，CI 有 12 条门禁**，而配置文件头一行写着
"与 .github/workflows/ci.yml **同口径**的本地版"。

"同口径"到底指什么？本仓库把它**定义**成了"路径相等"（test_packaging），
那个定义本身没问题——**但它留下了另一半没人管**：集合。

## 为什么这个差异值得管

pre-commit 的价值是**在 commit 当下**拦住问题，而不是等 CI 跑完再回滚。
集合差一半，意味着 8 条门禁里有一半是"事后才知道"。

实测这 6 条未钩住的门禁各自耗时（本机）：

```
selftest          1963 ms
eval_prompts       738 ms
gate               600 ms
modelhub coverage  324 ms
data clean          50 ms
releases（单套）     1 s
```

**全都不贵。** 所以"因为太慢所以没进钩子"这个解释不成立。

本模块的做法不是"把 12 条全塞进钩子"（那会让 commit 变成 3 分钟），
而是**把取舍写成数据并钉住**：哪些门禁只在 CI 跑、各自什么理由、多跑少跑都红。

## 不做的事

不要求 pre-commit 与 CI 的门禁**完全相等**。钩子的定位是"快的拦在本地"，
CI 的定位是"全的兜底"。e2e 桩、releases 三套这种要起进程/占端口的，
留在 CI 是合理的。关键是这个取舍**必须被登记**，而不是默认没人想过。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
CI = ROOT / ".github" / "workflows" / "ci.yml"
PRECOMMIT = ROOT / ".pre-commit-config.yaml"

# pre-commit 侧的 entry 写成 `.venv/Scripts/python.exe -m ruff check ...`，
# 所以判据要把前缀去掉再比对，否则一条都对不上。
_ENTRY_PREFIX = re.compile(r"^\s*entry:\s*\S*(?:python(\.exe)?)?\s*-m\s+")


def _bare(entry: str) -> str:
    return _ENTRY_PREFIX.sub("", entry.strip())


def _norm(cmd: str) -> str:
    """两侧归一化：统一 `python X` / `python.exe X` 的写法。

    ⚠️ 只剥一层 `python` + 可选 `.exe`，**不要顺手把 `-m` 也吃掉**：
    `python -m coverage report ...` 剥成 `coverage report ...` 之后，
    `-fail-under=82` 那条门禁会变成一条谁也认不出的怪命令。
    """
    return re.sub(r"^python(\.exe)?(\s+)", "", cmd.strip())


@pytest.fixture(scope="module")
def ci_gates() -> set[str]:
    wf = yaml.safe_load(CI.read_text(encoding="utf-8"))
    text = "\n".join(str(s.get("run", "")) for s in wf["jobs"]["test"]["steps"] if s.get("run"))
    return {ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")}


@pytest.fixture(scope="module")
def hooks() -> dict[str, str]:
    """钩子 id → 去掉解释器前缀后的命令。"""
    raw = PRECOMMIT.read_text(encoding="utf-8")
    out: dict[str, str] = {}
    for block in re.split(r"\n\s+- id: ", raw)[1:]:
        hook_id = block.split("\n", 1)[0].strip()
        m = re.search(r"^\s*entry:\s*(.+)$", block, re.M)
        if m:
            out[hook_id] = _bare(m.group(1))
    return out


# ---------------------------------------------------------------------------
# 判据：CI 里每条门禁，要么有对应钩子，要么在登记表里写明理由
# ---------------------------------------------------------------------------
# 键 = 命令特征串；值 = 为什么它**不**进 pre-commit。
# ⚠️ 方向在 §二十六 已经吃过一次亏（登记表对称 ⇒ 多跑也绿），这里直接写成
#    `{"probe": {"why": ..., "side": "ci"}}`，多跑/少跑分别判。
_LOCAL_ONLY_CI_GATES = {
    "run_e2e_stub": {
        "why": "要起一个 HTTP 桩并占端口（.ps1 还会做端口避让），塞进 commit 钩子"
        "会让每次提交都可能端口冲突；它是端到端链路验证，本就不该挡住编码一下的人。",
        "side": "ci",
    },
    "pytest releases/": {
        "why": "三套服务共 ~3 秒且要各自起 TestClient；它们验的是**别的产品**"
        "（releases/* 是发布快照，不是本产品），本机改了本产品代码时它们几乎总是绿的，"
        "属于 CI 的兜底职责。",
        "side": "ci",
    },
    "--fail-under=82": {
        "why": "modelhub 分项覆盖率线读的是**上一步 pytest 产出的 .coverage**；"
        "而本地的 pytest 钩子不带 --cov，钩子跑完没有这份文件。"
        "真跑它会读到上一轮（甚至别的项目）留下的陈旧 .coverage，"
        "给出一个**看起来正常的假读数**——比不跑更坏。"
        "这不是『太慢』（实测 324ms），是『没有输入』。"
        "（2026-10-05：这条曾经被我用 `# 跑它没有意义` 注释一句话带过，"
        "  而注释不是判据；那时 CI 与 windows 两侧的同一个问题也发生过。）",
        "side": "ci",
    },
}

# 反过来：**刻意不让 pre-commit 做的**代价登记（它防的是"钩子跑红了没人修"）。
# data-clean 本机只要 50ms，且它防的东西（测试写脏了被 commit 带走）后果最实际，
# 所以本轮给它补了钩子，而不是登记成例外。
_PRESENTLY_HOOKED = {
    "git diff --exit-code": "本机 50ms，2026-10-05 补钩子",
}


def test_ci_gates_are_either_hooked_or_registered(
    ci_gates: set[str], hooks: dict[str, str]
) -> None:
    hook_cmds = {_norm(c) for c in hooks.values()}
    unregistered = []
    for gate in sorted(_norm(g) for g in ci_gates):
        if any(gate == h or gate in h for h in hook_cmds):
            continue
        if any(probe in gate for probe in _LOCAL_ONLY_CI_GATES):
            continue
        if gate.startswith(
            ("pip install", "if ", "elif", "else", "fi", "}", "{", "set ", "-m pip")
        ):
            continue
        if "--base" in gate or "${{" in gate or gate.endswith((".ps1",)):
            continue
        # pytest 的参数不同不算两条门禁：CI 带 --cov，本地钩子不带
        # （带 --cov 会拖慢到 80s+，而覆盖率地板由 CI 负责）。
        if gate.startswith("pytest tests/") or gate.startswith("-m pytest tests/"):
            continue
        unregistered.append(gate)
    assert not unregistered, (
        "ci.yml 里有这些门禁，pre-commit 既没有对应钩子、也没登记理由：\n"
        + "\n".join(f"  {g}" for g in unregistered)
        + "\n二选一：① 给它加钩子（先量耗时，本机实测 selftest 2s / gate 0.6s，"
        "都不贵）；② 登记进 _LOCAL_ONLY_CI_GATES 并写明为什么它只该在 CI 跑。"
        "\n别让差异停在「没人想过为什么」的状态。"
    )


def test_every_registered_exception_says_why(ci_gates: set[str]) -> None:
    """登记表里不许有条目是空的：登记本身就是"我决定过"，理由不能缺。

    ⚠️ 这条**验不了理由真假**——"太慢"是一句话，任何检查都只能验它非空。
    2026-10-05 的变异（把 selftest 登记成"本地跑要 90 秒"）确实转红了，
    但转红的是 `test_the_registrations_actually_exist_in_both_places`
    ——它抓的是"登记了却已经有钩子"，**不是**抓那句假话。
    实测 selftest 1963ms 写在这条判据旁边的注释里，靠的是**人去读**。

    ⇒ 诚实的边界：登记表能钉住"这个取舍被显式记下来了"，
      钉不住"这个取舍的理由今天还成立"。后者只能靠 review。
    """
    for probe, entry in _LOCAL_ONLY_CI_GATES.items():
        assert entry["why"].strip(), f"{probe} 登记了但没写理由"
        assert entry["side"] == "ci", f"{probe} 的 side 只能是 ci（本模块只管 CI 独有的）"
        assert any(probe in g for g in ci_gates), (
            f"{probe} 在登记表里，但 ci.yml 里已经找不到这条门禁了 —— 删掉登记"
        )


def test_the_registrations_actually_exist_in_both_places(
    ci_gates: set[str], hooks: dict[str, str]
) -> None:
    """反向：登记说"只在 CI 跑"的，不许**同时**已经有钩子。

    这条防的是"补了钩子但忘了删登记"——两种状态都描述同一件事，
    留着一份过期登记，下一个人会以为这事还没做完。
    """
    hook_cmds = {_norm(c) for c in hooks.values()}
    for probe in _LOCAL_ONLY_CI_GATES:
        assert not any(probe in h for h in hook_cmds), (
            f"{probe} 已经进了 pre-commit 钩子，却还挂在 _LOCAL_ONLY_CI_GATES 里 —— "
            "登记该删了（两份描述同一件事，只留一份）"
        )


# ---------------------------------------------------------------------------
# 判据：钩子本身不许腐化
# ---------------------------------------------------------------------------
def test_the_header_claim_matches_reality(hooks: dict[str, str]) -> None:
    """配置文件头写"与 ci.yml 同口径的本地版"——那句话必须仍然成立。

    具体化：本地版至少要有**全部快速入口门禁**（能起进程/占端口的除外，
    那些登记在 `_LOCAL_ONLY_CI_GATES`）。
    """
    hook_cmds = {_norm(c) for c in hooks.values()}
    fast = ["ruff check", "ruff format --check", "mypy", "pytest tests/", "run.py --selftest"]
    missing = [f for f in fast if not any(f in h for h in hook_cmds)]
    assert not missing, (
        f"本地版少了这几条快速门禁：{missing}\n"
        "它们全都不贵（本机实测 selftest 2s）。缺了它们，"
        "『本地版与 CI 同口径』这句就只是标题。"
    )


# 判据刻意**不用钩子数量**：数量可以通过加空钩子刷上去，而集合对不齐。
# ——这条原本写成一个自检（扫自己的源码确认没用 len(hooks)），
#   结果它匹配到了自己那行 assert 字符串自己。删掉：它护的东西，
#   上面两条真正的判据已经护住了，而它本身是这仓最脆的一类写法。
