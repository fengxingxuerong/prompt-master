"""三态裁决（`pm/verdict.py`）与三个零调用命令的 JSON 契约。

## 起因：一条实测出来的静默错误（2026-10-26）

`gate` 在"没有基线"（新分支首次推送）时的 JSON 是：

```json
{"skipped": true, "skip_reason": "...", "regressed": false, "ok": true}
```

**一个模板都没比，却报 `ok: true`。** 消费方只读 `ok`，就会把"没检查"读成"检查通过"，
而它是静默的：退出码 0、日志无异常。这与产品线"如实说清哪里还不行"直接相反。

修法不是简单地让 `ok` 变 `false`（那会让 `ok` 同时承担"有没有问题"与"有没有核验过"
两件事，换个场景又会混回去），而是把裁决拆成三态：

| 裁决 | 含义 | 硬约束 |
|---|---|---|
| `confirmed` | 真的核验过，且没发现问题 | 不许带 `unvalidated` |
| `needs_validation` | 没能核验 | **必须**带至少一条"确切未决事实"，且**不许给程度** |
| `rejected` | 核验过，且发现问题 | — |

`ok` 严格等于 `confirmed`；**退出码不变**（首次推送仍放行 —— 构建不该红，
但"没查"不许被读成"查过了"）。这条差异由 `test_ok_and_exit_code_are_deliberately_different` 钉住。

措辞与两条硬约束借用 cloudflare/security-audit-skill（它明写
`needs_validation has an exact unresolved fact and no severity`）：给了程度就会被人
当成"轻微问题"放行，不给事实则没人知道该补什么。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from pm.cli.check import check_command
from pm.cli.diffcmd import diff_command
from pm.cli.gatecmd import gate_command
from pm.verdict import (
    UNVALIDATED_KEYS,
    VERDICT_CONFIRMED,
    VERDICT_NEEDS_VALIDATION,
    VERDICT_REJECTED,
    VerdictError,
    attach,
    decide,
    unvalidated,
    validate,
)

ROOT = Path(__file__).resolve().parents[1]
RUN = [sys.executable, str(ROOT / "run.py")]

CLEAN_T = (
    "[角色] 你是资深销售数据分析师。\n"
    "[任务] 依据用户提供的销售数据，输出趋势结论、异常点与下一步建议。\n"
    "[输出格式] Markdown 表格：区域｜销售额｜同比，随后给出 3 条结论。\n"
    "[边界处理] 输入完全为空时标注「数据缺失」；仅部分字段缺失时已有数据照常输出。"
)


def _init_repo(repo: Path) -> None:
    import subprocess as sp

    (repo / "pm").mkdir(parents=True, exist_ok=True)
    # 无 BOM：`Set-Content`/`Out-File` 写 UTF-8 会带 BOM，而 ast.parse 会因为 U+FEFF 直接报错
    # （本轮探针真踩过：明明代码没问题，却看到"源码无法解析 U+FEFF"）。
    (repo / "pm" / "prompts.py").write_text('A_SYS = "你是分析师。"\n', encoding="utf-8")
    for args in (
        ("init", "-q", "."),
        ("config", "user.email", "ci@example.invalid"),
        ("config", "user.name", "CI"),
        ("add", "."),
        ("commit", "-q", "-m", "base"),
    ):
        sp.run(["git", *args], cwd=str(repo), capture_output=True, check=True)


# --------------------------------------------------------------------------
# 纯逻辑：三态裁决与载荷自检
# --------------------------------------------------------------------------
def test_decide_is_rejected_first_then_needs_validation_then_confirmed() -> None:
    """顺序敏感：真发现问题优先于"没核验成"。

    「既有新引入、又有一部分没比成」是真实组合（相对基线改了 3 个模板、另 2 个是新增的）。
    此时报 rejected 才对：已经确认有问题，补料与否都不改变"这次引入了问题"这个结论。
    """
    fact = [unvalidated("x", "某个东西没核验")]
    assert decide(rejected=True, unvalidated_facts=fact) == VERDICT_REJECTED
    assert decide(rejected=False, unvalidated_facts=fact) == VERDICT_NEEDS_VALIDATION
    assert decide(rejected=False, unvalidated_facts=[]) == VERDICT_CONFIRMED


def test_unvalidated_refuses_empty_code_or_fact() -> None:
    """空 code / 空 fact 都不许：前者消费方没法分支，后者没法补料。"""
    with pytest.raises(VerdictError):
        unvalidated("", "有事实")
    with pytest.raises(VerdictError):
        unvalidated("code", "   ")


def test_attach_forces_ok_to_equal_confirmed() -> None:
    """`attach` 会**覆盖**调用方原先写的 `ok` —— 这正是它存在的理由。"""
    payload = {"ok": True}  # 模拟"跳过"分支此前恒为 true 的那段代码
    with pytest.raises(VerdictError, match="必须带至少一条"):
        attach(payload, VERDICT_NEEDS_VALIDATION)


def test_attach_rejects_needs_validation_without_facts() -> None:
    with pytest.raises(VerdictError, match="必须带至少一条"):
        attach({"ok": True}, VERDICT_NEEDS_VALIDATION)


def test_attach_rejects_confirmed_with_facts() -> None:
    """有未决事实就不是 confirmed —— 这两件事不能同时成立。"""
    with pytest.raises(VerdictError, match="不许带 unvalidated"):
        attach({"unvalidated": [unvalidated("a", "b")]}, VERDICT_CONFIRMED)


def test_attach_overwrites_a_wrong_ok() -> None:
    payload = {"ok": True, "unvalidated": [unvalidated("no_baseline", "没基线")]}
    attach(payload, VERDICT_NEEDS_VALIDATION)
    assert payload["ok"] is False and payload["verdict"] == VERDICT_NEEDS_VALIDATION


def test_validate_flags_severity_keys_in_unvalidated() -> None:
    """`unvalidated` 只许 code/fact：给了 severity 就会被人按"轻微"放行。"""
    bad = {
        "verdict": VERDICT_NEEDS_VALIDATION,
        "ok": False,
        "unvalidated": [{"code": "x", "fact": "y", "severity": "low"}],
    }
    problems = validate(bad)
    assert any("不该有的键" in p for p in problems), problems
    assert all(k in UNVALIDATED_KEYS for k in ("code", "fact"))


def test_validate_flags_ok_verdict_mismatch() -> None:
    problems = validate({"verdict": VERDICT_NEEDS_VALIDATION, "ok": True, "unvalidated": []})
    assert any("不自洽" in p for p in problems)
    assert any("没给出任何 unvalidated" in p for p in problems)


def test_validate_accepts_a_wellformed_confirmed_payload() -> None:
    assert validate({"verdict": VERDICT_CONFIRMED, "ok": True, "unvalidated": []}) == []
    ok = {"verdict": VERDICT_REJECTED, "ok": False, "unvalidated": []}
    assert validate(ok) == []


# --------------------------------------------------------------------------
# 命令层：三个零调用命令都要给出裁决，且 ok 跟随裁决
# --------------------------------------------------------------------------
def test_gate_without_baseline_is_needs_validation_not_ok(tmp_path, capsys, monkeypatch) -> None:
    """本模块的**起因用例**：没有基线 ⇒ 一个模板都没比 ⇒ 不许说 ok。

    修前这里输出 `{"skipped": true, "regressed": false, "ok": true}`；
    修后同一条命令必须给 needs_validation + 确切未决事实。
    """
    _init_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    code = gate_command(["--base", "0" * 40, "--path", "pm/prompts.py", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0, "首次推送仍放行：构建不该红"
    assert payload["skipped"] is True
    assert payload["ok"] is False, "门禁一个模板都没比，却说自己 ok —— 正是这条要钉的"
    assert payload["verdict"] == VERDICT_NEEDS_VALIDATION
    assert [f["code"] for f in payload["unvalidated"]] == ["no_baseline"]
    assert validate(payload) == []


def test_gate_clean_run_is_confirmed(tmp_path, capsys, monkeypatch) -> None:
    _init_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    code = gate_command(["--base", "HEAD", "--path", "pm/prompts.py", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["verdict"] == VERDICT_CONFIRMED and payload["ok"] is True
    assert payload["unvalidated"] == []
    assert validate(payload) == []


def test_gate_error_is_needs_validation_too(tmp_path, capsys, monkeypatch) -> None:
    """门禁自己坏了同样是"没核验成"，不该退化成普通失败（消费方要靠 verdict 分支）。"""
    _init_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    code = gate_command(["--base", "__nope__", "--path", "pm/prompts.py", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 2
    assert payload["verdict"] == VERDICT_NEEDS_VALIDATION
    assert [f["code"] for f in payload["unvalidated"]] == ["gate_error"]
    assert validate(payload) == []


def test_gate_regression_is_rejected(tmp_path, capsys, monkeypatch) -> None:
    """回归判定的是**同名模板的正文变化**，不是"多了一个模板"。

    本轮夹具第一版直接往文件里**追加**一个 `B_SYS = "…停止处理…"`，结果拿到
    `needs_validation` 而不是 `rejected` —— 因为新增模板没有基线可比，`gate`
    （正确地）不把它算作回归。这条行为值得单独记一句：想造回归就得改**已有模板的正文**，
    否则测到的是另一条路径。
    """
    _init_repo(tmp_path)
    # 覆盖同名模板 A_SYS 的正文，并引入 suppressive_rule 的写法
    (tmp_path / "pm" / "prompts.py").write_text(
        'A_SYS = "输入异常时停止处理，不输出任何结论。"\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    code = gate_command(["--base", "HEAD", "--path", "pm/prompts.py", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 1
    assert payload["verdict"] == VERDICT_REJECTED and payload["ok"] is False
    assert payload["regressed"] is True
    assert validate(payload) == []


def test_check_clean_prompt_is_confirmed(tmp_path, capsys) -> None:
    f = tmp_path / "ok.md"
    f.write_text(CLEAN_T, encoding="utf-8")
    assert check_command(["--prompt-file", str(f), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["verdict"] == VERDICT_CONFIRMED and payload["ok"] is True
    assert validate(payload) == []


def test_check_hit_prompt_is_rejected(capsys) -> None:
    payload_text = "[任务] 你是一位严格的 AI 输出质量评估专家，请给下面的提示词打分。"
    assert check_command(["--prompt", payload_text, "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["verdict"] == VERDICT_REJECTED, "找到了问题就是核验过且有问题，不是'没核验成'"
    assert validate(payload) == []


def test_check_cases_without_strong_veto_is_needs_validation(tmp_path, capsys) -> None:
    """考卷"结构没问题"但一张强否决票都没有 ⇒ 它挡不住差输出，不许报 ok。

    `render_cases` 早就把这句话印给人看了，JSON 出口却只给 `ok: true`。这里钉住两者一致。

    夹具用**标准 scenario 名**（`main_path` / `boundary`）：`casesaudit` 会对照
    `pm/nodes/execute.py::_has_coverage` 的同一份判据查场景缺口，缺了就会多出
    `scenario_gap` 这条 finding，于是裁决变成 `rejected` —— 那是"考卷本身有结构问题"，
    与"考卷结构没问题、但挡不住差输出"不是同一件事。本轮夹具第一版就栽在这里
    （用中文 scenario，撞上 `scenario_gap`，测出来的是 rejected）。
    """
    f = tmp_path / "cases.json"
    f.write_text(
        json.dumps(
            [
                {"input": "甲", "expected": "甲里的字", "mode": "rule", "scenario": "main_path"},
                {"input": "乙", "expected": "乙外的字", "mode": "rule", "scenario": "boundary"},
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    code = check_command(["--cases-file", str(f), "--json"])
    payload = json.loads(capsys.readouterr().out)
    codes = [x["code"] for x in payload["unvalidated"]]
    assert payload["findings"] == [], f"夹具本该结构干净，却命中：{payload['findings']}"
    assert payload["ok"] is False
    assert payload["verdict"] == VERDICT_NEEDS_VALIDATION
    assert codes == ["no_strong_veto", "no_veto_capable"], codes
    assert code == 0, "考卷没结构问题，退出码仍是 0（判据与退出码不同步，见模块 docstring）"
    assert validate(payload) == []


def test_diff_always_carries_an_unvalidated_key(tmp_path, capsys) -> None:
    """三个命令的 JSON 同形：`unvalidated` 恒在，消费方不用写 `if 'unvalidated' in p`。"""
    a, b = tmp_path / "a.md", tmp_path / "b.md"
    a.write_text(CLEAN_T, encoding="utf-8")
    b.write_text(CLEAN_T, encoding="utf-8")
    assert diff_command([str(a), str(b), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert "unvalidated" in payload and payload["verdict"] == VERDICT_CONFIRMED
    assert validate(payload) == []


def test_ok_and_exit_code_are_deliberately_different(tmp_path, capsys, monkeypatch) -> None:
    """**这条是本模块最重要的一条**：`ok` 与退出码回答的不是同一个问题。

    - `ok`：这次核验结论成立吗（只有 confirmed 为 True）；
    - 退出码：要不要把构建弄红（首次推送默认放行，`--require-baseline` 才红）。

    故意不同步：若让"没基线"也退 1，那新分支永远红，门禁会被绕过（本仓对"永远红的门禁"
    有专门教训）；若让 `ok` 跟着退出码走，又回到本模块要修的那个静默错误。
    """
    _init_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    code = gate_command(["--base", "0" * 40, "--path", "pm/prompts.py", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0 and payload["ok"] is False, (
        "放行但绝不 ok —— 这两个信号必须不同步，否则'没查'又会被读成'查过了'"
    )

    strict = gate_command(
        ["--base", "0" * 40, "--path", "pm/prompts.py", "--json", "--require-baseline"]
    )
    assert strict == 2, "--require-baseline 才把'没有基线'算失败（严格门禁场景）"


# --------------------------------------------------------------------------
# `python -m pm.verdict`：让消费方不必相信我们的文档，直接调它
# --------------------------------------------------------------------------
def _run_verdict(payload: dict | str) -> subprocess.CompletedProcess[str]:
    """把载荷经 **stdin 管道** 喂给 `python -m pm.verdict -`。

    走管道而不是临时文件：一是免得在 Windows 上写出 BOM（本轮探针被这个坑过一次），
    二是它正好覆盖"中文经管道进 GBK 控制台"这条真实用法。
    """
    raw = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return subprocess.run(
        [sys.executable, "-m", "pm.verdict", "-"],
        input=raw,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        cwd=str(ROOT),
    )


def test_module_cli_accepts_a_wellformed_payload() -> None:
    proc = _run_verdict({"verdict": VERDICT_CONFIRMED, "ok": True, "unvalidated": []})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "通过" in proc.stdout


def test_module_cli_rejects_an_inconsistent_payload() -> None:
    proc = _run_verdict({"verdict": VERDICT_NEEDS_VALIDATION, "ok": True, "unvalidated": []})
    assert proc.returncode == 1
    assert "不通过" in proc.stdout


def test_module_cli_reads_utf8_from_stdin_on_a_gbk_console() -> None:
    """中文载荷经管道喂进来时必须活得下来。

    本仓对这类事故有专门记录（`tests/test_subprocess_encoding.py`）：Windows 控制台的
    stdin 默认按本地码页解码，中文在读到之前就烂了。校验器在这里崩掉比不校验更糟 ——
    它会让调用方以为"载荷坏了"。
    """
    payload = {
        "verdict": VERDICT_NEEDS_VALIDATION,
        "ok": False,
        "unvalidated": [{"code": "no_baseline", "fact": "没有可对比的基线，未做任何回归判定"}],
    }
    proc = _run_verdict(payload)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "没有" not in proc.stderr, "中文不该在 stderr 里变成乱码或异常"


def test_module_cli_usage_error_is_exit_two() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "pm.verdict"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        cwd=str(ROOT),
    )
    assert proc.returncode == 2
    assert "用法" in proc.stderr
