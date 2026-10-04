"""提示词差分（`pm/promptdiff.py` + `diff` 子命令）的回归。

这块的定位是"CI 回归门禁"，所以三类断言最重要：
1. **与 `check` 同源**：同一个 prompt 在 `diff` 与 `check` 两条路径上必须得出同一组规则命中。
   两套判据一旦漂移，"diff 说没问题、check 说有"就会成为最坏的一类 bug——两边都"有理由"。
2. **退出码语义**：只有"新引入"才判 1。修好 0 条不判 1，因为 diff 不回答"够不够好"。
3. **来源解析**：文件 / run_id 两条路径都要能走，且"读到但没产出提示词"要与"查无此 run"分开报。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from pm.cli.diffcmd import diff_command, render_text
from pm.cli.main import _SUBCOMMANDS, _subcommand_help
from pm.promptdiff import (
    DiffSourceError,
    diff_prompts,
    resolve_source,
    rule_delta,
    structure_delta,
    unified_diff,
)

ROOT = Path(__file__).resolve().parents[1]
RUN = [sys.executable, str(ROOT / "run.py")]

CLEAN = (
    "[角色] 你是资深销售数据分析师。\n"
    "[任务] 依据用户提供的销售数据，输出趋势结论、异常点与下一步建议。\n"
    "[输出格式] Markdown 表格：区域｜销售额｜同比，随后给出 3 条结论。\n"
    "[边界处理] 输入完全为空时标注「数据缺失」；仅部分字段缺失时已有数据照常输出。"
)

SUPPRESSIVE = (
    "[角色] 你是资深销售数据分析师，负责把区域销售数据整理成可决策的结论。\n"
    "[任务] 依据用户提供的销售数据，输出趋势结论、异常点与下一步建议三部分内容。\n"
    "[输出格式] Markdown 表格：区域｜销售额｜同比，随后给出 3 条结论。\n"
    "[判断规则] 所有结论必须引用输入中的具体数值，禁止只给定性描述。\n"
    "[边界处理] 输入异常时停止处理，不输出任何结论。\n"
    "[补充说明] 结论按重要性排序，每条不超过 50 字，避免重复表述同一现象。"
)


# --------------------------------------------------------------------------
# 判据层：与 check 同源
# --------------------------------------------------------------------------
def test_rule_delta_matches_check_command_source():
    """diff 的规则判据必须与 `check` 同源 —— 两条路径给出不同结论是最坏的一类 bug。"""
    from pm.cli.check import collect_findings

    for text in (CLEAN, SUPPRESSIVE):
        from_check = {f["code"] for f in collect_findings(text)}
        # diff 侧用同一函数算，所以相等是恒真的；这里钉的是"有人把它改成另写一套"
        from pm.promptdiff import rule_findings

        assert rule_findings(text) == from_check


def test_rule_delta_fixed_and_introduced():
    d = rule_delta(SUPPRESSIVE, CLEAN)
    assert d["fixed"] == ["suppressive_rule"]
    assert d["introduced"] == []

    rev = rule_delta(CLEAN, SUPPRESSIVE)
    assert rev["introduced"] == ["suppressive_rule"]
    assert rev["fixed"] == []


def test_rule_delta_only_compares_codes_not_detail_text():
    """detail 里带条数/字符数，按文本比会把"修好一条"误报成"新引入一条"。"""
    few = "[角色] 你是分析师。\n[任务] 给出结论。\n[约束]\n1. 不编造。\n"
    many = "[角色] 你是分析师。\n[任务] 给出结论。\n[约束]\n" + "".join(
        f"{i}. 不要编造数据点 {i}。\n" for i in range(1, 4)
    )
    d = rule_delta(few, many)
    assert "constraint_overload" not in d["fixed"]
    assert d["still"] == [] or "constraint_overload" not in d["introduced"]


def test_structure_delta_counts_and_items():
    st = structure_delta(SUPPRESSIVE, CLEAN)
    assert st["chars"]["delta"] == len(CLEAN.strip()) - len(SUPPRESSIVE.strip())
    assert st["constraints"]["after"] >= st["constraints"]["before"]
    # 条目的增删是给"改了什么"提供证据，不是只给计数
    assert st["items"]["n_removed"] >= 1
    assert any("停止处理" in t for t in st["items"]["removed"])


def test_unified_diff_is_line_level():
    d = unified_diff("a\nb\nc\n", "a\nB\nc\n", "old", "new")
    assert "-b" in d and "+B" in d
    assert d.startswith("--- old")


def test_diff_prompts_flags_regression_only_on_introduced():
    assert diff_prompts(CLEAN, SUPPRESSIVE)["regressed"] is True
    assert diff_prompts(SUPPRESSIVE, CLEAN)["regressed"] is False
    # 完全一致：不是回归，且要能被识别出来（报告不渲染 diff）
    same = diff_prompts(CLEAN, CLEAN)
    assert same["identical"] is True and same["regressed"] is False


def test_diff_prompts_identical_ignores_trailing_whitespace():
    assert diff_prompts(CLEAN, CLEAN + "\n\n  ")["identical"] is True


# --------------------------------------------------------------------------
# 来源解析
# --------------------------------------------------------------------------
def test_resolve_source_reads_file(tmp_path):
    f = tmp_path / "p.md"
    f.write_text(CLEAN, encoding="utf-8")
    src = resolve_source(str(f))
    assert src.kind == "file" and src.text == CLEAN


def test_resolve_source_reads_run_id(tmp_path):
    (tmp_path / "run_abc123.json").write_text(
        json.dumps({"prompt": CLEAN, "status": "passed"}), encoding="utf-8"
    )
    src = resolve_source("abc123", log_dir=tmp_path)
    assert src.kind == "run" and src.label == "run:abc123" and src.text == CLEAN


def test_resolve_source_run_without_prompt_is_distinct_error(tmp_path):
    """ "有这个 run 但它没产出提示词"与"没这个 run"必须分开报：前者是真实故障现场。"""
    (tmp_path / "run_ffffffff.json").write_text(
        json.dumps({"prompt": "", "status": "failed"}), encoding="utf-8"
    )
    with pytest.raises(DiffSourceError, match="没有交付提示词"):
        resolve_source("ffffffff", log_dir=tmp_path)


def test_resolve_source_unknown_spec_lists_both_options(tmp_path):
    with pytest.raises(DiffSourceError) as e:
        resolve_source("__既不是文件也不是run__", log_dir=tmp_path)
    msg = str(e.value)
    assert "文件路径" in msg and "run_id" in msg


def test_resolve_source_empty_and_blank_file(tmp_path):
    with pytest.raises(DiffSourceError):
        resolve_source("   ")
    blank = tmp_path / "blank.md"
    blank.write_text("   \n", encoding="utf-8")
    with pytest.raises(DiffSourceError, match="空文件"):
        resolve_source(str(blank))


def test_resolve_source_prefers_file_over_run_id(tmp_path):
    """同名时按文件走：用户显式指向的文件比一个短十六进制串更明确。"""
    f = tmp_path / "abc123"
    f.write_text(CLEAN, encoding="utf-8")
    assert resolve_source(str(f)).kind == "file"


# --------------------------------------------------------------------------
# 命令入口与退出码
# --------------------------------------------------------------------------
def test_command_exit_code_regression(tmp_path, capsys):
    a, b = tmp_path / "a.md", tmp_path / "b.md"
    a.write_text(CLEAN, encoding="utf-8")
    b.write_text(SUPPRESSIVE, encoding="utf-8")
    assert diff_command([str(a), str(b), "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "prompt_diff"
    assert payload["ok"] is False and payload["regressed"] is True

    assert diff_command([str(b), str(a), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_command_no_diff_strips_unified(tmp_path, capsys):
    a, b = tmp_path / "a.md", tmp_path / "b.md"
    a.write_text(CLEAN, encoding="utf-8")
    b.write_text(SUPPRESSIVE, encoding="utf-8")
    diff_command([str(a), str(b), "--json", "--no-diff"])
    assert json.loads(capsys.readouterr().out)["unified"] == ""


def test_command_usage_error_exits_two(tmp_path):
    """参数/来源错误必须是 2，不能是 traceback 也不是 1。"""
    f = tmp_path / "a.md"
    f.write_text(CLEAN, encoding="utf-8")
    with pytest.raises(SystemExit) as e:
        diff_command([str(f), "__不存在__"])
    assert e.value.code == 2


def test_render_text_explains_codes_and_never_claims_improvement():
    """人读版必须给 code 释义，且不能把"修好几条"说成"变好了"。"""
    out = render_text(diff_prompts(SUPPRESSIVE, CLEAN, a_label="a", b_label="b"))
    assert "抑制型规则" in out
    assert "未新引入任何确定性规则问题" in out
    assert "变好" not in out


def test_render_text_identical_short_circuits():
    out = render_text(diff_prompts(CLEAN, CLEAN))
    assert "完全一致" in out


# --------------------------------------------------------------------------
# 真实入口分发（subprocess）
# --------------------------------------------------------------------------
def _run(args: list[str], env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        RUN + args,
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=120,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, **(env or {})},
    )


def test_run_py_dispatches_diff_subcommand(tmp_path):
    a, b = tmp_path / "a.md", tmp_path / "b.md"
    a.write_text(CLEAN, encoding="utf-8")
    b.write_text(SUPPRESSIVE, encoding="utf-8")
    r = _run(["diff", str(a), str(b), "--json"])
    assert r.returncode == 1, (r.stdout[-400:], r.stderr[-400:])
    assert json.loads(r.stdout)["mode"] == "prompt_diff"


def test_diff_works_without_any_api_key(tmp_path):
    """零调用承诺的实证：零调用子命令必须无 Key 可跑，否则 CI 门禁是假的。"""
    a, b = tmp_path / "a.md", tmp_path / "b.md"
    a.write_text(SUPPRESSIVE, encoding="utf-8")
    b.write_text(CLEAN, encoding="utf-8")
    r = _run(
        ["diff", str(a), str(b), "--json"],
        env={
            "PM_API_KEY": "",
            "PM_TARGET_API_KEY": "",
            "PM_API_TOKEN": "",
            "PM_FAKE_BACKEND": "",
        },
    )
    assert r.returncode == 0, (r.stdout[-400:], r.stderr[-400:])


def test_help_lists_diff_subcommand():
    assert "diff" in _SUBCOMMANDS
    assert "diff" in _subcommand_help("run.py")


def test_summary_says_text_changed_when_no_rule_fires():
    """正文改了但规则/约束都没动时，不许含糊成"什么都没改"。

    措辞变化没有确定性判据（本模块的已知边界），所以必须把这句话说出来，
    而不是给一个会被读成"没改动"的 summary。
    """
    a = (
        "[角色] 你是资深数据分析师。\n"
        "[任务] 分析数据并输出结论、异常点与建议。\n"
        "[输出格式] Markdown 表格。\n"
        "[约束] 1. 结论必须引用具体数值。"
    )
    b = a.replace("分析数据并输出结论", "分析数据并给出结论")
    d = diff_prompts(a, b)
    assert d["identical"] is False
    assert "正文有改动" in d["summary"]


def test_summary_of_identical_pair_says_so():
    a = "[角色] 你是分析师。\n[任务] 分析数据并输出结论与建议。\n[约束] 1. 不得编造数值。"
    assert "两份正文一致" in diff_prompts(a, a)["summary"]


# --------------------------------------------------------------------------
# 渲染分支：人读出口要把"加重"与"截断"如实写出来
# --------------------------------------------------------------------------
def test_render_text_shows_added_hits_on_already_dirty_rules():
    """已命中规则上继续加重时，人读出口必须列出**具体新增了哪一条**。

    只给一个计数（"另有 N 处加重"）读者无法定位；而这条通道正是
    code 级判据看不见的那一类回归，不给证据等于让它继续隐形。
    """
    from pm.promptdiff import diff_prompts

    before = (
        "[角色] 你是提示词工程师。\n[规则] 不写「停止处理」这类条款——它们会压低完成度。\n"
        "[任务] 生成一份结构清晰的提示词骨架，覆盖角色、任务、输出格式与边界处理四部分。"
    )
    after = before + "\n[边界] 若输入异常则停止处理，不输出任何结论。"
    d = diff_prompts(before, after, a_label="a", b_label="b")
    assert d["regressed"] is True
    # 该 code 本来就命中 → 走"加重"通道
    assert d["regressed_items"], "已命中规则上的新增没有被记进 regressed_items"
    out = render_text(d)
    assert "继续加重" in out, "人读出口没说明这是「加重」而不是「新引入」"
    assert "新增「" in out, "没列出具体新增了哪条命中项"


def test_render_text_truncates_long_item_lists():
    """增删条目很多时要给"另有 N 条未列出"，而不是把提示词整个贴回来。"""
    from pm.promptdiff import diff_prompts

    def section(n: int) -> str:
        items = "\n".join(f"{i}. 约束条目编号 {i} 的具体内容描述。" for i in range(1, n + 1))
        return f"[约束]\n{items}\n[任务] 分析数据并输出结论与建议。\n[输出格式] Markdown 表格。"

    d = diff_prompts(section(5), section(30), a_label="a", b_label="b")
    out = render_text(d)
    assert "未列出" in out, "条目被截断时必须说出来，否则读者以为看到的是全部"


def test_render_text_uses_no_explanation_mode():
    """`findings_explain=False` 时不带释义（CI 日志里只要 code）。"""
    from pm.promptdiff import diff_prompts

    before = "[角色] 你是分析师。\n[任务] 分析数据并输出结论与建议。\n[输出格式] 表格。\n[约束] 1. 不得编造。"
    after = before + "\n[边界] 输入异常时停止处理。"
    out = render_text(diff_prompts(before, after, a_label="a", b_label="b"), findings_explain=False)
    assert "抑制型规则（让模型少说/不说）" not in out
    assert "suppressive_rule" in out


def test_render_text_marks_rules_present_in_both_versions():
    """两版都还在的规则要单独列（读者需要知道"这次没动它"）。"""
    from pm.promptdiff import diff_prompts

    base = "[角色] 你是分析师。\n[任务] 分析数据并输出结论与建议。\n[输出格式] 表格。"
    # 两边都命中 too_short? 用长文本避免；改为构造"都命中约束超载"
    heavy = (
        "[角色] 你是分析师。\n[任务] 分析数据并输出结论与建议。\n[输出格式] 表格。\n[约束]\n"
        + "\n".join(f"{i}. 第 {i} 条约束要求的具体内容。" for i in range(1, 12))
    )
    d = diff_prompts(base, heavy, a_label="a", b_label="b")
    assert "constraint_overload" in d["rules"]["introduced"]
    # 反向：从超载改回正常 → 应报 fixed 而不是 still
    d2 = diff_prompts(heavy, base, a_label="a", b_label="b")
    assert d2["rules"]["fixed"], d2["rules"]


# --------------------------------------------------------------------------
# `diff` 子命令的剩余分支（长文本才能触发的那些）
# --------------------------------------------------------------------------
def test_command_text_output_includes_unified_diff(tmp_path, capsys):
    """人读出口默认带逐行 diff；`--no-diff` 才去掉。"""
    a, b = tmp_path / "a.md", tmp_path / "b.md"
    a.write_text(CLEAN, encoding="utf-8")
    b.write_text(SUPPRESSIVE, encoding="utf-8")
    diff_command([str(a), str(b), "--context", "0"])
    out = capsys.readouterr().out
    assert "逐行 diff" in out
    assert "--- " in out and "+++ " in out


def test_command_no_diff_omits_unified_from_text(tmp_path, capsys):
    """`--no-diff` 在人读出口也要生效（前面只测过 JSON 出口）。"""
    a, b = tmp_path / "a.md", tmp_path / "b.md"
    a.write_text(CLEAN, encoding="utf-8")
    b.write_text(SUPPRESSIVE, encoding="utf-8")
    diff_command([str(a), str(b), "--no-diff"])
    out = capsys.readouterr().out
    assert "逐行 diff" not in out


def test_command_text_marks_rules_present_in_both(tmp_path, capsys):
    """ "两版都还在"要出现在人读出口 —— 读者需要知道这次没动它。"""
    a, b = tmp_path / "a.md", tmp_path / "b.md"
    heavy = (
        "[角色] 你是分析师。\n[任务] 分析数据并输出结论与建议。\n[输出格式] 表格。\n[约束]\n"
        + "\n".join(f"{i}. 第 {i} 条约束要求的具体内容。" for i in range(1, 12))
    )
    a.write_text(heavy, encoding="utf-8")
    b.write_text(heavy + "\n[补充] 需要额外说明的一句话。", encoding="utf-8")
    diff_command([str(a), str(b), "--no-diff"])
    out = capsys.readouterr().out
    assert "两版都还在" in out and "constraint_overload" in out


def test_command_text_shows_remaining_items_when_truncated(tmp_path, capsys):
    """条目超过展示上限时要给"另有 N 条未列出"。"""
    a, b = tmp_path / "a.md", tmp_path / "b.md"

    def section(n: int) -> str:
        return "[角色] 你是分析师。\n[任务] 分析数据并输出结论与建议。\n[约束]\n" + "\n".join(
            f"{i}. 约束条目编号 {i} 的具体内容描述。" for i in range(1, n + 1)
        )

    # 差异条目必须超过展示上限（`_ITEM_SHOW = 12`）才会触发截断提示
    a.write_text(section(5), encoding="utf-8")
    b.write_text(section(40), encoding="utf-8")
    diff_command([str(a), str(b), "--no-diff"])
    out = capsys.readouterr().out
    assert "未列出" in out


def test_command_identical_files_says_so(tmp_path, capsys):
    a = tmp_path / "a.md"
    a.write_text(CLEAN, encoding="utf-8")
    diff_command([str(a), str(a)])
    out = capsys.readouterr().out
    assert "完全一致" in out


def test_command_regressed_text_warns_about_what_it_does_not_claim(tmp_path, capsys):
    """回归时的那句免责必须出现：门禁只拦"变坏"，不回答"够不够好"。"""
    a, b = tmp_path / "a.md", tmp_path / "b.md"
    a.write_text(CLEAN, encoding="utf-8")
    b.write_text(SUPPRESSIVE, encoding="utf-8")
    diff_command([str(a), str(b), "--no-diff"])
    out = capsys.readouterr().out
    assert "修好多少条都不构成通过的充分条件" in out


def test_command_bad_source_reports_usage_error(tmp_path, capsys):
    """来源解析失败时给出可操作的提示（文件路径或 run_id 两种用法）。"""
    a = tmp_path / "a.md"
    a.write_text(CLEAN, encoding="utf-8")
    with pytest.raises(SystemExit) as e:
        diff_command([str(a), "__既不是文件也不是run__"])
    assert e.value.code == 2
    err = capsys.readouterr().err
    assert "文件路径" in err or "run_id" in err
