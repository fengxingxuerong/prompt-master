"""提示词改动的 CI 回归门禁（`pm/promptgate.py` + `gate` 子命令）的回归。

这块的价值全在"**它必须能红、也必须只在该红的时候红**"：
- 能红：真引入回归时必须拦（否则门禁是装饰）；
- 不假红：16/17 个现存模板天然命中规则（它们是模板），绝对判据会永久假红。
这两条相互拉扯，所以本文件里的**变异测试**（注入回归 → 断言变红）与
**假红测试**（无回归 → 断言放行）同等重要，缺一边都会让门禁变成"永远绿"或"永远红"。

夹具说明：门禁的输入是**Python 源码**（`pm/prompts.py`），所以夹具必须写成
`NAME = "..."` 的合法常量形状，不能直接写中文提示词正文（pytest 收集阶段就会 SyntaxError）。
`_py_const()` 负责这层包装，`CLEAN_BODY` / `DIRTY_BODY` 是正文。⚠️ 改夹具时别忘了这一层。
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from pm.cli.gatecmd import gate_command
from pm.cli.main import _SUBCOMMANDS, _subcommand_help
from pm.promptgate import (
    DEFAULT_PATHS,
    GateError,
    NoBaseline,
    extract_templates,
    gate,
    read_revision,
    render_text,
    resolve_base,
)

ROOT = Path(__file__).resolve().parents[1]
RUN = [sys.executable, str(ROOT / "run.py")]

# 干净模板正文：零规则命中（用于"干净→脏"变异，判定最直观）
CLEAN_BODY = "[角色] 你是成对评审员。\n[任务] 比较两份候选输出并选出更好的那一份。"
# 本来就命中 suppressive_rule 的模板正文（引号内是"禁止示例列举"，不是真条款）
DIRTY_BODY = (
    "[角色] 你是提示词工程师。\n[规则] 不写「停止处理」「不输出任何结论」这类条款——\n"
    "它们会压低任务完成度。\n[任务] 生成一份结构清晰的提示词骨架。"
)


def _py_const(body: str, name: str = "REVIEWER_SYSTEM") -> str:
    """把提示词正文包成合法的 `NAME = "..."` 常量（门禁抽取的对象形状）。

    换行必须转义成 `\\n`：`CLEAN_BODY` 里的 `\\n` 在测试进程里是**真实换行**，
    直接写进双引号里就是 "unterminated string literal"（实测踩过）。
    ast 解析后会还原成同一个正文，所以断言仍然对得上。
    """
    escaped = body.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'{name} = "{escaped}"\n'


CLEAN_T = _py_const(CLEAN_BODY)
DIRTY_T = _py_const(DIRTY_BODY)


def _clean_plus(extra: str, name: str = "REVIEWER_SYSTEM") -> str:
    """在干净模板正文后追加内容，再包成合法常量（变异测试统一入口）。"""
    return _py_const(CLEAN_BODY + extra, name)


def _dirty_plus(extra: str) -> str:
    return _py_const(DIRTY_BODY + extra)


def _init_repo(d: Path) -> None:
    """建一个已提交的临时 git 仓库（门禁需要真 git）。"""
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    subprocess.run(["git", "init", "-q"], cwd=d, check=True)
    subprocess.run(["git", "add", "-A"], cwd=d, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=d, check=True, env=env)


def _run_gate(cwd: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [*RUN, "gate", "--base", "HEAD", "--path", "prompts.py", *extra, "--json"],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )


# --------------------------------------------------------------------------
# 抽取层：门禁"看不见对象"就等于没在检查
# --------------------------------------------------------------------------
def test_extract_templates_takes_upper_string_constants():
    src = 'A = "hello"\nB = "x" "y"\nC = f"z{1}"\nlower = "no"\nD: str = "typed"\n'
    got = extract_templates(src)
    assert got["A"] == "hello"
    assert got["B"] == "xy"  # 隐式拼接会被解析器折成单个常量
    assert got["D"] == "typed"
    assert "C" not in got  # f-string 不是静态文本
    assert "lower" not in got


def test_extract_templates_raises_on_syntax_error():
    with pytest.raises(GateError, match="无法解析"):
        extract_templates("def broken(:\n")


def test_gate_refuses_to_pass_when_nothing_extracted(tmp_path):
    """抽取不到模板 → 必须报错，**绝不能绿着通过**（这是门禁最危险的失效方式）。"""
    (tmp_path / "prompts.py").write_text("lower = 'no upper constants'\n", encoding="utf-8")
    _init_repo(tmp_path)
    with pytest.raises(GateError, match="抽取不到任何"):
        gate(base_ref="HEAD", paths=("prompts.py",), root=tmp_path)


# --------------------------------------------------------------------------
# 基线解析
# --------------------------------------------------------------------------
def test_zero_sha_is_first_push_not_an_error():
    """全零 SHA = 新分支首次推送，客观上没有基线 —— 这是**唯一**允许跳过的情形。"""
    with pytest.raises(NoBaseline, match="首次推送"):
        resolve_base("0" * 40, ROOT)


def test_explicit_unknown_base_is_a_gate_error():
    with pytest.raises(GateError, match="解析不出来"):
        resolve_base("__不存在的ref__", ROOT)


def test_auto_discovery_refuses_to_use_head_as_its_own_baseline(tmp_path):
    """**实测缺陷**（2026-10-02）：单提交仓库里 `master` 能解析，而它**就是 HEAD**。

    拿它当基线，diff 出来的永远是"没有改动" → 门禁静默通过。
    这比报错危险得多：它长得像一次成功的检查。自动探测必须跳过 HEAD 自己。
    """
    (tmp_path / "prompts.py").write_text(CLEAN_T, encoding="utf-8")
    _init_repo(tmp_path)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True, text=True
    ).stdout.strip()
    with pytest.raises(GateError, match=r"等于当前 HEAD|找不到可用的基线"):
        resolve_base(None, tmp_path, head_sha=head)


def test_explicit_head_base_is_still_allowed(tmp_path):
    """显式 --base HEAD 是正当用法（本地对比"已提交版 vs 工作区"），不能被上面的规则误伤。"""
    (tmp_path / "prompts.py").write_text(CLEAN_T, encoding="utf-8")
    _init_repo(tmp_path)
    sha, _ = resolve_base("HEAD", tmp_path)
    assert sha


def test_read_revision_returns_none_for_absent_file():
    assert read_revision("HEAD", "__不存在__.py", ROOT) is None


# --------------------------------------------------------------------------
# 假红防线：这两条决定门禁能不能长期留在 CI 里
# --------------------------------------------------------------------------
def test_real_prompts_file_has_no_regression_against_head_minus_1():
    """本仓库当前 HEAD 相对 HEAD~1 不得报回归（否则门禁一进 CI 就是红的）。"""
    result = gate(base_ref="HEAD~1", root=ROOT)
    assert result["skipped"] is False
    assert result["regressed"] is False, [
        (c["name"], c["introduced"], c["added_hits"]) for c in result["changed"]
    ]


def test_gate_does_not_false_red_on_untouched_file(tmp_path):
    (tmp_path / "prompts.py").write_text(CLEAN_T + "X = 1\n", encoding="utf-8")
    _init_repo(tmp_path)
    result = gate(base_ref="HEAD", paths=("prompts.py",), root=tmp_path)
    assert result["regressed"] is False
    assert result["unchanged_count"] == 1
    assert result["changed"] == []


def test_new_template_is_disclosed_not_reported_as_regression(tmp_path):
    """新增模板没有基线可比 —— 报成"新引入"会是假红，必须只披露不判定。"""
    f = tmp_path / "prompts.py"
    f.write_text(CLEAN_T, encoding="utf-8")
    _init_repo(tmp_path)
    f.write_text(
        CLEAN_T + _py_const("[角色] 你是新助手。\n[任务] 输出结论。", "NEW_SYS"),
        encoding="utf-8",
    )
    result = gate(base_ref="HEAD", paths=("prompts.py",), root=tmp_path)
    assert result["regressed"] is False
    assert len(result["added_templates"]) == 1
    assert "NEW_SYS" in result["added_templates"][0]
    assert any("没有基线可比" in n for n in result["notes"])


def test_removed_template_is_disclosed(tmp_path):
    f = tmp_path / "prompts.py"
    f.write_text(CLEAN_T + _py_const("[角色] 你是将被删掉的模板。", "GONE"), encoding="utf-8")
    _init_repo(tmp_path)
    f.write_text(CLEAN_T, encoding="utf-8")
    result = gate(base_ref="HEAD", paths=("prompts.py",), root=tmp_path)
    assert result["removed_templates"]
    assert any("删除或改名" in n for n in result["notes"])


# --------------------------------------------------------------------------
# 变异测试：真引入回归必须变红（这是门禁存在的理由）
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "label,after,expect_code",
    [
        (
            "干净模板加入抑制型条款",
            _clean_plus("\n[边界] 输入异常时停止处理，不输出任何结论。"),
            "suppressive_rule",
        ),
        ("写进本系统的元话语", _clean_plus("\n你是世界顶级的提示词工程师。"), "meta_leak"),
        ("混入未渲染占位符", _clean_plus("\n参考 <<task_description>>。"), "context_leak"),
        (
            "重复开标签（真实事故形状）",
            _clean_plus("\n<输入>数据A\n<输入>数据B\n</输入>"),
            "delimiter_unbalanced",
        ),
    ],
)
def test_mutations_are_caught(tmp_path, label, after, expect_code):
    f = tmp_path / "prompts.py"
    f.write_text(CLEAN_T, encoding="utf-8")
    _init_repo(tmp_path)
    f.write_text(after, encoding="utf-8")
    result = gate(base_ref="HEAD", paths=("prompts.py",), root=tmp_path)
    assert result["regressed"] is True, f"{label} 未被拦截"
    codes = {c for ch in result["changed"] for c in ch["introduced"]}
    assert expect_code in codes, f"{label}：期望 {expect_code}，实得 {codes}"


def test_adding_a_real_clause_to_an_already_dirty_template_is_caught(tmp_path):
    """**实测盲区**（2026-10-02）：模板本来就命中 suppressive_rule 时，
    再往里塞一条真的抑制条款，code 集合前后完全相同 → 只比 code 的判据判为"无回归"。

    这门禁恰好对它最该管的模板失效，所以必须有命中项级通道。
    """
    f = tmp_path / "prompts.py"
    f.write_text(DIRTY_T, encoding="utf-8")
    _init_repo(tmp_path)
    # 补一条**真条款**（不在引号里，与文件里那段"禁止示例列举"性质完全不同）
    f.write_text(_dirty_plus("\n[边界] 若输入异常则停止处理，不输出任何结论。"), encoding="utf-8")
    result = gate(base_ref="HEAD", paths=("prompts.py",), root=tmp_path)
    assert result["regressed"] is True
    assert result["introduced_total"] == 0, "code 级确实看不出变化（这正是盲区所在）"
    assert result["added_hits_total"] > 0, "必须由命中项级通道抓住"


def test_quoted_examples_are_not_treated_as_real_clauses():
    """引号里的**禁止示例列举**不是真条款：本来就存在的引用不该被算成命中项，
    否则基线满命中、任何改动看起来都"没变化"（实测就是这么被掩盖的）。"""
    from pm.promptdiff import rule_signals

    assert "suppressive_rule" not in rule_signals(DIRTY_BODY)


def test_wording_polish_is_not_a_regression(tmp_path):
    """只是润色措辞 → 必须放行。门禁不回答"够不够好"。"""
    f = tmp_path / "prompts.py"
    f.write_text(CLEAN_T, encoding="utf-8")
    _init_repo(tmp_path)
    f.write_text(_py_const(CLEAN_BODY.replace("成对评审员", "严格的成对评审员")), encoding="utf-8")
    result = gate(base_ref="HEAD", paths=("prompts.py",), root=tmp_path)
    assert result["regressed"] is False
    assert len(result["changed"]) == 1  # 有改动被记下，但不判失败


def test_fixing_a_problem_is_not_a_regression(tmp_path):
    """修掉问题（fixed 非空）不判失败 —— 门禁只拦"变坏"。"""
    f = tmp_path / "prompts.py"
    f.write_text(_clean_plus("\n[边界] 输入异常时停止处理。"), encoding="utf-8")
    _init_repo(tmp_path)
    f.write_text(_clean_plus("\n[边界] 输入异常时标注缺失并继续输出其余内容。"), encoding="utf-8")
    result = gate(base_ref="HEAD", paths=("prompts.py",), root=tmp_path)
    assert result["regressed"] is False
    assert any(c["fixed"] for c in result["changed"])


# --------------------------------------------------------------------------
# 子命令入口与退出码（CI 唯一的判据面）
# --------------------------------------------------------------------------
def test_command_exit_codes_via_real_entry(tmp_path):
    f = tmp_path / "prompts.py"
    f.write_text(CLEAN_T, encoding="utf-8")
    _init_repo(tmp_path)

    clean = _run_gate(tmp_path)
    assert clean.returncode == 0, clean.stderr[-300:]
    assert json.loads(clean.stdout)["regressed"] is False

    f.write_text(_clean_plus("\n[边界] 输入异常时停止处理，不输出任何结论。"), encoding="utf-8")
    dirty = _run_gate(tmp_path)
    assert dirty.returncode == 1, dirty.stdout[-400:]
    payload = json.loads(dirty.stdout)
    assert payload["mode"] == "prompt_gate" and payload["regressed"] is True


def test_gate_error_exit_is_two_not_one(tmp_path):
    """门禁自己没跑起来（基线解析不出）必须是 2：与"提示词有回归"分开报。"""
    (tmp_path / "prompts.py").write_text(CLEAN_T, encoding="utf-8")
    _init_repo(tmp_path)
    r = subprocess.run(
        [*RUN, "gate", "--base", "__不存在的ref__", "--path", "prompts.py", "--json"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    assert r.returncode == 2, (r.stdout[-300:], r.stderr[-300:])
    assert "gate_error" in json.loads(r.stdout)


def test_gate_works_without_any_api_key(tmp_path):
    """零调用承诺：门禁必须在无 Key、不出网时可用，否则它进不了 CI 的第一步。"""
    (tmp_path / "prompts.py").write_text(CLEAN_T, encoding="utf-8")
    _init_repo(tmp_path)
    r = subprocess.run(
        [*RUN, "gate", "--base", "HEAD", "--path", "prompts.py"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        env={
            **os.environ,
            "PM_API_KEY": "",
            "PM_TARGET_API_KEY": "",
            "PM_API_TOKEN": "",
            "PM_FAKE_BACKEND": "",
        },
    )
    assert r.returncode == 0, (r.stdout[-300:], r.stderr[-300:])


def test_command_usage_error_exits_two():
    with pytest.raises(SystemExit) as e:
        gate_command(["--base"])  # 缺值
    assert e.value.code == 2


def test_require_baseline_turns_skip_into_failure():
    """首次推送默认放行，但 --require-baseline 要能把它变成失败（严格门禁场景）。"""
    assert gate_command(["--base", "0" * 40, "--json", "--require-baseline"]) == 2


def test_first_push_skips_but_never_claims_pass(capsys):
    code = gate_command(["--base", "0" * 40])
    out = capsys.readouterr().out
    assert code == 0
    # 措辞必须说清"没做判定"，否则"跳过"会被读成"通过"
    assert "跳过" in out and "不等于「没回归」" in out


def test_render_text_states_what_was_checked():
    out = render_text(gate(base_ref="HEAD~1", root=ROOT))
    assert "基线" in out and "检查对象" in out and "未改动模板" in out
    assert "pm/prompts.py" in out


def test_help_lists_gate_subcommand():
    assert "gate" in _SUBCOMMANDS
    assert "gate" in _subcommand_help("run.py")


def test_default_paths_point_at_the_real_prompt_asset():
    """默认门禁对象必须真的存在且有模板 —— 配置漂了就等于门禁空转。"""
    for p in DEFAULT_PATHS:
        f = ROOT / p
        assert f.exists(), f"默认检查对象 {p} 不存在"
        assert extract_templates(f.read_text(encoding="utf-8")), f"{p} 里抽不到模板"


def test_prompts_module_still_holds_all_templates():
    """门禁对象是 `pm/prompts.py`；若模板被挪走，这条会红，提醒同步 DEFAULT_PATHS。"""
    names = set(extract_templates((ROOT / "pm" / "prompts.py").read_text(encoding="utf-8")))
    assert {
        "CLARIFIER_SYSTEM",
        "OPTIMIZER_SYSTEM",
        "REFINER_SYSTEM",
        "MOCKGEN_SYSTEM",
        "EVALUATOR_SYSTEM",
        "REVISER_SYSTEM",
        "COMPARATOR_SYSTEM",
    } <= names


def test_extraction_is_static_not_execution():
    """抽取必须是静态的：执行基线代码会在 import 副作用上炸，也可能根本装不上。"""
    tree = ast.parse((ROOT / "pm" / "promptgate.py").read_text(encoding="utf-8"))
    assert any(
        isinstance(n, ast.Import) and any(a.name == "ast" for a in n.names) for n in tree.body
    )


def test_absolute_path_is_normalized_not_silently_skipped(tmp_path):
    """**实测缺陷**（2026-10-02）：--path 传绝对路径时 `git show <ref>:<abs>` 永远失败
    → 基线取不到 → 所有模板被算成"本次新增" → 判定为空、**静默放行**。

    它长得像"没有回归"，实际是"根本没比"。CI 里路径是相对的所以看不出问题，
    但本地用绝对路径调试的人会拿到一个假的绿灯。
    """
    f = tmp_path / "prompts.py"
    f.write_text(CLEAN_T, encoding="utf-8")
    _init_repo(tmp_path)
    f.write_text(_clean_plus("\n[边界] 输入异常时停止处理，不输出任何结论。"), encoding="utf-8")

    abs_path = str(f)
    result = gate(base_ref="HEAD", paths=(abs_path,), root=tmp_path)
    # 归一化生效 → 真的比上了 → 该模板被判为"有改动"，且回归被抓到
    # （而不是变成"新增模板、判定为空、静默通过"）
    assert result["added_templates"] == [], "绝对路径没被归一化 ⇒ 模板被误判成新增"
    assert [c["name"] for c in result["changed"]] == ["REVIEWER_SYSTEM"]
    assert result["regressed"] is True


def test_missing_base_file_is_disclosed_not_hidden(tmp_path):
    """基线里找不到该文件时，必须显式说明"未做任何判定" ——
    与"比对后没发现问题"在输出里要分得开（两者都会让 regressed=False）。
    """
    f = tmp_path / "prompts.py"
    init = _py_const(CLEAN_BODY, "OLD_SYS")
    f.write_text(init, encoding="utf-8")
    _init_repo(tmp_path)
    # 换一个基线里不存在的路径（模拟 --path 拼错）
    (tmp_path / "other.py").write_text(CLEAN_T, encoding="utf-8")
    result = gate(base_ref="HEAD", paths=("other.py",), root=tmp_path)
    assert any("未做任何回归判定" in n for n in result["notes"]), result["notes"]


# --------------------------------------------------------------------------
# CI 接线护栏：配置漂了门禁就空转，而空转的门禁不会自己喊
# --------------------------------------------------------------------------
def _ci_yaml() -> dict:
    import yaml

    return yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))


def _step_runs(job: dict) -> list[str]:
    return [str(s.get("run", "")) for s in job["steps"]]


def test_both_ci_jobs_run_the_gate():
    """两条 CI 线都要跑门禁 —— 少一条就是"本地查、CI 不查"的第三种口径。

    这个仓库对那类漂移有专门的记录（`.pre-commit-config.yaml` 顶部与
    `test_ci_and_docs_run_every_release_suite`），这里沿用同一纪律。
    """
    jobs = _ci_yaml()["jobs"]
    for job_name, job in jobs.items():
        runs = "\n".join(_step_runs(job))
        assert "run.py gate" in runs, f"{job_name} job 没有跑提示词门禁"


def test_ci_checkouts_are_deep_enough_for_a_baseline():
    """`fetch-depth: 0` 是门禁的前提：浅克隆里没有基线的历史对象。

    缺了它，门禁会在 CI 上报"自己没跑起来"（退出码 2）—— 那是配置问题不是提示词问题，
    但同样会让这一步红，等于门禁没用。
    """
    for job_name, job in _ci_yaml()["jobs"].items():
        checkouts = [
            s for s in job["steps"] if str(s.get("uses", "")).startswith("actions/checkout")
        ]
        assert checkouts, f"{job_name} job 里没有 checkout"
        for c in checkouts:
            assert c.get("with", {}).get("fetch-depth") == 0, (
                f"{job_name} job 的 checkout 没有 fetch-depth: 0，门禁取不到基线"
            )


def test_ci_gate_step_declares_its_exit_code_contract():
    """门禁那一步必须把退出码语义写进注释：否则下一个看到红叉的人
    分不清"提示词有回归"和"门禁自己没跑起来"。"""
    jobs = _ci_yaml()["jobs"]
    gate_steps = [
        s for job in jobs.values() for s in job["steps"] if "run.py gate" in str(s.get("run", ""))
    ]
    assert gate_steps
    # linux job 那一步带了完整说明（windows 侧是简版，指向上面）
    joined = "\n".join(str(s.get("name", "")) + str(s.get("run", "")) for s in gate_steps)
    assert gate_steps[0].get("name")
    assert any("门禁" in str(s.get("name", "")) for s in gate_steps), joined


def test_ci_gate_handles_first_push_without_claiming_pass():
    """首次推送（before 是全零 SHA）不能报红，也不能声称"检查通过"。"""
    job = _ci_yaml()["jobs"]["test"]
    gate_run = next(s["run"] for s in job["steps"] if "run.py gate" in str(s.get("run", "")))
    assert "0000000000000000000000000000000000000000" in gate_run
    assert "pull_request" in gate_run  # PR 场景用 base.sha


def test_ci_never_runs_the_gate_bare_on_the_first_push_branch():
    """**实测缺陷**（2026-10-02）：首次推送那一支最初写的是裸 `python run.py gate`。

    而裸跑会走自动探测，自动探测**拒绝把 HEAD 自己当基线**（那是防"静默通过"的护栏）。
    在只有一个提交的检出里（`actions/checkout` 出来的 PR 合并态就可能是这样），
    自动探测无候选可用 → 门禁报"自己没跑起来"（退出码 2）→ **CI 在首次推送时红**。
    正确写法是显式把全零 SHA 传给 --base。

    这条钉住"那一支必须带 --base"，改回裸跑就会红。
    """

    for job_name, job in _ci_yaml()["jobs"].items():
        for st in job["steps"]:
            run = str(st.get("run", ""))
            if "run.py gate" not in run:
                continue
            # 每个 else 分支里的 gate 调用都必须带 --base
            for line in run.splitlines():
                line = line.strip()
                if "run.py gate" in line and not line.startswith("#"):
                    assert "--base" in line, (
                        f"{job_name} job 里出现了不带 --base 的裸 gate 调用：{line!r}"
                    )


def test_both_ci_jobs_use_the_same_gate_branch_logic():
    """两条 CI 线的门禁分支必须同口径；写法漂了会出现"一条红一条绿"的假象。"""
    jobs = _ci_yaml()["jobs"]
    runs = {}
    for job_name, job in jobs.items():
        for st in job["steps"]:
            if "run.py gate" in str(st.get("run", "")):
                runs[job_name] = str(st["run"])
    assert len(runs) == len(jobs), f"有的 job 没跑门禁：{set(jobs) - set(runs)}"
    for job_name, run in runs.items():
        assert "pull_request" in run, f"{job_name} 的 gate 步骤没处理 PR 场景"
        assert "0000000000000000000000000000000000000000" in run, (
            f"{job_name} 的 gate 步骤没处理首次推送（全零 SHA）"
        )


# --------------------------------------------------------------------------
# `gate` 子命令的 CLI 输出分支（力度在"门禁自己坏了"这一支：它必须与"有回归"分得开）
# --------------------------------------------------------------------------
def test_gate_command_reports_gate_error_in_json(tmp_path, capsys, monkeypatch):
    """门禁自己没跑起来时，**JSON 出口**也必须给出可区分的结构。

    ⚠️ CI 消费的是 `--json`：只在人读分支里写"这是门禁自己坏了"，
    CI 拿到的就只有一个 `ok: false` —— 与"提示词有回归"长得一模一样，
    而两者的下一步动作完全不同（一个去修 CI 配置，一个去改提示词）。
    """
    f = tmp_path / "prompts.py"
    f.write_text(CLEAN_T, encoding="utf-8")
    _init_repo(tmp_path)
    # `gate_command` 用 `Path.cwd()` 作仓库根（对 CI 是正确语义：工作流就在检出根跑），
    # 所以测试必须真的进到临时仓库里 —— 否则它查的是本仓库的 pm/prompts.py。
    monkeypatch.chdir(tmp_path)
    code = gate_command(["--base", "__不存在__", "--path", "prompts.py", "--json"])
    assert code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["gate_error"], "JSON 出口没带上门禁自身的报错"
    assert payload["hint"], "JSON 出口没有告诉调用方这是哪一类失败"


def test_gate_command_reports_gate_error_in_text(tmp_path, capsys, monkeypatch):
    """人读分支同样要说清"坏的是门禁不是提示词"。"""
    f = tmp_path / "prompts.py"
    f.write_text(CLEAN_T, encoding="utf-8")
    _init_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    code = gate_command(["--base", "__不存在__", "--path", "prompts.py"])
    out = capsys.readouterr().out
    assert code == 2
    assert "门禁未能执行" in out
    assert "不是「提示词有回归」" in out


def test_gate_command_passes_when_no_regression(tmp_path, capsys, monkeypatch):
    """无回归 → rc=0（覆盖 `EXIT_UNDELIVERED if regressed else PASSED` 的 else 支）。"""
    f = tmp_path / "prompts.py"
    f.write_text(CLEAN_T, encoding="utf-8")
    _init_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert gate_command(["--base", "HEAD", "--path", "prompts.py", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["regressed"] is False


def test_gate_command_custom_path_flag_is_honored(tmp_path, capsys, monkeypatch):
    """`--path` 是可重复参数：传了就按传的查，不能仍去查默认对象。"""
    f = tmp_path / "other.py"
    f.write_text(CLEAN_T, encoding="utf-8")
    _init_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    code = gate_command(["--base", "HEAD", "--path", "other.py", "--json"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["paths"] == ["other.py"]


def test_gate_command_text_output_lists_what_it_checked(tmp_path, capsys, monkeypatch):
    """人读出口要能回答"你查了什么、比的是谁"。"""
    f = tmp_path / "prompts.py"
    f.write_text(CLEAN_T, encoding="utf-8")
    _init_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    gate_command(["--base", "HEAD", "--path", "prompts.py"])
    out = capsys.readouterr().out
    assert "基线" in out and "检查对象" in out and "未改动模板" in out


# --------------------------------------------------------------------------
# `render_text` 的回归分支：出问题的时候，输出才是人唯一能看的东西
# --------------------------------------------------------------------------
def test_render_text_reports_introduced_rules_with_evidence():
    """有新引入时，人读出口要给出**模板名 + 规则 code + 字符变化**。

    只写一句"检测到回归"没有可操作性：改哪个文件、哪条规则、
    改动多大，都得能当场看到（CI 日志里没有第二次机会）。
    """
    result = gate(base_ref="HEAD~1", root=ROOT)
    assert result["regressed"] is False  # 先确认本仓库当前是干净的
    # 构造一个必然回归的 result（不依赖真实 git：render_text 是纯函数）
    fake = {
        **result,
        "regressed": True,
        "ok": False,
        "skipped": False,
        "changed": [
            {
                "name": "OPTIMIZER_SYSTEM",
                "path": "pm/prompts.py",
                "introduced": ["suppressive_rule"],
                "fixed": [],
                "still": [],
                "constraint_delta": 0,
                "chars_delta": 19,
                "added_hits": {},
            }
        ],
        "introduced_total": 1,
        "added_hits_total": 0,
    }
    out = render_text(fake)
    assert "❌" in out and "新引入" in out
    assert "OPTIMIZER_SYSTEM" in out and "suppressive_rule" in out
    assert "+19" in out, "没给出字符变化，读者无法判断改动规模"
    assert "已引入" not in out  # 别把"新增"写成别的措辞


def test_render_text_reports_added_hits_with_specific_clauses():
    """ "已命中规则上加重"要列出**具体新增的那一条**，不能只给计数。"""
    result = gate(base_ref="HEAD~1", root=ROOT)
    fake = {
        **result,
        "regressed": True,
        "ok": False,
        "skipped": False,
        "changed": [
            {
                "name": "REVISER_SYSTEM",
                "path": "pm/prompts.py",
                "introduced": [],
                "fixed": [],
                "still": ["suppressive_rule"],
                "constraint_delta": 0,
                "chars_delta": 12,
                "added_hits": {"suppressive_rule": ["停止处理"]},
            }
        ],
        "introduced_total": 0,
        "added_hits_total": 1,
    }
    out = render_text(fake)
    assert "加重" in out
    assert "REVISER_SYSTEM" in out and "停止处理" in out


def test_render_text_lists_fixed_problems_without_claiming_improvement():
    """修好的问题要列出来，但必须标注"不计入判定" —— 门禁不回答"够不够好"。"""
    result = gate(base_ref="HEAD~1", root=ROOT)
    fake = {
        **result,
        "regressed": False,
        "ok": True,
        "skipped": False,
        "changed": [
            {
                "name": "OPTIMIZER_SYSTEM",
                "path": "pm/prompts.py",
                "introduced": [],
                "fixed": ["suppressive_rule"],
                "still": [],
                "constraint_delta": -2,
                "chars_delta": -30,
                "added_hits": {},
            }
        ],
        "introduced_total": 0,
        "added_hits_total": 0,
    }
    out = render_text(fake)
    assert "不计入判定" in out
    assert "OPTIMIZER_SYSTEM" in out and "suppressive_rule" in out
    assert "变好" not in out


def test_render_text_lists_changed_templates_even_when_clean():
    """没回归时也要列出"本次有文本改动的模板" —— 否则读者不知道门禁到底比了什么。"""
    result = gate(base_ref="HEAD~1", root=ROOT)
    fake = {
        **result,
        "regressed": False,
        "ok": True,
        "skipped": False,
        "changed": [
            {
                "name": "COMPARATOR_SYSTEM",
                "path": "pm/prompts.py",
                "introduced": [],
                "fixed": [],
                "still": [],
                "constraint_delta": 0,
                "chars_delta": 5,
                "added_hits": {},
            }
        ],
    }
    out = render_text(fake)
    assert "本次有文本改动的模板" in out
    assert "COMPARATOR_SYSTEM" in out


def test_render_text_surfaces_names_of_new_and_removed_templates(tmp_path):
    """新增/删除模板必须**点名**，不能只给个数。

    用**真实 gate() 结果**而不是手搓 notes：手搓的话，我写什么它就渲染什么，
    测的只是自己的夹具（第一版就这么写的 —— 我把 notes 覆盖成不含名字的文本，
    于是"测试失败"其实是夹具不真实，而不是产品有问题）。
    这里让 gate() 自己生成 notes，只断言"名字在该出现的地方出现了"。
    """
    repo = tmp_path
    (repo / "prompts.py").write_text(CLEAN_T + _py_const("旧模板正文", "OLD_SYS"), encoding="utf-8")
    _init_repo(repo)
    (repo / "prompts.py").write_text(CLEAN_T + _py_const("新模板正文", "NEW_SYS"), encoding="utf-8")
    result = gate(base_ref="HEAD", paths=("prompts.py",), root=repo)
    out = render_text(result)
    joined_notes = "\n".join(result["notes"])
    assert "NEW_SYS" in joined_notes, "新增模板没被点名"
    assert "OLD_SYS" in joined_notes, "删除的模板没被点名"
    assert "NEW_SYS" in out and "OLD_SYS" in out, "点名的信息没有渲染到人读输出里"
    assert "没有基线可比" in out
