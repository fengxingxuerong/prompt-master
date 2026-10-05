"""CI 覆盖矩阵的元测试：两个 job 必须跑**同一批**门禁。

## 起因（2026-10-05 实测，不是推测）

把 `.github/workflows/ci.yml` 按 YAML 解出来，对每个 job 逐条问"这条 `run:`
里有没有 X"，发现 **windows job 少 5 步**：

```
lint                   linux=True   windows=False
format                 linux=True   windows=False
types                  linux=True   windows=False
modelhub coverage      linux=True   windows=False
TOPOLOGY SELFTEST      linux=True   windows=False
```

其中 lint/format/types/modelhub 四条**大概率是有意的**——它们与平台无关，
在 ubuntu 上跑一次就够,Windows 上重复跑只是拖慢 CI。**本模块不要求它们对齐。**

但 `Topology selftest` 不是同一类：它是**唯一一条走 `run.py` 入口、
在干净检出里把整张图跑一遍**的门禁。windows job 不跑它，意味着
"Windows 上入口层（`pm/cli/`、编码、路径归一化）的拓扑自检"没人验。

## 更要紧的：selftest 的两个运行环境不是同一个程序

CI 是**干净检出**（无 `.env`）+ `PM_API_KEY=""`；开发机是 `pm.llm` 模块级
`load_dotenv()` 把本机 `.env` 灌进来。同一条命令，两套输入：

```
本机（有 .env）  arbitration gate = 3.18
CI （无 .env）  arbitration gate = 2.0
```

差 1.6 倍。仲裁场景的分差一旦落在 2.0 与 3.18 之间，本机红、CI 绿——
**或者反过来**。这不是"本地严一点没关系"，是两个 CI 环境给出不同结论。

⇒ 判据一：selftest 必须在**两个** job 里都跑，且显式清空会改行为的键。
⇒ 判据二：`PM_API_KEY: ""` 这个写法不许被删（它就是"干净检出"的实现方式）。

## 不做的事（**并且这条"不做"本身是一条判据**）

不要求 lint/format/types/modelhub-coverage 在两个 job 对齐——它们与平台无关，
在 ubuntu 上跑一次就够，Windows 上重复跑只是拖慢 CI。**那是有意的取舍。**

但"有意的取舍"写在 docstring 里等于**没有记录**：下一个人看到 windows job 少跑
四步，很可能当成漏配去补齐，白白多花 CI 时间；也可能反过来，把某条该两边都跑的
门禁当成"那几条之一"跟着砍掉。两种都是静默的。

⇒ 所以取舍必须写成**数据**（`_ALLOWED_JOB_DIFFERENCES`），并且由
`test_the_two_jobs_differ_only_in_registered_ways` 钉住：
**两个 job 的差异超出登记表，就红。** 登记一次要写理由，改理由要留痕。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
CI = ROOT / ".github" / "workflows" / "ci.yml"


@pytest.fixture(scope="module")
def jobs() -> dict:
    return yaml.safe_load(CI.read_text(encoding="utf-8"))["jobs"]


def _runs(job: dict) -> str:
    return "\n".join(str(s.get("run", "")) for s in job.get("steps", []))


def _step(job: dict, needle: str) -> dict | None:
    """找到 `run:` 里含 needle 的那一步（按 run 文本找，不按 name 标签找）。"""
    for step in job.get("steps", []):
        if needle in str(step.get("run", "")):
            return step
    return None


# ---------------------------------------------------------------------------
# 判据一：产品入口类门禁必须在两个 job 里都跑
# ---------------------------------------------------------------------------
# 这些都是"从命令行入口把产品真正跑一遍"的门禁。它们与平台**有关**
# （路径、编码、shell 都不同），所以两个 job 各跑一次不是冗余，是覆盖面。
# 与 lint/format/types 相反——那几条与平台无关，跑一次就够。
#
# ⚠️ 探针写成 `python <脚本>` 而不是裸文件名：`eval_prompts.py` 在 CI 里
# 还作为 ruff / mypy 的**检查目标路径**出现过（`ruff check ... eval_prompts.py`），
# 裸文件名会把那三行也算成"跑了这个门禁"。
_ENTRYPOINT_GATES = {
    "拓扑自检": "python run.py --selftest",
    "节点提示词结构契约": "python eval_prompts.py",
    "提示词改动回归门禁": "python run.py gate",
}


@pytest.mark.parametrize(("label", "probe"), sorted(_ENTRYPOINT_GATES.items()))
def test_every_job_runs_the_entrypoint_gates(jobs: dict, label: str, probe: str) -> None:
    missing = [name for name, job in jobs.items() if probe not in _runs(job)]
    assert not missing, (
        f"门禁「{label}」（`{probe}`）在 {missing} job 里没有跑。\n"
        "这些门禁走的是**命令行入口**，与平台相关（路径 / 编码 / shell），"
        "少一个 job 就少一类平台上的入口层验证。\n"
        "若确实要某个 job 豁免，把它的理由写进本文件顶部的『不做的事』，别默默少跑。"
    )


def _run_bodies(job: dict) -> list[str]:
    """取 `run:` 字段里的**可执行行**（去掉 `#` 注释行与空行）。

    为什么连 `run:` 字段本身也要再洗一遍：CI 里写的是 YAML 块标量
    （`run: |`），步骤的说明文字就**躺在 run 字段里面**——它是 YAML 注释，
    但仍然属于 `run` 的字符串。直接数会出现
    "节点提示词结构契约 4 次"这种假阳性（linux job 那一步的 run 里，
    注释就引用了三次 eval_prompts.py）。

    ⇒ 数"这条命令被真正执行几次"，必须数**去掉注释之后**的 run 行。
    """
    bodies = []
    for step in job.get("steps", []):
        raw = str(step.get("run", ""))
        bodies.append(
            "\n".join(
                ln for ln in raw.splitlines() if ln.strip() and not ln.strip().startswith("#")
            )
        )
    return bodies


def test_each_entrypoint_gate_appears_exactly_once_per_job(jobs: dict) -> None:
    """每个 job 里的入口门禁**只能出现一次**。

    防的是"看起来跑了、其实被后面一条覆盖掉"：CI 的 `run:` 是顺序执行的，
    同一块里两次调用时，前一次的非零退出码会被后一次盖掉。
    """
    for name, job in jobs.items():
        for label, probe in _ENTRYPOINT_GATES.items():
            count = sum(1 for body in _run_bodies(job) if probe in body)
            assert count == 1, (
                f"{name} job 里「{label}」在 run 字段中出现 {count} 次（应为 1 次）。\n"
                "CI 的 run 是顺序执行：同一门禁跑两遍时，前一遍的红会被后一遍盖掉。"
            )


# ---------------------------------------------------------------------------
# 判据二：selftest 必须是"干净检出"语义
# ---------------------------------------------------------------------------
def test_selftest_runs_with_an_empty_api_key(jobs: dict) -> None:
    """`PM_API_KEY: ""` 是"干净检出"的实现方式，删了它 selftest 就变了语义。

    有 Key 时 selftest 走真实后端（花真钱、结果随端点漂）；
    空 Key 才保证它验的是**图与兜底逻辑**，而不是某个端点的脾气。
    """
    for name, job in jobs.items():
        if "python run.py --selftest" not in _runs(job):
            continue
        step = _step(job, "python run.py --selftest")
        env = step.get("env") or {}
        assert "PM_API_KEY" in env, (
            f"{name} job 的 selftest 步骤没有设 PM_API_KEY —— "
            "要么用真 Key 跑（花钱且随端点漂），要么继承宿主环境（不可复现）"
        )
        assert str(env["PM_API_KEY"]) == "", (
            f"{name} job 的 selftest 步骤 PM_API_KEY={env['PM_API_KEY']!r}，应为空串"
        )


def test_both_jobs_have_the_same_matrix_of_entrypoint_gates(jobs: dict) -> None:
    """一次性把矩阵打出来，便于 review 时肉眼复核（失败时信息最全）。"""
    matrix = {
        name: {label: (probe in _runs(job)) for label, probe in _ENTRYPOINT_GATES.items()}
        for name, job in jobs.items()
    }
    values = list(matrix.values())
    assert all(v == values[0] for v in values), f"两个 job 的入口门禁矩阵不一致：{matrix}"


# ---------------------------------------------------------------------------
# 判据四：两个 job 的差异必须是**登记过的**差异
# ---------------------------------------------------------------------------
# 键 = 探针；值 = (允许出现在哪一侧, 为什么)。
#
# ⚠️ 方向是必需的，不是修饰（2026-10-05 变异测试逼出来的）：
# 第一版只记"这个键允许有差异"，**没记允许差在哪一侧**。于是往 windows job
# 里**加**一条 `ruff check`，对称差集里出现它、登记里也有 "ruff check"、
# 测试照样绿 —— 而那条登记的原意是"它只该在 linux 侧"。
# ⇒ 多跑与少跑必须分别能红，否则这张表只挡了单向。
_ALLOWED_JOB_DIFFERENCES: dict[str, tuple[str, str]] = {
    "run.py gate --base": (
        "both",
        "同一条门禁在两个 job 里的**基线参数写法不同**（bash 用 `if/elif`，"
        "pwsh 用 `elseif`；前者带 --json 后者不带）。判据同源，都是与基线比 "
        "pm/prompts.py，差异是 shell 语法造成的，不是覆盖面差异。",
    ),
    "pytest tests/ -q": (
        "both",
        "**同一条门禁、不同参数**：linux 侧带 --cov=pm --cov-report=term-missing "
        "并产出 .coverage 供 modelhub 分项线消费，windows 侧不带"
        "（它没跑 --cov，分项线在那边没有输入）。不是少跑一条测试。",
    ),
    "run_e2e_stub": (
        "both",
        "两个 job 各用自己的 shell 版本（.sh / .ps1）。这不是覆盖面差异："
        "Windows 侧覆盖的正是 .ps1 独有的那部分（UTF-8 / 端口避让 / 任务清理）。",
    ),
    "ruff check": ("linux", "与平台无关；ubuntu 跑一次即全量覆盖，Windows 重复跑只拖慢 CI"),
    "ruff format --check": ("linux", "同 ruff check"),
    "mypy": (
        "linux",
        "与平台无关；且 Windows 侧检出的多半是 CRLF/路径类噪声，ubuntu 跑一次即全量覆盖",
    ),
    "--fail-under=82": (
        "linux",
        "modelhub 分项覆盖率线读的是上一步 pytest 产出的 .coverage；"
        "windows job 不带 --cov，这条在那边**没有输入**，跑了只会读上一份陈旧数据。",
    ),
}


def test_the_two_jobs_differ_only_in_registered_ways(jobs: dict) -> None:
    """任何**未登记**的 job 覆盖差异都红。

    这条存在的理由：上一轮发现 windows job 少跑 5 步，其中 4 步是合理的
    平台无关取舍，1 步（selftest）是真缺口。修完之后"只差那 4 步"成了
    一句**写在注释里的话**——没有东西守它。这里把它变成数据 + 判据：

    · 想给某个 job 少跑一条  → 必须在上面登记并写明理由；
    · 想给某个 job 多跑一条  → 本条立刻红（先想清楚是不是漏登记）；
    · 改了 job 却没改登记  → 本条立刻红。
    """
    assert len(jobs) == 2, f"CI 现在有 {len(jobs)} 个 job：{sorted(jobs)}。判据按两个写死了。"

    # shell 控制流 / GitHub 表达式 / 装依赖，都不是门禁本身
    NOT_A_GATE = ("if ", "elif", "else", "fi", "}", "{", "set -euo", "pip install", "python -m pip")
    ends = (".ps1",)

    def probes(job: dict) -> set[str]:
        out: set[str] = set()
        for body in _run_bodies(job):
            for line in body.splitlines():
                t = line.strip()
                if not t or t.startswith("#"):
                    continue
                if t.startswith(NOT_A_GATE) or "${{" in t or t.endswith(ends):
                    continue
                out.add(t)
        return out

    names = sorted(jobs)
    linux_probes, win_probes = probes(jobs[names[0]]), probes(jobs[names[1]])

    def registered(cmd: str, side: str) -> bool:
        return any(
            (key in cmd or cmd.startswith(key)) and allowed in ("both", side)
            for key, (allowed, _why) in _ALLOWED_JOB_DIFFERENCES.items()
        )

    unregistered = sorted(
        f"{names[0]} 有 / {names[1]} 无: {cmd}"
        for cmd in linux_probes - win_probes
        if not registered(cmd, "linux")
    ) + sorted(
        f"{names[1]} 有 / {names[0]} 无: {cmd}"
        for cmd in win_probes - linux_probes
        if not registered(cmd, "windows")
    )
    assert not unregistered, (
        "两个 job 的覆盖差异超出登记表：\n"
        + "\n".join(f"  {c}" for c in unregistered)
        + "\n要么把它登记进 _ALLOWED_JOB_DIFFERENCES 并写明允许出现在哪一侧、为什么，"
        "要么把它补到缺的那一侧。别让差异停在「没人知道为什么」的状态。"
    )


# ---------------------------------------------------------------------------
# 判据三：这一步本身不许是空转的
# ---------------------------------------------------------------------------
def test_the_probe_strings_still_exist_in_the_workflow() -> None:
    """探针字符串写错时，上面几条会**假绿**（找都找不到，自然"没缺"）。

    先证明 CI 里确实还写着这些命令。
    """
    text = CI.read_text(encoding="utf-8")
    for label, probe in _ENTRYPOINT_GATES.items():
        assert probe in text, (
            f"ci.yml 里已经找不到「{label}」的命令 `{probe}` —— "
            "可能是被改名/移走了。那样判据一全部变成空转。"
        )
    assert "Topology selftest" in text, "selftest 那一步的标签也改了？同步本文件"


def test_the_lint_targets_do_not_count_as_running_the_gate() -> None:
    """`eval_prompts.py` 既是门禁、又是 ruff/mypy 的检查目标——别数混。

    早先那版探针用裸文件名，于是「节点提示词结构契约」被数成 4 次
    （ruff check / ruff format / mypy 三行里都出现了那个文件名）。
    这条把那个混淆钉死：探针必须带 `python ` 前缀才算「跑了入口」。
    """
    text = CI.read_text(encoding="utf-8")
    assert "eval_prompts.py" in text, "检查目标列表里本来就有它"
    assert "python eval_prompts.py" in text, "真正调用那一行不见了？"


# ---------------------------------------------------------------------------
# 判据五：文档里那份「提交前门禁块」与 CI **逐条同口径**
# ---------------------------------------------------------------------------
OPS = ROOT / "docs" / "operations.md"


def _docs_gate_block() -> list[str]:
    """取 operations.md「质量门禁（提交前）」那个 ```bash 块里的命令行。"""
    import re

    doc = OPS.read_text(encoding="utf-8")
    m = re.search(r"质量门禁（提交前）：\s*```bash\n(.*?)```", doc, re.S)
    assert m, "operations.md 里的门禁块不见了（标题或围栏改了？）"
    out = []
    for line in m.group(1).splitlines():
        t = line.strip()
        if not t or t.startswith("#"):
            continue
        t = re.split(r"\s+#", t)[0].strip()  # 去掉行尾说明性注释
        if t:
            out.append(t)
    return out


def test_the_docs_gate_block_actually_matches_ci() -> None:
    """operations.md 那句「与 ci.yml 逐条同口径」是**加粗断言**，此前没人验。

    本轮实测过当时是对的（12/12）。但"实测过"不等于"以后都对"——
    CI 加了步或文档漏抄，那句加粗就会变成假话，而且**没有任何东西会红**。

    ⇒ 这条把那句话变成判据：文档块里每条命令必须在 CI 里找得到；
    反向也查（CI 里该有的门禁不能只在文档里）。
    """
    import re

    jobs_all = yaml.safe_load(CI.read_text(encoding="utf-8"))["jobs"]
    ci_text = re.sub(
        r"[ \t]+",
        " ",
        "\n".join(
            str(s.get("run", ""))
            for j in jobs_all.values()
            for s in j.get("steps", [])
            if s.get("run")
        ),
    )
    missing_in_ci = [c for c in _docs_gate_block() if c not in ci_text]
    assert not missing_in_ci, (
        "operations.md 门禁块里的这些命令在 ci.yml 里找不到：\n"
        + "\n".join(f"  {c}" for c in missing_in_ci)
        + "\n那句「与 ci.yml 逐条同口径」要么改成真实的，要么把 CI 补齐。"
    )


def test_every_ci_gate_is_listed_in_the_docs_block() -> None:
    """反向：CI 里的门禁不许只活在 CI 里。

    少抄一条的后果很具体：有人照着文档跑"提交前门禁"，少跑一条，
    而且**心里是踏实���**——文档说是全的。
    """

    docs_cmds = set(_docs_gate_block())
    linux = yaml.safe_load(CI.read_text(encoding="utf-8"))["jobs"]["test"]
    gate_like = (
        "ruff check",
        "ruff format",
        "mypy",
        "pytest",
        "coverage report",
        "run.py",
        "eval_prompts.py",
        "run_e2e_stub",
        "git diff --exit-code",
    )
    undocumented = []
    for step in linux["steps"]:
        for line in str(step.get("run", "")).splitlines():
            t = line.strip()
            if not t or t.startswith("#") or "${{" in t:
                continue
            if t.startswith(("if", "elif", "else", "fi", "}", "{", "set -")):
                continue
            if not any(k in t for k in gate_like):
                continue
            # 同一门禁的**参数化分支**（`run.py gate --base <sha>` 的三种基线取法、
            # --json 变体）不算独立门禁——文档写一条 `python run.py gate` 即覆盖。
            if "--base" in t:
                continue
            if t.rstrip(";") not in {d.rstrip(";") for d in docs_cmds}:
                undocumented.append(t)
    assert not undocumented, (
        "ci.yml 里有这些门禁，operations.md 的门禁块里没列：\n"
        + "\n".join(f"  {c}" for c in undocumented)
        + "\n照文档跑的人会少跑这几步，而文档声称自己是全的。"
    )
