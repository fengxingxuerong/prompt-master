"""技能 / 插件 / Action 三条分发线的形状守卫（2026-10-26，第五轮竞品对照 PE-1）。

## 起因（本轮实测，不是推测）

GitHub Trending 周榜前 15 里有 6 条是技能/插件市场条目；本周新建、两天破千星的项目也几乎全是技能
（见 `research-reports/prompt-optimization-competitors-2026-10-26/report.md` §二）。
而本仓当时 **0 个 `SKILL.md` / `marketplace.json` / `plugin.json`**，PyPI 也 404 ——
能力在、入口不在。于是这一轮把"把已有 CLI 包起来"当头号短板（PE-1），本模块钉它的形状。

## 钉住什么（每条都对应上面那个缺口）

1. **技能正文**（`skills/prompt-master/SKILL.md`）：frontmatter 合法、名字与目录一致、
   并且**"默认只读、计费要明确授权"这条安全声明必须在**——它是"加载技能不授权花钱"的实现，
   丢了就等于让 Agent 顺手烧钱（Cloudflare 的技能也把这件事写成 operating modes，同一套理由）。
2. **插件清单**（`.claude-plugin/*.json`）：市场里指到的目录真实存在、两边名字一致、
   版本号与 `pm.__version__` 同一个事实源。**这是第三份版本号拷贝**：本仓已有产品版本与网关版本两个源，
   再多一处没有守卫就是下次发版必漂。
3. **Action**（`action.yml` + `scripts/action-prompt-gate.sh`）：composite、**只调零调用子命令**
   （计费闭环绝不进 CI）、声明的 output 与脚本写进 `$GITHUB_OUTPUT` 的键对得上，
   并且**脚本用 bash 真跑一遍**：临时 git 仓库里造一次真回归，看退出码是不是 1。

第 3 条为什么值得真跑：本仓有一条"不许发布没跑过的接线"的规矩（Round 34 为此拒绝过 Action）。
执行体因此落在 `.sh` 里而不是 `action.yml` 的多行 `run` 里——脚本能在本地跑，yml 不能。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from pm.cli.main import _SUBCOMMANDS

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "prompt-master" / "SKILL.md"
MARKETPLACE = ROOT / ".claude-plugin" / "marketplace.json"
PLUGIN = ROOT / ".claude-plugin" / "plugin.json"
ACTION = ROOT / "action.yml"
GATE_SH = ROOT / "scripts" / "action-prompt-gate.sh"


def _find_bash() -> str | None:
    """找 bash：PATH 上先找；Windows 上再找 Git for Windows 的常见落点。

    只认 `shutil.which("bash")` 的话，本机 Git Bash 明明装了却没进 PATH——
    用例会**静默跳过**，而"Action 跑不跑得起来"这件事就又回到没证据的状态。
    """
    found = shutil.which("bash")
    if found:
        return found
    if os.name == "nt":
        for cand in (
            r"C:\Program Files\Git\bin\bash.exe",
            r"C:\Program Files (x86)\Git\bin\bash.exe",
        ):
            if Path(cand).is_file():
                return cand
    return None


BASH = _find_bash()
needs_bash = pytest.mark.skipif(BASH is None, reason="没有 bash：Action 执行体只能在 POSIX 侧真跑")

# 用例文本与 tests/test_diff_base_ref.py 逐字同源：同一对 CLEAN/REGRESSED，
# 两处各写一份迟早漂，这里靠"复制自哪"的注释 + test_skill_and_gate_use_the_same_regression_pair 钉住。
CLEAN = (
    "[角色] 你是资深销售数据分析师。\n"
    "[任务] 依据用户提供的销售数据，输出趋势结论、异常点与下一步建议。\n"
    "[输出格式] Markdown 表格：区域｜销售额｜同比，随后给出 3 条结论。\n"
    "[边界处理] 输入完全为空时标注「数据缺失」；仅部分字段缺失时已有数据照常输出。"
)

REGRESSED = (
    "[角色] 你是资深销售数据分析师，负责把区域销售数据整理成可决策的结论。\n"
    "[任务] 依据用户提供的销售数据，输出趋势结论、异常点与下一步建议三部分内容。\n"
    "[输出格式] Markdown 表格：区域｜销售额｜同比，随后给出 3 条结论。\n"
    "[判断规则] 所有结论必须引用输入中的具体数值，禁止只给定性描述。\n"
    "[边界处理] 输入异常时停止处理，不输出任何结论。\n"
    "[补充说明] 结论按重要性排序，每条不超过 50 字，避免重复表述同一现象。"
)


def _frontmatter() -> dict:
    text = SKILL.read_text(encoding="utf-8")
    assert text.startswith("---\n"), "SKILL.md 必须以 YAML frontmatter 开头"
    _, fm, _body = text.split("---\n", 2)
    return yaml.safe_load(fm)


def test_skill_frontmatter_matches_directory_name() -> None:
    """技能名必须等于目录名：Agent 按目录发现技能，两边不一致就会出现"装了但叫不醒"。"""
    fm = _frontmatter()
    assert fm.get("name") == SKILL.parent.name
    assert fm["name"].islower() and " " not in fm["name"], "技能名要 kebab-case（Agent 侧惯例）"
    desc = fm.get("description") or ""
    assert desc, "description 是 Agent 决定要不要加载本技能的唯一依据，不能空"
    assert len(desc) <= 1024, f"description {len(desc)} 字，超过常见的 1024 上限会被截断"


def test_skill_keeps_the_read_only_default_and_the_paid_consent_rule() -> None:
    """安全声明不许被删：默认零调用，计费闭环要用户明确点头。

    这条丢了不会让任何测试变红、只会让 Agent 顺手跑一轮 45~102 次调用的闭环 ——
    所以它必须是一条判据，而不是文档里的好习惯。
    """
    body = SKILL.read_text(encoding="utf-8")
    assert "授权花钱" in body, "少了「加载本技能不授权花钱」这条运行模式声明"
    assert "明确要求" in body, "少了「计费只在用户明确要求时才走」这句"
    assert "--dry-run" in body, "计费前先报调用数的做法要在技能里写明"
    assert "零调用" in body and "零出网" in body


def test_skill_links_only_to_existing_docs() -> None:
    """技能里指向 `docs/*.md` 的链接必须真的存在：分发出去以后没人替你改死链。"""
    body = SKILL.read_text(encoding="utf-8")
    targets = set(re.findall(r"\((?:\.\./\.\./)?(docs/[A-Za-z0-9_\-/]+\.md)\)", body))
    assert targets, "技能里应当给出「去哪读更多」的文档链接"
    missing = sorted(t for t in targets if not (ROOT / t).is_file())
    assert not missing, f"技能指向了不存在的文档：{missing}"


def test_doc_images_exist_and_are_referenced_where_they_matter() -> None:
    """三个对外文档里的图片必须真实存在，且 SKILL/README 都要有那张演示图。

    两条判据，各挡一种真实的坏法（2026-10-27，第六轮竞品对照的 PF-1a）：

    ① **路径必须存在**：图片是"作者看不见"的那类坏 —— 仓库里图片没了，markdown 语法照样合法，
       本地看起来也没事，只有别人打开时才是一个裂图。
    ② **演示图必须挂在门口**：第六轮实测，本仓 0★ 而同期破千星的技能仓全都带演示资产；
       README 中/英与 SKILL.md 是三个"第一眼"入口，任缺一处就等于没有。

    演示图本身是 `prompt-master check` 的**真实输出**渲染成的（见 docs/assets/check-demo.png）。
    """
    docs = {
        "README.md": ROOT / "README.md",
        "README.en.md": ROOT / "README.en.md",
        "SKILL.md": SKILL,
    }
    problems: list[str] = []
    for label, path in docs.items():
        text = path.read_text(encoding="utf-8")
        # markdown 图片语法；相对路径以该文件所在目录为基准
        refs = re.findall(r"!\[[^\]]*\]\(([^)\s]+)\)", text)
        assert refs, f"{label} 里没有任何演示/示意图 —— 第六轮实测：0★ 与'门口没图'同时成立"
        assert any("check-demo.png" in r for r in refs), f"{label} 没挂那张审计演示图"
        for ref in refs:
            if ref.startswith(("http://", "https://")):
                continue
            resolved = (path.parent / ref).resolve()
            if not resolved.is_file():
                problems.append(f"{label} 指向了不存在的图片：{ref} → {resolved}")
    assert not problems, problems
    # 图片本身不能是空文件（0 字节的 PNG 同样是坏图，但路径检查看不出来）
    png = ROOT / "docs" / "assets" / "check-demo.png"
    assert png.stat().st_size > 5000, f"演示图只有 {png.stat().st_size} 字节，几乎肯定渲染失败"


def test_marketplace_points_at_an_existing_plugin_whose_name_matches() -> None:
    """市场条目 → 插件目录 → 插件名，三者必须闭合。

    形状抄自两份真在用的清单（本轮实测原文存在 `research-reports/.../raw/`）：
    iFixAi 的 `.claude-plugin/marketplace.json` 与 Cursor 的同一文件。
    """
    market = json.loads(MARKETPLACE.read_text(encoding="utf-8"))
    assert market.get("name") and market.get("owner"), "市场清单要有 name 与 owner"
    plugins = market.get("plugins") or []
    assert plugins, "市场里至少要有本产品这一条"
    for entry in plugins:
        src = (ROOT / str(entry["source"])).resolve()
        assert src.is_dir(), f"市场指到的目录不存在：{entry['source']}"
        manifest_path = src / ".claude-plugin" / "plugin.json"
        assert manifest_path.is_file(), f"{entry['source']} 里没有 .claude-plugin/plugin.json"
        assert manifest_path.resolve() == PLUGIN.resolve(), (
            "市场指到的清单不是仓根那份 —— 出现第二份插件清单就会两边漂"
        )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest["name"] == entry["name"], "市场条目名与插件清单名不一致"
        skills_dir = (src / str(manifest.get("skills", "./skills/"))).resolve()
        assert (skills_dir / "prompt-master" / "SKILL.md").is_file(), (
            f"插件声明的技能目录 {skills_dir} 里没有 prompt-master/SKILL.md"
        )
        assert manifest.get("license") == "MIT", "许可证要与仓库 LICENSE 同一个事实源"
        assert manifest.get("skills") == "./skills/", "插件要声明自己的技能目录"
        # 版本号不在这里查：它是"第三份版本拷贝"，归口在 tests/test_version_single_source.py
        # （版本纪律只该有一个家，两份断言同一件事迟早互相打架）。


def _action() -> dict:
    return yaml.safe_load(ACTION.read_text(encoding="utf-8"))


def _executable_lines(text: str) -> str:
    """去掉整行注释后再做禁用词扫描。

    为什么需要这层：本仓的脚本习惯把"不做的事"写成注释（执行体头部就有
    "不做的事（有意）：不跑计费闭环（没有 `--task` / `submit`）"）——
    字符串级扫描会把**声明不这么做**的注释读成违规。本轮检修实测的假阳性，
    修法是"只扫会执行的行"，不是把注释删掉（注释恰恰是这套判断的说明书）。
    """
    return "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))


def test_action_and_its_script_touch_only_zero_call_entrypoints() -> None:
    """CI 里不许出现计费动作 —— 而且 **`action.yml` 与它调的那支脚本都要查**。

    为什么必须查脚本（本轮检修实测的洞）：`action.yml` 的 `run:` 只有一行
    （`bash …/action-prompt-gate.sh`），真正决定跑什么的是脚本正文。
    只按 yml 查的话，往脚本里加一句 `$PM_CLI --task …` **不会有任何用例变红** ——
    那样"零调用"就只是一句注释，不是判据。

    这条不是洁癖：Action 会在别人的仓库、别人的 PR 上跑，一按就花钱的接线不该存在于 CI。
    """
    action = _action()
    assert action["runs"]["using"] == "composite", "用 composite，别要求用户先装个 JS 运行时"
    runs = "\n".join(str(s.get("run", "")) for s in action["runs"]["steps"])
    script = GATE_SH.read_text(encoding="utf-8")
    surface = runs + "\n" + _executable_lines(script)
    for forbidden in ("--task", "--preflight", "optimize_submit", " submit", "PM_API_KEY"):
        assert forbidden not in surface, f"Action 或脚本里出现了计费/凭据相关写法：{forbidden!r}"
    assert "action-prompt-gate.sh" in runs, "Action 应当调仓内脚本（逻辑要能在本地跑）"

    invoked = set(re.findall(r"\$PM_CLI\s+([a-z][a-z-]*)", script))
    assert invoked, "脚本里找不到 `$PM_CLI <子命令>` 调用 —— 那这条闸查了个寂寞"
    assert invoked <= set(_SUBCOMMANDS), f"脚本调的子命令不在活体分发器里：{invoked}"
    zero_call = {"check", "diff", "gate", "doctor", "history", "calibrate", "library"}
    assert invoked <= zero_call, f"脚本调了会花钱的子命令：{invoked - zero_call}"


def test_action_outputs_match_the_keys_the_script_writes() -> None:
    """声明的 output 与脚本写进 `$GITHUB_OUTPUT` 的键必须对得上。

    对不上时的表现是"Action 跑成功、output 恒空"——静默的，靠人看日志发现不了。
    """
    action = _action()
    declared = set(action["outputs"])
    script = GATE_SH.read_text(encoding="utf-8")
    written = set()
    for line in script.splitlines():
        stripped = line.strip()
        if stripped.startswith("echo ") and "=" in stripped:
            written.add(stripped.split("=", 1)[0].removeprefix("echo ").strip('"'))
    assert declared <= written, f"声明了却没写：{declared - written}；脚本实际写的：{written}"
    gate_steps = [s for s in action["runs"]["steps"] if s.get("id") == "gate"]
    assert len(gate_steps) == 1, "脚本必须挂在一个有 id 的 step 上，否则 output 接不出来"


def test_skill_and_gate_use_the_same_regression_pair() -> None:
    """本模块的 CLEAN/REGRESSED 与 tests/test_diff_base_ref.py 逐字一致。

    两份用例文本一旦分叉，"Action 拦住了回归"这句话就只在其中一份上成立。
    """
    src = (ROOT / "tests" / "test_diff_base_ref.py").read_text(encoding="utf-8")
    for name, text in (("CLEAN", CLEAN), ("REGRESSED", REGRESSED)):
        for line in text.splitlines():
            assert line in src, f"{name} 的这行已与 test_diff_base_ref.py 分叉：{line}"


@needs_bash
def test_action_gate_script_is_bash_syntax_clean() -> None:
    """`bash -n`：脚本得先是合法 bash。语法错误在 CI 里只会表现为"step 挂了"，看不出原因。"""
    proc = subprocess.run(
        [BASH, "-n", str(GATE_SH)], capture_output=True, text=True, encoding="utf-8", timeout=60
    )
    assert proc.returncode == 0, proc.stderr


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )


def _configure_identity(repo: Path) -> None:
    """给仓库装上提交身份：克隆出来的仓库**不带**上游的 user.email/name，
    少了这两行，用例会在 `git commit` 上以 rc=128 挂掉（本轮检修实测的假红）。"""
    _git(repo, "config", "user.email", "ci@example.invalid")
    _git(repo, "config", "user.name", "CI")


def _seed_repo(repo: Path) -> None:
    (repo / "prompts").mkdir(parents=True)
    _git(repo, "init", "-q", ".")
    _configure_identity(repo)
    (repo / "prompts" / "p.md").write_text(CLEAN, encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")


def _clone(source: Path, dest: Path) -> Path:
    subprocess.run(
        ["git", "clone", "-q", str(source), str(dest)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    _configure_identity(dest)
    return dest


def _posix(path: Path) -> str:
    """转成 bash 认得的路径：Windows 下走 cygpath，POSIX 下原样。

    不转的话，`command -v "D:/…/python.exe"` 在 Git Bash 里能不能认全看运气——
    探针不该把"平台差异"混进"接线对不对"这个结论里。
    """
    cygpath = shutil.which("cygpath")
    if os.name == "nt" and cygpath:
        proc = subprocess.run(
            [cygpath, "-u", str(path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
    return Path(path).as_posix()


def _run_gate(
    repo: Path, *, paths: str, base_ref: str
) -> tuple[subprocess.CompletedProcess[str], str]:
    """起一次 Action 执行体：真 git 仓库 + 真 CLI + 真 GITHUB_OUTPUT。"""
    out_file = repo / "ghout.txt"
    env = dict(os.environ)
    env.update(
        {
            "PM_PATHS": paths,
            "PM_BASE_REF": base_ref,
            # 这里让脚本调 **本仓的 run.py**（而不是 PATH 上装好的 prompt-master）：
            # 测的是接线与判据，不是"包装没装对"，后者由 tests/test_packaging.py 管。
            "PM_CLI": f"{_posix(sys.executable)} {_posix(ROOT / 'run.py')}",
            "GITHUB_OUTPUT": _posix(out_file),
        }
    )
    proc = subprocess.run(
        [BASH, str(GATE_SH)],
        cwd=str(repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
        env=env,
    )
    return proc, out_file.read_text(encoding="utf-8") if out_file.exists() else ""


@needs_bash
def test_action_gate_flags_a_real_regression_and_passes_a_clean_file(tmp_path) -> None:
    """端到端三条路径：真回归退 1、干净退 0、新增文件跳过（且明说"不算通过"）。

    这条是本轮唯一能证明 Action 接线**真的工作**的东西：造一次真回归，看它是不是拦下来了。
    """
    repo = tmp_path / "user-repo"
    _seed_repo(repo)

    (repo / "prompts" / "p.md").write_text(REGRESSED, encoding="utf-8")
    proc, ghout = _run_gate(repo, paths="prompts/p.md", base_ref="HEAD")
    assert proc.returncode == 1, f"回归没被拦下：{proc.stdout}\n{proc.stderr}"
    assert "::error file=prompts/p.md" in proc.stdout
    assert "regressed=1" in ghout and "checked=1" in ghout
    assert "suppressive_rule" in proc.stdout, "要能看见是哪条规则被新引入"

    _git(repo, "checkout", "-q", "--", "prompts/p.md")
    proc, ghout = _run_gate(repo, paths="prompts/p.md", base_ref="HEAD")
    assert proc.returncode == 0, f"干净版反被判红：{proc.stdout}\n{proc.stderr}"
    assert "regressed=0" in ghout

    (repo / "prompts" / "new.md").write_text(CLEAN, encoding="utf-8")
    proc, ghout = _run_gate(repo, paths="prompts/new.md", base_ref="HEAD")
    assert proc.returncode == 0, "新增文件在基线里没有对照物，不该判红"
    assert "skipped=1" in ghout and "checked=0" in ghout
    assert "不计入通过" in proc.stdout, "跳过要明说，不能让人误以为它通过了"


@needs_bash
def test_action_gate_refuses_to_guess_when_inputs_are_wrong(tmp_path) -> None:
    """空 paths / 解析不了的基线 → rc 2 并给人话（与 CLI 的四态契约同口径，不另立一套）。"""
    repo = tmp_path / "user-repo"
    _seed_repo(repo)

    proc, _ = _run_gate(repo, paths="", base_ref="HEAD")
    assert proc.returncode == 2 and "inputs.paths 为空" in proc.stdout

    proc, _ = _run_gate(repo, paths="prompts/p.md", base_ref="origin/nope")
    assert proc.returncode == 2 and "不是本仓库能解析的引用" in proc.stdout


@needs_bash
def test_action_gate_finds_a_baseline_by_itself_when_none_is_given(tmp_path) -> None:
    """不给 base-ref 时执行体要自己找基线 —— README 推荐的用法恰恰是不给。

    上面那条覆盖的是显式 `base-ref`；这条盖**自动探测**。克隆后先补一个提交，
    让 HEAD 严格晚于 `origin/main`（CI 里的常态：检出的是 PR 头，基线是更早的主干），
    否则会自动探测会（正确地）拒绝 —— 那条路见下一条用例。
    """
    upstream = tmp_path / "upstream"
    _seed_repo(upstream)
    clone = _clone(upstream, tmp_path / "clone")
    _git(clone, "commit", "-q", "--allow-empty", "-m", "head is later than origin/main")
    (clone / "prompts" / "p.md").write_text(REGRESSED, encoding="utf-8")

    proc, ghout = _run_gate(clone, paths="prompts/p.md", base_ref="")
    assert "基线 = origin/" in proc.stdout, f"没能自动找到远端基线：{proc.stdout}\n{proc.stderr}"
    assert proc.returncode == 1, f"相对远端基线的回归没被拦下：{proc.stdout}\n{proc.stderr}"
    assert "regressed=1" in ghout


@needs_bash
def test_action_gate_refuses_an_auto_baseline_that_is_just_head(tmp_path) -> None:
    """自动探测到的候选**全都等于 HEAD** 时必须退 2，绝不许"比对自己"然后报绿。

    这条是本轮检修里最要紧的一条：`pm/promptgate.py::resolve_base` 在 2026-10-02
    记过一次同款真实缺陷 —— 单提交仓库里 `master` 能解析且**就是 HEAD**，
    拿它当基线 diff 出来永远是"没有改动"，门禁**静默通过**，且长得像一次成功的检查。
    执行体当时的候选表里正好只有远端 ref + `HEAD~1`，这条 2026-10-26 才对齐过来，
    所以必须有用例把它钉住（否则下次有人"顺手加个 origin/HEAD 候选"就会复发）。
    """
    upstream = tmp_path / "upstream"
    _seed_repo(upstream)
    clone = _clone(upstream, tmp_path / "clone")
    (clone / "prompts" / "p.md").write_text(REGRESSED, encoding="utf-8")

    proc, ghout = _run_gate(clone, paths="prompts/p.md", base_ref="")
    assert proc.returncode == 2, (
        f"候选基线全都等于 HEAD 时必须吵，不能报绿：rc={proc.returncode}\n{proc.stdout}"
    )
    assert "全都等于当前 HEAD" in proc.stdout and "假绿" in proc.stdout
    assert "regressed=0" not in ghout, "没有可比对象时不该产出'没发现回归'的 output"


@needs_bash
def test_action_gate_treats_the_all_zero_sha_as_no_baseline(tmp_path) -> None:
    """`github.event.before` 在"新分支首次推送"时是全零 SHA —— 与 `gate` 的 rc=3 同口径。

    那条路是**客观没有基线**（放行 + 横幅），不是"检查通过"，也不是配置错。
    不区分的话，首次推送会被判红（假红），而用户读到的是一句"引用解析不了"。
    """
    repo = tmp_path / "user-repo"
    _seed_repo(repo)
    (repo / "prompts" / "p.md").write_text(REGRESSED, encoding="utf-8")

    proc, ghout = _run_gate(repo, paths="prompts/p.md", base_ref="0" * 40)
    assert proc.returncode == 0, f"首次推送被误判成红：{proc.stdout}\n{proc.stderr}"
    assert "首次推送" in proc.stdout
    assert "checked=0" in ghout and "skipped=1" in ghout, "没比过的文件不该算进 checked"
