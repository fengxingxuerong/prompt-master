"""用例集零调用体检（`pm/casesaudit.py` + `run.py check --cases-file`）的守卫。

为什么这批断言值得写：产品的"区分度"告警原本只在**烧完之后**才出现
（`pm/report.py`：基线均分 ≥8.0 才说"用例区分度不足"），而实测两轮都是这样
（Δ+0.0 与 Δ−0.35 那两份报告）。把账提前到零调用之后，判据本身必须被钉住 ——
尤其是"体检说的"与"执行时真正发生的"不许各说一套。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from pm.assertions import check_assertion
from pm.casesaudit import (
    DEFAULT_MODE,
    INJECTION_MIN_CASES,
    RULE_MODE,
    audit_cases,
    echoes_input,
    has_assertion,
    is_advisory,
    missing_scenarios,
    mode_of,
)
from pm.cli.support import MAX_SEED_CASES

ROOT = Path(__file__).resolve().parents[1]
REPO = str(ROOT)


def _codes(report: dict) -> set[str]:
    return {f["code"] for f in report["findings"]}


def test_check_command_cases_path_in_process(tmp_path: Path, capsys) -> None:
    """同一入口的两条分支都要在**进程内**被走到：子进程那批用例不计进覆盖率分子
    （CI 的 `--cov` 只量主进程），只在子进程里测等于新增分支零覆盖 —— 覆盖率掉了 1.3 个点
    就是这一条没做时量到的。"""
    from pm.cli.check import check_command

    path = tmp_path / "set.json"
    path.write_text(
        json.dumps(
            [
                {"input": "商品「K870」¥299，请列价格表", "expected": "K870", "mode": "contains"},
                {"input": "只给半年就要求同比", "expected": "口径", "mode": "contains"},
            ]
        ),
        encoding="utf-8",
    )

    assert check_command(["--cases-file", str(path), "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "cases_check" and payload["veto_capable"] == 2
    # 这份样本没标 scenario，所以覆盖缺口也会报出来 —— 期望里带着它，
    # 免得下一个人把 scenario_gap 当成误报删掉
    assert {f["code"] for f in payload["findings"]} == {"echoes", "scenario_gap"}

    assert check_command(["--cases-file", str(path)]) == 1
    text = capsys.readouterr().out
    assert "能一票否决的：2 条" in text and "强判据落在第 2 条" in text

    with pytest.raises(SystemExit):
        check_command(["--cases-file", str(path), "--prompt", "一句提示词"])


def test_the_default_mode_is_the_same_for_both_paths() -> None:
    """跑批与体检对"没写 mode 的用例"必须用同一个缺省，否则同一份文件两边各说一套。"""
    from pm.cli.main import _build_parser

    run_parser = _build_parser("run.py")
    matched = [a for a in run_parser._actions if getattr(a, "dest", "") == "assert_mode"]
    assert matched, "run.py 的 --assert-mode 不见了：这条差分判据的素材变了，先修判据本身"
    default = matched[0].default
    assert default == DEFAULT_MODE, f"跑批的缺省是 {default!r}，体检用的是 {DEFAULT_MODE!r}"


def test_the_cap_comes_from_one_constant_not_a_second_copy() -> None:
    """条数上限只许有一份。

    为什么这条是**源码级**断言而不是行为断言：`MAX_SEED_CASES=8` 与在 check.py 里
    写死 `max_cases=8` 在任何黑盒输入下都给同一个答案 —— 实测植入这个变异后
    本文件全绿（我把变异跑过一遍，见提交说明）。数值相等时行为测不出来，
    只能按 `tests/test_version_single_source.py` 的先例钉字面量。
    """
    import re

    check_src = (ROOT / "pm" / "cli" / "check.py").read_text(encoding="utf-8")
    main_src = (ROOT / "pm" / "cli" / "main.py").read_text(encoding="utf-8")
    assert "max_cases=MAX_SEED_CASES" in check_src, "体检没读共享常量"
    assert re.search(r"max_cases=\d", check_src) is None, "check.py 里把上限抄成了字面量"
    assert ">= MAX_SEED_CASES" in main_src, "跑批没读共享常量"
    assert re.search(r"seed_cases\) >= \d", main_src) is None, "main.py 里把上限抄成了字面量"
    # 缺省 mode 同理：两条路径必须共用一个来源，否则"没写 mode 的用例"在体检里是 contains、
    # 在跑批里可能是别的 —— 体检与跑批就会对同一份文件各说一套
    assert "default=DEFAULT_MODE" in check_src and "default=DEFAULT_MODE" in main_src
    assert 'default="contains"' not in check_src and 'default="contains"' not in main_src


def test_bucket_partition_is_exhaustive() -> None:
    """四桶必须不重不漏地覆盖全部用例 —— 这是这份体检唯一的"数得对"证明。"""
    cases = [
        {"input": "a", "expected": "结果 A", "mode": "contains"},
        {"input": "b", "expected": "必须给出结论并标注缺失", "mode": "contains"},  # advisory
        {"input": "c", "expected": "", "mode": "rule"},  # judge only
        {"input": "d", "expected": "", "mode": "contains"},  # no assertion
        {"input": "e", "expected": "d", "mode": "custom:nonexistent"},  # deterministic
    ]
    s = audit_cases(cases, max_cases=MAX_SEED_CASES)["stats"]
    assert s["veto_capable"] + s["advisory"] + s["judge_only"] + s["no_assertion"] == 5, s
    assert (s["veto_capable"], s["advisory"], s["judge_only"], s["no_assertion"]) == (2, 1, 1, 1), s


def test_assert_mode_key_is_honoured_like_the_executor() -> None:
    """用例 JSON 允许用 `assert_mode` 而不是 `mode`（`support.case_entry_fields` 就认它）。

    2026-10-31 修：`mode_of` 只读 `mode`，于是同一份 JSON 换个键名就变成两套结论 ——
    `{"assert_mode":"custom:x","expected":""}` 被报"白占一个用例位"（运行时其实执行），
    `{"assert_mode":"rule"}` 被算成 contains（judge_only 桶全数错）。
    """
    custom = {"input": "a", "expected": "", "assert_mode": "custom:no_such"}
    judge_only = {"input": "b", "expected": "", "assert_mode": RULE_MODE}
    cases = [custom, judge_only]
    s = audit_cases(cases, max_cases=MAX_SEED_CASES)["stats"]
    assert mode_of(custom) == "custom:no_such"
    assert mode_of(judge_only) == RULE_MODE
    assert s["judge_only"] == 1, s
    assert s["no_assertion"] == 0, "custom 模式被误报成没有断言"


def test_assertion_eligibility_matches_the_executor_itself() -> None:
    """差分判据：`has_assertion()` 与真正的执行器 `check_assertion()` 对"这条算不算数"
    必须给同一个答案。只测前者等于让体检与跑批各说一套（本仓库在这类分叉上吃过多次亏）。
    """
    probes = [
        ("contains", "华东"),
        ("contains", ""),
        ("exact", "X"),
        ("regex", "^X$"),
        (RULE_MODE, "任何"),
        (RULE_MODE, ""),
        ("custom:no_such_assertion", ""),
    ]
    for mode, expected in probes:
        case = {"input": "华东 120 万", "expected": expected, "mode": mode}
        executed = check_assertion(expected, "随便一份输出", mode) is not None
        assert has_assertion(case) is executed, (mode, expected, executed)


def test_echo_only_assertion_is_flagged_and_downgraded() -> None:
    """「expected 就在 input 里」是内容保留性断言：它测不出任务质量 ⇒ 不许计入强判据。"""
    echo = {"input": "商品「机械键盘 K870」价格 ¥299", "expected": "机械键盘", "mode": "contains"}
    strong = {"input": "商品「机械键盘 K870」价格 ¥299", "expected": "合计", "mode": "contains"}
    assert echoes_input(echo) is True and echoes_input(strong) is False
    report = audit_cases([echo, strong], max_cases=MAX_SEED_CASES)
    assert "echoes" in _codes(report)
    s = report["stats"]
    assert (s["veto_capable"], s["veto_strong"], s["echo_only"]) == (2, 1, 1), s
    assert s["veto_strong_cases"] == [2], "强判据要点名到具体第几条，否则读者无从动手改"


def test_rule_and_regex_are_not_claimed_as_echo() -> None:
    """regex/custom 的字面比对语义静态判不出来 ⇒ 不许顺手打个"echoes"标签冒充知道。"""
    for mode in ("regex", RULE_MODE, "custom:x"):
        case = {"input": "abc", "expected": "abc", "mode": mode}
        assert echoes_input(case) is False, mode


def test_advisory_uses_the_same_predicate_the_executor_uses() -> None:
    """降级判据与 `check_assertion` 内部同一个 `looks_like_rule`，不许体检自造一套。"""
    rule_like = {
        "input": "x",
        "expected": "必须先列出缺失项并显式标注，不得编造",
        "mode": "contains",
    }
    assert is_advisory(rule_like) is True
    report = audit_cases([rule_like], max_cases=MAX_SEED_CASES)
    assert "rule_like" in _codes(report)
    assert report["stats"]["veto_capable"] == 0, "被降级的断言不许再算进否决票"


def test_duplicate_cases_are_named_with_their_twin() -> None:
    a = {"input": "请分析  Q1  数据", "expected": "结论", "mode": "contains"}
    b = {"input": "请分析 Q1 数据", "expected": "结论", "mode": "contains"}
    report = audit_cases([a, b], max_cases=MAX_SEED_CASES)
    dup = [f for f in report["findings"] if f["code"] == "duplicate"]
    assert len(dup) == 1 and dup[0]["case"] == 2, dup
    assert "第 1 条" in dup[0]["detail"], "要点名跟谁重复，'有重复'这种话没法行动"


def test_injection_gap_only_demanded_at_three_or_more_cases() -> None:
    """n<3 不强要注入格 —— 阈值与 execute.py 的覆盖校验同源（同一份常量，不是抄的）。"""
    two = ["main_path", "boundary"]
    three = [*two, "boundary"]
    assert missing_scenarios(two, 2) == []
    assert missing_scenarios(three, 3) and missing_scenarios(three, 3)[0].startswith("injection")
    assert INJECTION_MIN_CASES == 3


def test_scenario_vocabulary_is_case_and_space_tolerant() -> None:
    assert missing_scenarios([" Main_Path ", "BOUNDARY", "Injection"], 3) == []


def test_over_cap_is_reported_not_truncated() -> None:
    cases = [{"input": f"x{i}", "expected": "y", "mode": "contains"} for i in range(9)]
    report = audit_cases(cases, max_cases=MAX_SEED_CASES)
    assert "over_cap" in _codes(report)
    assert report["stats"]["cases"] == 9, "体检要如实数出 9 条，不能像跑批那样静默截到 8"


@pytest.mark.parametrize(
    "name",
    sorted(p.name for p in (ROOT / "case_templates").glob("*.json")),
)
def test_the_audit_runs_on_every_shipped_template(name: str) -> None:
    """真素材上跑通：分区和等于条数、JSON 恰好一个对象。
    ⚠️ 不钉"零命中"—— 实测这 5 份模板各有 2/4 条是内容保留性断言（其中两份强判据为 0 条），
    那正是"基线也能拿 8.25"的结构成因，钉成绿等于把发现抹掉。
    """
    out = subprocess.run(
        [sys.executable, "run.py", "check", "--cases-file", f"case_templates/{name}", "--json"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=REPO,
        timeout=120,
        check=False,
    )
    assert out.returncode in (0, 1), out.stderr[-400:]
    d = json.loads(out.stdout)  # 多一个字符就炸在这里
    assert d["mode"] == "cases_check"
    assert d["veto_capable"] + d["advisory"] + d["judge_only"] + d["no_assertion"] == d["cases"]
    assert d["max_cases"] == MAX_SEED_CASES, "体检报的上限必须是同一个常量，不是又抄了一个 8"


def test_cli_exit_codes_and_argument_conflicts(tmp_path: Path) -> None:
    def run(*args: str) -> tuple[int, str]:
        p = subprocess.run(
            [sys.executable, "run.py", "check", *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=REPO,
            timeout=120,
            check=False,
        )
        return p.returncode, p.stdout + p.stderr

    def write(name: str, payload: object) -> str:
        path = tmp_path / name
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return str(path)

    good = write(
        "good.json",
        [
            {
                "input": "问：1+1 等于几？",
                "expected": "2",
                "mode": "contains",
                "scenario": "main_path",
            },
            {
                "input": "只给了半年数据就要求同比",
                "expected": "口径",
                "mode": "contains",
                "scenario": "boundary",
            },
        ],
    )
    rc, out = run("--cases-file", good, "--json")
    assert rc == 0, out[-400:]
    assert json.loads(out)["findings"] == [], "标齐场景、断言是输出侧字面片段的考卷不该被判命中"

    bad_echo = write(
        "echo.json",
        [
            {"input": "商品「K870」¥299，请列价格表", "expected": "K870", "mode": "contains"},
            {"input": "商品「K870」¥299，请列价格表", "expected": "M3", "mode": "contains"},
        ],
    )
    rc, out = run("--cases-file", bad_echo, "--json")
    assert rc == 1, out[-300:]
    codes = {f["code"] for f in json.loads(out)["findings"]}
    assert {"echoes", "duplicate"} <= codes, codes

    # 参数与格式类一律走"配置错误"（2），别与"有命中"（1）混成一种红
    assert run("--cases-file", str(tmp_path / "不存在.json"), "--json")[0] == 2
    assert run("--cases-file", write("obj.json", {"not": "an array"}))[0] == 2
    assert run("--cases-file", write("mode.json", [{"input": "a", "mode": "fuzzy"}]))[0] == 2
    rc, out = run("--cases-file", good, "--prompt", "随便一句")
    assert rc == 2, out[-300:]
    assert "互斥" in out, "退出码相同而原因不同，Agent 就没法分支"
    # 非法的 --assert-mode 在两条路径上都必须是 2（配置错误），不许一个 2 一个 1
    assert run("--cases-file", good, "--assert-mode", "fuzzy")[0] == 2
