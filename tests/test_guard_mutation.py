"""守卫的守卫：变异测试证明 `test_env_isolation_meta.py` 与
`test_ci_gate_matrix.py` 真的会红。

## 为什么需要这一条

本仓的元测试自己踩过一次坑：初版判据是"这个键在 conftest 文本里出现过"，
而 `PM_FAKE_BACKEND` 在夹具里只出现在**注释里**（"一旦 .env 里留着…"），
于是把那行真正的隔离删掉，元测试**照样绿**。

**一个不会失败的闸比没有闸更糟** —— 它买的是假的安心感。所以这里把它
钉成常规用例：每次改 conftest 的隔离逻辑，都自动验一遍"删掉它，元测试红不红"。

CI 接线那条线同理：`test_ci_gate_matrix.py` 守的是"windows job 不许漏跑
拓扑自检"，而 windows job 漏跑**正是本轮修的那个真实缺陷**——
没人红过，因为根本没有东西会红。

## 成本

每条变异跑一次 pytest（约 1–2 秒）。落在 CI 上是十几秒，
换来的是"隔离闸与 CI 接线闸本身可信"这个不变量。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

CONFTEST = Path(__file__).resolve().parent / "conftest.py"
META = "tests/test_env_isolation_meta.py"

# 每条 = (说明, 要删掉/改坏的那段原文)
MUTATIONS = [
    ("PM_JUDGES 隔离", '("PM_JUDGES", "2"),'),
    ("PM_JUDGE_DISAGREEMENT 隔离", '("PM_JUDGE_DISAGREEMENT", "2.0"),'),
    # 这条是本文件的由来：初版闸漏掉了它，删了照样绿
    ("PM_FAKE_BACKEND 隔离", '("PM_FAKE_BACKEND", ""),'),
    ("PM_CACHE_DIR 落盘目录", 'monkeypatch.setenv("PM_CACHE_DIR", str(tmp_path / "cache"))'),
    ("行为夹具的 autouse", "@pytest.fixture(autouse=True)\ndef isolate_behaviour_switches"),
]

# ---------------------------------------------------------------------------
# CI 接线那条线：同上，windows job 漏跑拓扑自检**就是本轮修的真实缺陷**
# ——没人红过，因为根本没有东西会红。
# ---------------------------------------------------------------------------
CI_WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml"
CI_MUTATIONS = [
    # 整步删掉（模拟"改 CI 时顺手删了 windows 那条"）
    ("windows selftest 步骤", "      - name: Topology selftest（Windows 侧同口径，2026-10-05 补）"),
    # 只删 env（模拟"以为空 Key 是多余的"）——判据二要抓的正是这一档
    (
        "selftest 的空 API_KEY",
        '        env:\n          PM_API_KEY: ""\n        run: python run.py --selftest',
    ),
    # 第三档（2026-10-05 补）：**多**跑一条没登记的门禁。
    # 两个方向都得红：少跑是真缺口，多跑是"有人把某条门禁当成了可选项"。
    # ⚠️ 锚点必须落在 **windows job 段**内——同一个步骤名两边都有，
    #    `replace(..., 1)` 只换第一处，锚点写松了等于变异了 linux job（而 linux
    # 本来就有 ruff check，变异后毫无变化，测试自然绿）。
    (
        "windows 侧多跑一条 ruff check",
        "      - name: Stub e2e (PowerShell)",
        "      - name: Lint (ruff check)\n        run: ruff check pm/ tests/\n"
        "      - name: Stub e2e (PowerShell)",
    ),
    # 第四档（2026-10-05 补）：凭空多出一条**登记表里根本没有**的门禁。
    # 登记表只管"两边有差异"，这一档是"两边都没有却突然多了一条"——
    # 它能红，是因为判据里那条"未登记差异"把新命令算成了 windows 侧的额外内容。
    (
        "CI 里多一条没登记的门禁",
        "      - name: Stub e2e (PowerShell)",
        "      - name: Gates nobody registered\n        run: python run.py surprise_gate\n"
        "      - name: Stub e2e (PowerShell)",
    ),
]

# ---------------------------------------------------------------------------
# 判据五（文档 ↔ CI 同步）的守卫：同样要有变异
# ---------------------------------------------------------------------------
OPS_DOC = Path(__file__).resolve().parents[1] / "docs" / "operations.md"
DOC_MUTATIONS = [
    # 文档块里删掉一条 CI 真的在跑的门禁 → 照文档跑的人会少跑一步
    (
        "文档漏抄 selftest",
        "python run.py --selftest          # CI 里额外清 PM_API_KEY 再跑一次（干净检出无 .env 也必须过）",
        "",
    ),
    # 反向：文档块**里面**凭空多一条 CI 没有的命令。
    # ⚠️ 替换目标必须落在 ```bash 围栏内——早先那版把命令追加在围栏外面，
    #    而 `_docs_gate_block()` 只取块内内容，于是变异根本没进被测范围，
    #    守卫当然绿。**变异锚点本身也要验它落在判据的作用域里。**
    (
        "文档多写一条不存在的门禁",
        "python eval_prompts.py            # 节点提示词结构契约",
        "python eval_prompts.py            # 节点提示词结构契约\npython run.py nonexistent_gate",
    ),
]

DOC_GUARD = "tests/test_ci_gate_matrix.py"


@pytest.fixture
def _restore_docs():
    original = OPS_DOC.read_text(encoding="utf-8")
    try:
        yield original
    finally:
        OPS_DOC.write_text(original, encoding="utf-8")


@pytest.mark.parametrize(
    ("label", "anchor", "replacement"), DOC_MUTATIONS, ids=[m[0] for m in DOC_MUTATIONS]
)
def test_docs_drifting_from_ci_makes_the_parity_guard_red(
    label: str, anchor: str, replacement: str, _restore_docs: str
) -> None:
    assert anchor in _restore_docs, f"锚点已变，变异测试失效：{anchor!r}"
    OPS_DOC.write_text(_restore_docs.replace(anchor, replacement, 1), encoding="utf-8")
    try:
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                DOC_GUARD,
                "-q",
                "--no-header",
                "-p",
                "no:cacheprovider",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    finally:
        OPS_DOC.write_text(_restore_docs, encoding="utf-8")
    assert proc.returncode != 0, (
        f"「{label}」之后，文档↔CI 同步守卫仍然全绿 —— 它是个假闸。\n"
        "operations.md 里「与 ci.yml 逐条同口径」是加粗断言，"
        "它红不了就等于没有。"
    )


CI_GUARD = "tests/test_ci_gate_matrix.py"


@pytest.fixture
def _restore_ci():
    original = CI_WORKFLOW.read_text(encoding="utf-8")
    try:
        yield original
    finally:
        CI_WORKFLOW.write_text(original, encoding="utf-8")


@pytest.mark.parametrize(
    ("label", "anchor", "replacement"),
    [(m[0], m[1], m[2] if len(m) > 2 else "") for m in CI_MUTATIONS],
    ids=[m[0] for m in CI_MUTATIONS],
)
def test_breaking_the_ci_wiring_makes_the_matrix_guard_red(
    label: str, anchor: str, replacement: str, _restore_ci: str
) -> None:
    assert anchor in _restore_ci, f"锚点已变，变异测试失效：{anchor!r}"
    CI_WORKFLOW.write_text(_restore_ci.replace(anchor, replacement, 1), encoding="utf-8")
    try:
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                CI_GUARD,
                "-q",
                "--no-header",
                "-p",
                "no:cacheprovider",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    finally:
        CI_WORKFLOW.write_text(_restore_ci, encoding="utf-8")
    assert proc.returncode != 0, (
        f"把「{label}」改坏后，CI 接线守卫仍然全绿 —— 它是个假闸。\n"
        "接线闸的意义恰恰在于：漏跑的那一步当时没人红过。"
    )


@pytest.fixture
def _restore_conftest():
    """无论变异结果如何（含失败）都要把 conftest 还原 —— 否则会污染工作树。"""
    original = CONFTEST.read_text(encoding="utf-8")
    try:
        yield original
    finally:
        CONFTEST.write_text(original, encoding="utf-8")


@pytest.mark.parametrize(("label", "anchor"), MUTATIONS, ids=[m[0] for m in MUTATIONS])
def test_removing_this_isolation_makes_the_meta_test_red(
    label: str, anchor: str, _restore_conftest: str
) -> None:
    assert anchor in _restore_conftest, f"锚点已变，变异测试失效：{anchor!r}"
    mutated = _restore_conftest.replace(anchor, "", 1)
    CONFTEST.write_text(mutated, encoding="utf-8")
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", META, "-q", "--no-header", "-p", "no:cacheprovider"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    finally:
        CONFTEST.write_text(_restore_conftest, encoding="utf-8")
    assert proc.returncode != 0, (
        f"删掉「{label}」的隔离后，元测试仍然全绿 —— 它是个假闸。\n"
        "一个不会失败的守卫比没有守卫更糟：它让人以为隔离有保护。"
    )


# ---------------------------------------------------------------------------
# §二十七：pre-commit 门禁**集合**那条守卫
# ---------------------------------------------------------------------------
# 这条守卫最可能的假绿方式是"钩子少了但登记也没少"——两边同时缺，
# 差集为空 → 绿。所以变异要覆盖三种真实退化。
#
# ⚠️ 变异目标分两种文件，别混：删钩子动 `.pre-commit-config.yaml`，
#    而"给例外编个理由"只能动判据自己的登记表（在 guard 文件里）。
#    ——这正是本轮开头那次的错：我把两条都挂在配置文件的 fixture 上，
#    结果锚点根本不在那儿，测试自己先红了（好过假绿）。
PRECOMMIT_CFG = Path(__file__).resolve().parents[1] / ".pre-commit-config.yaml"
PC_GUARD_PATH = Path(__file__).resolve().parents[1] / "tests" / "test_precommit_gate_set.py"
PC_GUARD = "tests/test_precommit_gate_set.py"

# 变异目标 = .pre-commit-config.yaml（真的少一条钩子）
PC_CFG_MUTATIONS = [
    (
        "本地少了 selftest 钩子",
        "      - id: selftest\n        name: 拓扑自检（唯一会在推送前抓到仲裁/兜底回归的一条）\n"
        "        entry: .venv/Scripts/python.exe run.py --selftest\n        language: system\n"
        "        pass_filenames: false\n        always_run: true\n",
        "",
    ),
]

# 变异目标 = 判据自己的登记表（在 guard 文件里）
PC_REGISTRY_MUTATIONS = [
    # 给例外编个理由（"太慢"）来放行一条本地该有的门禁。
    # selftest 实测 1963ms —— 这个"90 秒"是假的，判据必须红。
    (
        "把自检登记成「太慢所以只在 CI」",
        '    "run_e2e_stub": {\n        "why": "要起一个 HTTP 桩并占端口',
        '    "run.py --selftest": {\n'
        '        "why": "太慢，本地跑要 90 秒。",\n'
        '        "side": "ci",\n'
        "    },\n"
        '    "run_e2e_stub": {\n        "why": "要起一个 HTTP 桩并占端口',
    ),
    # 登记表说"只在 CI"，可钩子其实已经在了 —— 两份描述同一件事，留着会误导下一个人。
    (
        "已进钩子却仍挂在例外表里",
        '    "run_e2e_stub": {\n        "why": "要起一个 HTTP 桩并占端口',
        '    "eval_prompts.py": {\n'
        '        "why": "它已经有钩子了，登记忘删",\n'
        '        "side": "ci",\n'
        "    },\n"
        '    "run_e2e_stub": {\n        "why": "要起一个 HTTP 桩并占端口',
    ),
    # 登记的理由写成空的 —— "登记过"不等于"想过"
    (
        "登记表有条目但没写理由",
        '        "why": "三套服务共 ~3 秒且要各自起 TestClient；',
        '        "why": "",\n        "_unused": "三套服务共 ~3 秒且要各自起 TestClient；',
    ),
]


def _mutate_and_run(
    path: Path, label: str, anchor: str, replacement: str, original: str, guard: str
) -> None:
    assert anchor in original, f"锚点已变，变异测试失效：{anchor!r}"
    path.write_text(original.replace(anchor, replacement, 1), encoding="utf-8")
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", guard, "-q", "--no-header", "-p", "no:cacheprovider"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    finally:
        path.write_text(original, encoding="utf-8")
    assert proc.returncode != 0, (
        f"「{label}」之后，门禁集合守卫仍然全绿 —— 它是个假闸。\n"
        "pre-commit 钩子少于 CI 门禁这件事，"
        "就是靠这条守卫才不会再退化回去。"
    )


@pytest.fixture
def _restore_pc():
    original = PRECOMMIT_CFG.read_text(encoding="utf-8")
    try:
        yield original
    finally:
        PRECOMMIT_CFG.write_text(original, encoding="utf-8")


@pytest.fixture
def _restore_pc_registry():
    original = PC_GUARD_PATH.read_text(encoding="utf-8")
    try:
        yield original
    finally:
        PC_GUARD_PATH.write_text(original, encoding="utf-8")


@pytest.mark.parametrize(
    ("label", "anchor", "replacement"), PC_CFG_MUTATIONS, ids=[m[0] for m in PC_CFG_MUTATIONS]
)
def test_dropping_a_hook_makes_the_set_guard_red(
    label: str, anchor: str, replacement: str, _restore_pc: str
) -> None:
    _mutate_and_run(PRECOMMIT_CFG, label, anchor, replacement, _restore_pc, PC_GUARD)


@pytest.mark.parametrize(
    ("label", "anchor", "replacement"),
    PC_REGISTRY_MUTATIONS,
    ids=[m[0] for m in PC_REGISTRY_MUTATIONS],
)
def test_forging_an_exception_makes_the_set_guard_red(
    label: str, anchor: str, replacement: str, _restore_pc_registry: str
) -> None:
    _mutate_and_run(PC_GUARD_PATH, label, anchor, replacement, _restore_pc_registry, PC_GUARD)


# ---------------------------------------------------------------------------
# §二十九：账本仪表身份过滤那条守卫
# ---------------------------------------------------------------------------
# 变异目标第一次落在**源码**（pm/schemas.py）而不是配置/文档，所以还原要字节精确：
# `write_text` 在 Windows 上会写 \r\n，于是 git status 显示 ` M` 而 git diff 是空的
# —— 本轮实测过这个坑（见 docs/evaluation.md §二十七·四·2）。
SCHEMAS = Path(__file__).resolve().parents[1] / "pm" / "schemas.py"
PARITY_GUARD = "tests/test_ledger_reader_parity.py"

SCHEMA_MUTATIONS = [
    # 核心那条：把身份过滤整个去掉 = §二十九 修复前的原样。
    (
        "去掉仪表身份过滤",
        '        if (entry.get("model") or "") != model:\n            continue\n',
        "",
    ),
    # 同一个 bug 的**后门档**：model=None 时不再一律拒绝。
    # 第一版实现就是死在这里——判据写 `!= (model or "")`，缺 model 的条目被判成匹配。
    (
        "把 model=None 放行（后门）",
        "    if not model:\n        return {}\n",
        "",
    ),
    # 只把"缺 model 也算匹配"改回来，身份过滤本身还在 —— 最隐蔽的一档。
    (
        "缺 model 视为匹配",
        '        if (entry.get("model") or "") != model:\n            continue\n',
        '        if entry.get("model") not in (None, "", model):\n            continue\n',
    ),
]


def _mutate_schemas(original: str, anchor: str, replacement: str) -> None:
    """按锚点改写 schemas.py，**换行风格无关**。

    仓库里这个文件是 CRLF 检出，而锚点按 LF 写死；早先那版直接 assert
    `anchor in text`，三条变异全部以"锚点已变"失败——那是**假通过**：
    守卫根本没跑，却因为参数化用例红了而看起来像"验过了"。
    换行无关靠 `newline=""` 读写：内容里的 `\r\n` 原样保留，替换时按实际出现的
    那一种做。
    """
    for nl in ("\r\n", "\n"):
        a = anchor.replace("\n", nl)
        if a in original:
            SCHEMAS.write_text(
                original.replace(a, replacement.replace("\n", nl), 1),
                encoding="utf-8",
                newline="",
            )
            return
    raise AssertionError(f"锚点已变，变异测试失效：{anchor!r}")


@pytest.fixture
def _restore_schemas():
    original = SCHEMAS.read_bytes()
    try:
        yield original.decode("utf-8")
    finally:
        SCHEMAS.write_bytes(original)  # 字节精确，见上面那段注记


@pytest.mark.parametrize(
    ("label", "anchor", "replacement"),
    SCHEMA_MUTATIONS,
    ids=[m[0] for m in SCHEMA_MUTATIONS],
)
def test_breaking_the_identity_filter_makes_the_parity_guard_red(
    label: str, anchor: str, replacement: str, _restore_schemas: str
) -> None:
    _mutate_schemas(_restore_schemas, anchor, replacement)
    try:
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                PARITY_GUARD,
                "-q",
                "--no-header",
                "-p",
                "no:cacheprovider",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    finally:
        SCHEMAS.write_bytes(_restore_schemas.encode("utf-8"))
    assert proc.returncode != 0, (
        f"「{label}」之后，账本读者一致性守卫仍然全绿 —— 它是个假闸。\n"
        "这条守卫守的正是本轮的真实缺陷：噪声带把不属于当前仪表的实测算了进去。"
    )
