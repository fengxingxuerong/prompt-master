"""钩子必须**能拦住东西** —— 而不只是跑得绿（2026-10-05 第十轮）。

## 起因

上一轮给 pre-commit 补了 4 条钩子（selftest / prompt-gate / eval-prompts /
releases-data-clean），当时的验证是：

    pre-commit run --all-files   →  七条全 Passed

**但这只证明了它们能跑绿，没证明它们会红。** 一条永远绿的钩子和没有这条钩子
在观测上完全一样。而本仓库对"配置存在但从未真跑"有过专门记录——
那次的教训是"看起来绿"不等于"在工作"。

所以本模块的判据是：**每条钩子都配一个故意弄坏的场景，断言它把 exit code 变成非 0。**

## 本轮实测得到的一条事实（比钩子本身更重要）

第一版我用 `pm/prompts.py` 里一句"要求 issues 有证据支撑。"做 prompt-gate 的
变异素材，结果门禁 rc=0。当时我据此得出一个**很大的结论**：

> 「`run.py gate` 完全看不见工作树，所以挂在 pre-commit 上没有意义」

**这个结论是错的，而且是我自己的探针错了。** 那句话在**模块 docstring** 里
（第 23 行），不在任何模板体内——门禁比对的是 17 个模板的正文，docstring 不在
比对范围内，所以"没有变化"是**正确行为**，不是缺陷。

换成从 `extract_templates()` 里取真实模板正文再删一句之后，门禁输出：

    本次有文本改动的模板：
      - CLARIFIER_SYSTEM：字符 -62，约束条目 +0，新引入 0 / 修好 0

它**看见了**改动（工作树是读得到的，`pm/promptgate.py:261` 就是
`head_file.read_text()`），只是没有判定为"回归"——因为门禁的判据是
"**新引入规则命中**"，不是"文本变了"。

⇒ 所以 `prompt-gate` 的正确变异素材是**引入一条命中规则**，不是改点文字。
本模块就是这么做的（`_break_prompt_rule()`）。

## 记录这次错误的原因

它符合本仓反复出现的那一类：**"探针的输出被当成权威结论"**。
同一个会话里已经犯过一次（§二十四 探针数出 1406、ratchet 说 1369）。
而这次更糟一档——我不只用了错的探针，还把错的结论写成了"门禁没价值"，
差点据此删掉一条钩子。

唯一的差别是：本轮在**动配置之前**先读回了 `pm/promptgate.py` 的实现，
发现第 261 行读的是工作树。**读实现救了这个错。**
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PROMPTS = ROOT / "pm" / "prompts.py"
PY = sys.executable


# ---------------------------------------------------------------------------
# 工具：安全地"弄坏 → 验 → 还原"
# ---------------------------------------------------------------------------
@pytest.fixture
def restorer():
    """登记要还原的文件；无论断言是否失败，都在退出时按字节还原。

    ⚠️ 必须按**字节**还原，不能按文本：Windows 上 Python 的 `write_text`
    默认会把 `\n` 写成 `\r\n`，于是"还原"出来的文件与 HEAD 有差异，
    `git status` 会一直显示 modified，而 `git diff` 又看不到内容变化——
    这是本轮实际踩到的一个坑（探针跑了三轮才发现自己污染了工作树）。
    """
    saved: list[Path] = []

    def save(path: Path) -> None:
        shutil.copy2(path, path.with_suffix(path.suffix + ".hooksbak"))
        saved.append(path)

    yield save

    for path in saved:
        backup = path.with_suffix(path.suffix + ".hooksbak")
        if backup.exists():
            shutil.copy2(backup, path)
            backup.unlink()


def run_hook(hook_id: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [PY, "-m", "pre_commit", "run", hook_id, "--all-files"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=ROOT,
        check=False,
        timeout=300,
    )


def _break_prompt_rule(text: str) -> str | None:
    """让某个模板**命中一条确定性规则**，而不是单纯改几个字。

    这是本模块最关键的一处修正：第一版只删了模板里一句话（字符 -62），
    门禁如实报告"未新引入任何规则问题"——那是**正确行为**。
    门禁判的是"新引入规则命中"，素材必须对着判据造。
    """
    sys.path.insert(0, str(ROOT))
    from pm.promptgate import extract_templates
    from pm.quality import check_prompt_quality

    def clean(body: str) -> bool:
        """模板是否**不**命中任何确定性规则。"""
        return not check_prompt_quality(body).issues

    templates = extract_templates(text)
    if not templates:
        return None
    # 找一个**干净**的模板，往里塞绝对判据会命中的东西。
    # 第一版只删了模板里一句话（字符 -62），门禁如实报"未新引入任何规则问题"——
    # 那是**正确行为**，因为门禁判的是"新引入规则命中"，素材必须对着判据造。
    for body in templates.values():
        if not clean(body):
            continue
        # 污染素材必须命中 `pm/quality.py` 里**真实存在**的确定性规则。
        # 第一版凭印象写了"禁止输出元说明""自由发挥"这类措辞，轮了一圈
        # `check_prompt_quality` 才发现它们**一条都不触发**（该模板 issues 恒为 0），
        # 于是判据一直 skip —— skip 不算绿，但也不算证据。
        # 现用 `delimiter_problems` 的确定项：同一标签开两次（pm/quality.py:143 记的 Run A 事故）。
        poison = body + "\n\n<输出>\n<输出>\n"
        if not clean(poison):
            return text.replace(body, poison, 1)
    return None


def test_a_broken_prompt_is_actually_blocked(restorer) -> None:
    """prompt-gate：引入一条命中规则 ⇒ 必须红。

    ⚠️ 素材必须是**规则命中**，不是文本变化。理由见模块 docstring。
    """
    restorer(PROMPTS)
    original = PROMPTS.read_bytes()
    poisoned = _break_prompt_rule(original.decode("utf-8"))
    if poisoned is None:
        pytest.skip("没找到可以污染的干净模板（模板集全变脏时属正常，跳过而非假绿）")
    PROMPTS.write_text(poisoned, encoding="utf-8", newline="")

    proc = run_hook("prompt-gate")
    assert proc.returncode != 0, (
        f"把一个干净模板污染到命中确定性规则后，prompt-gate 钩子仍然 rc=0。\n"
        f"{proc.stdout[-800:]}\n"
        "这条钩子如果永远绿，就等于没有——上一轮补它就是为了这个。"
    )


def test_an_untouched_repo_stays_green() -> None:
    """反向对照：不弄坏时必须是绿的。

    没有这条，上面那条可能是因为"钩子恒红"才通过的。
    """
    proc = run_hook("prompt-gate")
    assert proc.returncode == 0, (
        f"工作树是干净的，prompt-gate 却 rc={proc.returncode}：\n{proc.stdout[-800:]}"
    )


def test_dirty_release_data_is_actually_blocked(restorer) -> None:
    """releases-data-clean：把被跟踪的数据写脏 ⇒ 必须红。

    这条防的是"测试写脏的运营数据被 commit 直接带走"——CI 要等 push 才发现，
    那时脏数据已经在历史里了。
    """
    candidates = sorted((ROOT / "releases").rglob("data/*"))
    tracked = [p for p in candidates if p.is_file()]
    if not tracked:
        pytest.skip("releases/*/data 下没有可用的数据文件")
    # ⚠️ 只能用 **git 已跟踪** 的文件：`git diff --exit-code` 看不见未跟踪文件，
    #    而 releases/*/data 下同时存在 orders.db 这类**未跟踪**的运行时产物。
    #    第一版用 rglob 抓到 orders.db，写脏它钩子照样 rc=0——判据没红，
    #    原因是素材根本不在钩子的观测范围内。
    out = subprocess.run(
        ["git", "ls-files", "releases/*/data/*"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=ROOT,
        check=False,
    )
    tracked_paths = [ROOT / ln.strip() for ln in out.stdout.splitlines() if ln.strip()]
    tracked_paths = [p for p in tracked_paths if p.is_file()]
    if not tracked_paths:
        pytest.skip("releases/*/data 下没有被 git 跟踪的文件")
    victim = tracked_paths[0]

    restorer(victim)
    victim.write_bytes(victim.read_bytes() + b"\nprobe-dirty\n")

    proc = run_hook("releases-data-clean")
    assert proc.returncode != 0, (
        f"把 {victim.name} 写脏后，releases-data-clean 仍然 rc=0。\n{proc.stdout[-500:]}"
    )


def test_the_hook_config_lists_exactly_what_this_module_checks() -> None:
    """本模块覆盖的钩子必须真的在 `.pre-commit-config.yaml` 里。

    防的是"钩子被删了/改名了，本模块还在测一个不存在的东西"——
    那种情况下上面几条会**假绿**（`pre_commit run <不存在的id>` 会报错，
    但这里的 rc 恰好也是非 0，于是"必须红"的断言照样通过）。
    """
    cfg = (ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    hooks = set(re.findall(r"^\s+- id:\s*(\S+)", cfg, re.M))
    for required in ("prompt-gate", "releases-data-clean", "selftest", "eval-prompts"):
        assert required in hooks, f"钩子 {required} 不在 .pre-commit-config.yaml 里"
