"""节点提示词回归评测骨架（提示词#2）的 pytest 集成。

`eval_prompts.py` 的离线模式是零成本结构契约校验，这里让它进入常规回归：
任何人改 `pm/prompts.py` 破坏了模板结构（占位符、安全约束、专项规则块），
pytest 直接红，不用等到真实跑才发现。真实模式（--live）花真钱，保持手动执行。
"""

from __future__ import annotations

import inspect

import eval_prompts


def test_offline_prompt_eval_all_pass():
    """离线结构契约：全部用例必须通过（模板改动破坏结构时立刻失败）。"""
    cases = eval_prompts.load_cases()
    failures: list[str] = []
    for node in sorted(eval_prompts.TEMPLATES):
        for case in cases.get(node, []):
            issues = eval_prompts.check_structure(node, case)
            if issues:
                failures.append(f"{node}/{case['name']}: {issues}")
    assert not failures, "节点提示词结构契约被破坏：\n" + "\n".join(failures)


def test_eval_cases_cover_all_core_nodes():
    """用例集必须覆盖全部 5 个可评测节点（新提示词节点接入时同步补用例）。"""
    cases = eval_prompts.load_cases()
    for node in eval_prompts.TEMPLATES:
        assert cases.get(node), f"用例集缺少节点 {node} 的用例"


def test_live_check_reviser_growth_rule():
    """真实校验器的修订净增量规则存在且阈值正确（30% + 绝对下限 200 字）。

    --live 首轮验证发现纯相对阈值对短基准过严（65 字基准只有约 20 字余量），
    已改为 max(200 字, 30%)：短基准有合理补全空间，长基准仍守 30%。
    """
    # 不真正调用 LLM：只验证 check_live 的增量判定逻辑片段可独立复核
    src = inspect.getsource(eval_prompts.check_live)
    assert "REVISER_GROWTH_ABS_FLOOR" in src and "净增量" in src
    assert eval_prompts.REVISER_GROWTH_RATIO == 0.3
    assert eval_prompts.REVISER_GROWTH_ABS_FLOOR == 200


def test_growth_threshold_short_baseline():
    """短基准（65 字）的净增量余量应为绝对下限 200 字，而不是 30%（约 20 字）。"""
    prev = "a" * 65
    margin = max(
        eval_prompts.REVISER_GROWTH_ABS_FLOOR, len(prev) * eval_prompts.REVISER_GROWTH_RATIO
    )
    assert margin == 200
    # 265 字输出（旧逻辑必然超限）在新阈值下放行
    assert len(prev) + margin == 265
    # 长基准（1000 字）余量回到 30% = 300 字
    margin_long = max(
        eval_prompts.REVISER_GROWTH_ABS_FLOOR, 1000 * eval_prompts.REVISER_GROWTH_RATIO
    )
    assert margin_long == 300


# --------------------------------------------------------------------------
# 真实 CLi 入口层：函数级判据已被 test_offline_prompt_eval_all_pass 覆盖，
# 但**入口本身的行为**此前没有任何东西守着 —— 2026-10-02 变异测试实测：
# 把 `return 1 if failed else 0` 改成 `return 0`、把 `--node` 过滤注释掉，
# pytest 依然全绿。而入口正是人在 CI 里真正调用的那一层。
# --------------------------------------------------------------------------
import json  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
from pathlib import Path  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
EVAL = [sys.executable, str(ROOT / "eval_prompts.py")]


def _run_eval(*args: str, env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [*EVAL, *args],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
        env={**os.environ, **(env_extra or {})},
    )


def test_entry_runs_offline_and_exits_zero():
    """离线全量必须能在 CI 那套环境（无 Key）下跑通并 rc=0。"""
    r = _run_eval(env_extra={"PM_API_KEY": "", "PM_TARGET_API_KEY": ""})
    assert r.returncode == 0, (r.stdout[-400:], r.stderr[-400:])
    assert "6/6 通过" in r.stdout
    assert "[FAIL]" not in r.stdout


def test_entry_exit_code_reflects_failures(tmp_path):
    """**缺口实证**：入口的退出码必须真的反映失败。

    此前把 `return 1 if failed else 0` 改成 `return 0` 时 pytest 全绿 ——
    也就是说"报告说有失败但退出码 0"这种最典型的门禁失效没有任何东西守着。
    这里用一个必然失败的场景（把用例集指向坏模板）间接验判据：
    直接改产品源码不合适，所以用一个能确定性失败的注入点 —— 见下一条。
    """
    # 用 --node 传非法值验证 argparse 的 rc=2（与全局协议一致）
    r = _run_eval("--node", "__不存在__")
    assert r.returncode == 2, (r.stdout[-200:], r.stderr[-200:])


def test_entry_node_filter_actually_filters():
    """`--node` 必须真的只跑一个节点（变异测试实测：把它改成跑全量，pytest 仍绿）。"""
    r = _run_eval("--node", "clarifier")
    assert r.returncode == 0, r.stdout[-300:]
    assert "2/2 通过" in r.stdout, r.stdout[-300:]
    assert "optimizer" not in r.stdout, "只选了 clarifier，却出现了别的节点的结果"
    # 全量应远多于单节点
    full = _run_eval()
    assert "6/6 通过" in full.stdout


def test_entry_reports_mode_so_live_is_not_confused_with_offline():
    """离线与真实两种模式必须在输出里分得清 —— 否则"跑过了"会被误读成"验过效果"。"""
    r = _run_eval("--node", "clarifier")
    assert "离线结构契约" in r.stdout
    assert "真实调用" not in r.stdout


def test_entry_help_advertises_live_flag():
    r = _run_eval("--help")
    assert r.returncode == 0
    assert "--live" in r.stdout and "--node" in r.stdout


# --------------------------------------------------------------------------
# 退出码：门禁的唯一判据面
# --------------------------------------------------------------------------
def _failing_cases(tmp_path: Path) -> Path:
    """构造一份**必然失败**的用例集：需求里声明 optimizer 必须有 <输出前自检>，
    但给这条用例喂一个被破坏的模板变量（缺 vars），渲染后必然留占位符 → FAIL。

    为什么需要注入点（2026-10-02 实测）：仓内用例集全通过，于是
    `return 1 if failed else 0` 改成 `return 0` **pytest 依然全绿** ——
    "报告里有失败但退出码 0"这种最典型的门禁失效没有任何东西守着。
    要守住它，必须能在确有失败时观察退出码。
    """
    f = tmp_path / "cases.json"
    f.write_text(
        json.dumps(
            {
                "clarifier": [
                    {
                        "name": "缺变量的用例（必然渲染失败）",
                        "vars": {},  # 关键：不给任何变量 → 占位符残留
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return f


def test_exit_code_is_one_when_a_case_fails(tmp_path):
    """**缺口实证**：确有失败时必须 rc=1。

    这条是整条 CI 步骤的判据面 —— 没有它，"门禁"只是打印了一堆 PASS/FAIL
    却永远返回 0，CI 永远不会因为提示词结构被破坏而红。
    """
    cases = _failing_cases(tmp_path)
    r = _run_eval(env_extra={"PM_EVAL_CASES": str(cases)})
    assert r.returncode == 1, (r.stdout[-400:], r.stderr[-400:])
    assert "[FAIL]" in r.stdout
    assert "0/1 通过" in r.stdout


def test_exit_code_zero_only_when_everything_passes():
    r = _run_eval()
    assert r.returncode == 0
    assert "6/6 通过" in r.stdout


def test_cases_path_is_overridable_by_env(tmp_path):
    """PM_EVAL_CASES 必须生效（退出码注入点依赖它，也是扩展用例集的正规入口）。"""
    cases = _failing_cases(tmp_path)
    r = _run_eval(env_extra={"PM_EVAL_CASES": str(cases)})
    assert "0/1 通过" in r.stdout, r.stdout[-300:]


def test_missing_cases_file_fails_loudly(tmp_path):
    """指错路径要当场报清楚，不许静默回退到默认用例集 ——
    那种"回退"会让 CI 拿一份**不是你以为的**用例集打绿。"""
    r = _run_eval(env_extra={"PM_EVAL_CASES": str(tmp_path / "__不存在__.json")})
    assert r.returncode != 0
    assert "用例集读不到" in (r.stdout + r.stderr)


def test_broken_cases_json_fails_loudly(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{ 这不是 JSON", encoding="utf-8")
    r = _run_eval(env_extra={"PM_EVAL_CASES": str(bad)})
    assert r.returncode != 0
    assert "不是合法 JSON" in (r.stdout + r.stderr)


def test_load_cases_returns_only_node_keys_and_runs_without_errors(tmp_path):
    """用例集里的 `_comment` 是说明键，`load_cases()` 的返回值不该把它当节点。

    注意这里断言的是**返回值语义**，不是"不过滤就会跑挂"：
    `main()` 遍历的是 `sorted(TEMPLATES)` 并用 `cases.get(node, [])` 取用例，
    所以 `_comment` 本来就不会被当成节点执行 —— 实测把过滤去掉，pytest 仍然全绿，
    说明它不是一个"会导致跑挂"的缺陷。测试不该给一个不存在的行为加断言
    （那种断言只会让人以为改了它就会出事）。
    """
    cases = tmp_path / "c.json"
    cases.write_text(
        json.dumps({"_comment": "说明", "clarifier": []}, ensure_ascii=False), encoding="utf-8"
    )
    r = _run_eval(env_extra={"PM_EVAL_CASES": str(cases)})
    assert r.returncode == 0 and "结果：0/0 通过" in r.stdout, r.stdout[-300:]
    # 返回值里不该出现说明键
    import eval_prompts as _E

    orig = _E.cases_path
    try:
        _E.cases_path = lambda: cases  # type: ignore[assignment]
        assert "_comment" not in _E.load_cases()
    finally:
        _E.cases_path = orig  # type: ignore[assignment]


# --------------------------------------------------------------------------
# CI 接线护栏：这一步的配置漂了，节点提示词的回归就少了一半
# --------------------------------------------------------------------------
def _ci_yaml() -> dict:
    import yaml

    return yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))


def test_both_ci_jobs_run_the_offline_prompt_eval():
    """两条 CI 线都要跑 —— 与 gate 步骤同一条纪律（少一条就是第三种口径）。"""
    jobs = _ci_yaml()["jobs"]
    for job_name, job in jobs.items():
        runs = "\n".join(str(s.get("run", "")) for s in job["steps"])
        assert "eval_prompts.py" in runs, f"{job_name} job 没有跑节点提示词结构契约"


def test_ci_never_runs_eval_prompts_live():
    """`--live` 打真实端点、花真钱，**绝不能进 CI**。

    它一旦被写进 CI，绿不绿取决于外部端点状态，而且每次提交都在烧额度 ——
    这是那种"加进去当天没事、之后才发现账单"的配置错误。
    """
    for job_name, job in _ci_yaml()["jobs"].items():
        for st in job["steps"]:
            run = str(st.get("run", ""))
            if "eval_prompts.py" not in run:
                continue
            # 逐行看：允许注释里提 --live（说明为什么不用），但调用行不许带
            for line in run.splitlines():
                code = line.split("#", 1)[0].strip()
                assert "--live" not in code, (
                    f"{job_name} job 里把 --live 写进了 CI：{line.strip()!r}"
                )


def test_ci_prompt_eval_step_is_separate_from_the_change_gate():
    """两条步骤必须**并存**：gate 判增量（有没有变坏），
    eval_prompts 判绝对结构契约（是不是残破）。一条替不掉另一条。"""
    job = _ci_yaml()["jobs"]["test"]
    runs = "\n".join(str(s.get("run", "")) for s in job["steps"])
    assert "run.py gate" in runs, "缺少改动门禁步骤"
    assert "eval_prompts.py" in runs, "缺少结构契约步骤"


def test_structure_contract_catches_what_the_change_gate_cannot():
    """实证两条判据不可互替 —— 用**真实模板**做证据，而不是编一个例子。

    事实（2026-10-02 实测）：把 `pm/prompts.py` 里 CLARIFIER_SYSTEM 的整个
    `<安全约束>` 块（注入防御声明）删掉时：
      · `run.py gate` 的增量判据 `introduced == []` —— **完全看不见**
        （删块不新增任何 quality 规则命中：base 与 broken 命中的都是
         context_leak + delimiter_unbalanced，一模一样）；
      · `eval_prompts` 的结构契约明确抓到「system 缺少 <安全约束> 块」。

    所以"删掉安全约束声明"这类**结构缺失**只能靠绝对契约判据守；
    而绝对契约判据对 16/17 个模板天然命中、会永久假红，所以不能单独用在改动门禁上。
    两步各守一半，一条替不掉另一条。
    """
    from pm import prompts
    from pm.promptdiff import rule_delta

    base = prompts.CLARIFIER_SYSTEM
    broken = base.replace("<安全约束>", "").replace("</安全约束>", "")
    assert broken != base and len(broken) > 500  # 不是靠"变短"触发的 too_short

    # ① gate 的增量判据看不见结构缺失
    assert rule_delta(base, broken)["introduced"] == [], (
        "若这条不成立（gate 能看见删安全约束），说明判据已变，需重新论证两步的必要性"
    )

    # ② 结构契约判据必须看见
    orig = eval_prompts.TEMPLATES["clarifier"]
    try:
        eval_prompts.TEMPLATES["clarifier"] = (broken, orig[1])
        got = eval_prompts.check_structure("clarifier", {"name": "x", "vars": {"task": "t"}})
    finally:
        eval_prompts.TEMPLATES["clarifier"] = orig
    assert any("安全约束" in i for i in got), f"结构契约没抓到缺失的安全约束块：{got}"


def test_gate_extraction_covers_the_same_prompts_module():
    """两个门禁的对象必须都是 `pm/prompts.py` —— 对象漂了就是各查各的。"""
    from pm.promptgate import DEFAULT_PATHS, extract_templates

    assert "pm/prompts.py" in DEFAULT_PATHS
    names = set(extract_templates((ROOT / "pm" / "prompts.py").read_text(encoding="utf-8")))
    assert "CLARIFIER_SYSTEM" in names and len(names) >= 10


# --------------------------------------------------------------------------
# 判据强度：子串判据会被"只删一半标签"绕过（实测漏洞，2026-10-02）
# --------------------------------------------------------------------------
def test_safety_block_requires_paired_tags_not_a_substring():
    """**实测漏洞**：原来判据是 `"安全约束" not in system`。

    而 `</安全约束>`（闭合标签）里**仍然含"安全约束"这个子串** ——
    于是把开标签 `<安全约束>` 删掉、只留闭合标签时，残破模板照样判合格，
    一路绿过 CI。而那种模板在真实调用里起不到任何边界声明作用
    （接收方只看得到一段孤立的 `</安全约束>`）。

    现在判据要求**成对标签**。这条用例对全部 5 个节点逐一验证，
    两个方向（删开 / 删闭）都必须被抓到。
    """
    from pm import prompts

    node_const = {
        "clarifier": "CLARIFIER_SYSTEM",
        "optimizer": "OPTIMIZER_SYSTEM",
        "reviser": "REVISER_SYSTEM",
        "evaluator": "EVALUATOR_SYSTEM",
        "mockgen": "MOCKGEN_SYSTEM",
    }
    cases = {
        "clarifier": {"task": "t", "context": "c", "target_model": "m"},
        "optimizer": {"task": "t", "context": "c", "target_model": "m", "model_profile": "p"},
        "reviser": {
            "task": "t",
            "context": "c",
            "target_model": "m",
            "previous_prompt": "x" * 120,
            "feedback": "f",
        },
        "evaluator": {"task": "t", "test_input": "i", "test_output": "o", "prompt": "p"},
        "mockgen": {"task": "t", "context": "c", "n": "3"},
    }

    for node, const in node_const.items():
        real = getattr(prompts, const)
        assert "<安全约束>" in real and "</安全约束>" in real, f"{const} 没有成对安全约束块"
        orig = eval_prompts.TEMPLATES[node]
        for kind, tgt in (("删开标签", "<安全约束>"), ("删闭标签", "</安全约束>")):
            broken = real.replace(tgt, "", 1)
            assert broken != real
            try:
                eval_prompts.TEMPLATES[node] = (broken, orig[1])
                got = eval_prompts.check_structure(node, {"name": "x", "vars": cases[node]})
            finally:
                eval_prompts.TEMPLATES[node] = orig
            assert any("安全约束" in i for i in got), (
                f"{node}/{kind} 没被结构契约抓到（子串判据被绕过）：{got}"
            )


def test_structure_check_flags_missing_placeholder_vars():
    """用例缺变量时必须报"未渲染占位符"——这是最常见的模板漂移信号。"""
    got = eval_prompts.check_structure("clarifier", {"name": "x", "vars": {}})
    assert any("未渲染占位符" in i for i in got), got


# --------------------------------------------------------------------------
# 手动触发的 --live 工作流：配置漂了就会"点下去才发现"，或者更糟——悄悄烧钱
# --------------------------------------------------------------------------
LIVE_WF = ROOT / ".github" / "workflows" / "eval-prompts-live.yml"


def _wf() -> dict:
    import yaml

    return yaml.safe_load(LIVE_WF.read_text(encoding="utf-8"))


def _triggers(wf: dict) -> dict:
    """`on:` 在 YAML 1.1 里会被解析成布尔 True —— 别用 wf["on"] 取。"""
    return wf.get("on") or wf.get(True) or {}


def _run_lines(wf: dict) -> list[str]:
    return [str(s.get("run", "")) for s in wf["jobs"]["live"]["steps"]]


def test_live_workflow_exists_and_is_manual_only():
    """`--live` 只能靠人主动点：**不许**有任何自动触发器。

    加一个 push/pull_request/schedule 触发器，就等于把"打真实端点、花真钱"的动作
    变回每次提交都跑 —— 那正是把它从 ci.yml 拆出来的原因。
    """
    assert LIVE_WF.is_file(), "缺少手动触发的 live 评测工作流"
    trig = _triggers(_wf())
    assert "workflow_dispatch" in trig, "没有 workflow_dispatch，人点不了"
    for auto in ("push", "pull_request", "pull_request_target", "schedule", "release"):
        assert auto not in trig, f"live 工作流出现了自动触发器 {auto} —— 它会开始烧钱"


def test_ci_workflow_still_has_no_live_call():
    """反过来也要守住：ci.yml（自动跑的那个）里不许出现 `--live`。"""
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    for line in ci.splitlines():
        code = line.split("#", 1)[0]
        assert "--live" not in code, f"ci.yml 里出现了 --live：{line.strip()!r}"


def test_live_workflow_node_choices_match_the_real_templates():
    """下拉框的选项必须与 `eval_prompts.TEMPLATES` 一致。

    不一致会导致两种坏结果：选了模板里没有的节点 → 白跑一次真实调用；
    模板新增节点却没进选项 → 那个节点永远评测不到（而且没人会发现）。
    """
    wf = _wf()
    options = set(_triggers(wf)["workflow_dispatch"]["inputs"]["node"]["options"])
    assert options - {"all"} == set(eval_prompts.TEMPLATES), (
        f"工作流选项与模板不符：工作流多 {options - {'all'} - set(eval_prompts.TEMPLATES)}，"
        f"模板多 {set(eval_prompts.TEMPLATES) - options}"
    )


def test_live_workflow_passes_node_through_to_the_cli():
    """选了节点要真的传下去 —— 只写在下拉框里而命令里不传，等于选项是装饰。"""
    joined = "\n".join(_run_lines(_wf()))
    assert "--node" in joined, "工作流没有把 node 选项传给 eval_prompts.py"
    assert "inputs.node" in joined, "没有引用 inputs.node，选项不会生效"
    assert "--live" in joined, "手动工作流的重点就是 --live"


def test_live_workflow_has_a_no_spend_dry_switch():
    """必须有一个"只查配置、不调用模型"的开关。

    没有它的话，第一次点这个工作流的人会在"密钥还没配好"或"就想确认参数对不对"
    的时候直接产生一次真实花费。
    """
    wf = _wf()
    inputs = _triggers(wf)["workflow_dispatch"]["inputs"]
    assert "dry_run" in inputs, "缺少 dry_run 开关"
    joined = "\n".join(_run_lines(wf))
    assert "inputs.dry_run" in joined, "dry_run 开关没有被任何步骤使用"


def test_live_workflow_references_secrets_never_hardcodes_them():
    """密钥必须走 Secrets 引用，且**任何一处都不许把它打进日志**。"""
    text = LIVE_WF.read_text(encoding="utf-8")
    assert "secrets.PM_API_KEY" in text, "没有引用 secrets.PM_API_KEY"

    code_lines = [ln.split("#", 1)[0] for ln in text.splitlines()]
    # ① 不许硬编码看起来像密钥的字面量
    for bad in ("sk-", "Bearer "):
        for ln in code_lines:
            assert bad not in ln, f"工作流里出现疑似硬编码凭据：{ln.strip()!r}"

    # ② 不许把**密钥变量本身**展开进输出。
    # 判据要精确到"值展开"（`echo $PM_API_KEY` / `echo "${PM_API_KEY}"`），
    # 而不是"出现 PM_API_KEY 这个字符串" —— 后者会把提示文案
    # （`echo "  Secret   PM_API_KEY"`，教用户去哪儿配）也误判成泄漏。
    # 安全栏误报的代价很实在：它逼人删掉本来有用的指引文字。
    dangerous = re.compile(r"(?:echo|printf|cat|>>?)[^\n]*\$\{?(?:#)?PM_API_KEY\}?(?!_)")
    for ln in code_lines:
        m = dangerous.search(ln)
        if not m:
            continue
        # 只允许"只取长度"的形态：${#PM_API_KEY}
        assert "${#PM_API_KEY}" in ln, f"疑似把密钥值打进日志：{ln.strip()!r}"


def test_live_workflow_is_bounded_in_time_and_concurrency():
    """没有超时与并发限制的长任务，最坏情况是"挂到 6 小时"或"两个 job 抢限流"。"""
    wf = _wf()
    assert wf["jobs"]["live"].get("timeout-minutes"), "没有 timeout-minutes，挂住会一直跑"
    assert wf.get("concurrency", {}).get("group"), "没有 concurrency，并发会互相抢端点限流"


def test_live_workflow_preflight_separates_config_errors_from_eval_failures():
    """配置缺失必须**独立成步**并给出可执行的修复指引。

    混在一起的话，"没配 Secret"的失败会看起来像"提示词有问题"——
    而两者要采取的动作完全不同（一个去 Settings 配密钥，一个去改提示词）。
    """
    steps = _wf()["jobs"]["live"]["steps"]
    check_steps = [s for s in steps if "前置检查" in str(s.get("name", ""))]
    assert check_steps, "没有独立的前置检查步骤"
    body = str(check_steps[0]["run"])
    assert "::error::" in body, "前置检查失败时没有输出 GitHub 注解"
    assert "Secrets and variables" in body, "没告诉人去哪儿配"
    assert "exit 1" in body, "缺配置时没有真的失败"


def test_live_workflow_env_blocks_are_identical_across_steps():
    """三个涉及真实调用的步骤，env 必须**逐字一致**。

    实测踩过（2026-10-02 本轮）：给前置检查/冒烟/评测三处分别写 env 时，
    漏了角色级覆盖、还写了 typo（`PM_COMPARATOR_B_BASE_URL` 多一个 `_B`）。
    typo 不会报错 —— `pm.llm` 的 `PM_{role}_X or 全局` 会静默回退到全局模型，
    于是"冒烟过了、评测也过了"，但评测用的根本不是你以为的那个模型。

    三处不一致的后果同样是静默的：冒烟验的是 A 配置、评测跑的是 B 配置。
    """
    steps = _wf()["jobs"]["live"]["steps"]
    # 取**所有**声明了 env 的步骤：前置检查那步的 run 里没有 --preflight/--live
    # （它只做变量存在性判断），但它同样需要完整的端点配置口径 —— 判据按"声明了 env"
    # 取，而不是按命令内容取，否则会把最该一致的那一步漏在外面。
    envs: dict[str, dict] = {str(s.get("name")): dict(s["env"]) for s in steps if s.get("env")}
    assert len(envs) >= 3, f"预期至少 3 个带 env 的步骤，实得 {len(envs)}：{list(envs)}"

    it = iter(envs.items())
    first_name, first = next(it)
    for name, cur in it:
        assert cur == first, (
            f"`{name}` 的 env 与 `{first_name}` 不一致：\n"
            f"  只在 {first_name}：{sorted(set(first) - set(cur))}\n"
            f"  只在 {name}：{sorted(set(cur) - set(first))}\n"
            f"  键相同但取值不同：{sorted(k for k in set(first) & set(cur) if first[k] != cur[k])}"
        )


def test_live_workflow_role_overrides_are_real_roles():
    """工作流里写的角色级变量，角色名必须是 `pm/llm.py` 真的认识的那些。

    防的是拼写漂移：`pm/llm.py` 按 `PM_{ROLE}_API_KEY` / `_BASE_URL` / `_MODEL`
    构造键名。多写一个字母（如 `PM_COMPARATOR_B_BASE_URL`）不会报错 ——
    `_env(f"PM_{role}_X") or 全局` 会静默回退到全局模型，
    于是"冒烟过了、评测也过了"，但评测用的根本不是你以为的那个模型。
    （本轮实测就写出过 `PM_COMPARATOR_B_BASE_URL` 这个 typo。）

    判据来源是**代码事实**（`pm.llm.TEMPERATURES` 的角色表），不是手写清单 ——
    手写清单会和真实角色漂开，而 `evaluator_b` 这种带下划线的角色名
    正是手写清单最容易漏掉的（我第一版测试就把它误判成了拼写错误）。
    """
    from pm.llm import TEMPERATURES

    wf_text = LIVE_WF.read_text(encoding="utf-8")
    keys = set(re.findall(r"^\s+(PM_[A-Z0-9_]+):", wf_text, re.M))
    assert keys, "工作流里没解析到任何 PM_* 键"

    valid_roles = {r.upper() for r in TEMPERATURES}
    for k in keys:
        m = re.fullmatch(r"PM_([A-Z0-9_]+?)_(API_KEY|BASE_URL|MODEL)", k)
        if not m:
            continue  # PM_API_KEY / PM_BASE_URL / PM_MODEL 这类全局键
        role = m.group(1)
        assert role in valid_roles, (
            f"{k} 的角色名 {role!r} 不在 pm.llm 的角色表里（{sorted(valid_roles)}）——"
            "疑似拼写漂移，它会被静默忽略并回退到全局模型"
        )

    for core in ("PM_API_KEY", "PM_BASE_URL", "PM_MODEL"):
        assert core in keys, f"缺少核心变量 {core}"


def test_live_workflow_uses_the_documented_environment_name():
    """工作流声明的 Environment 名必须与文档里让人去配的那个**完全一致**。

    审批是靠 Environment 上的 required reviewers 生效的：名字对不上，
    GitHub 会**静默建一个新 Environment**（默认没有审批人），
    于是这个"需要人工审批"的共识从未真正生效 —— 而工作流照样跑得通，
    没人会发现少了那道闸。这类"名字漂了就静默失效"的东西必须钉住。
    """
    wf = _wf()
    env_name = wf["jobs"]["live"].get("environment")
    assert env_name, "live 工作流没有声明 environment，无法挂 required reviewers"
    assert isinstance(env_name, str), "environment 写成了对象形式，审批人的配置面不同"

    # 判据要强到"文档里真的在教人配这个 Environment"，而不是"这个词出现过"。
    # 只查 `env_name in docs` 太弱：文档里任何一处提到它都能过，
    # 而真正要保证的是"第 2 步让人填的名字就是这个" —— 那一处漂了，审批就静默失效。
    docs = (ROOT / "docs" / "operations.md").read_text(encoding="utf-8")
    assert f"名称填 **`{env_name}`**" in docs, (
        f"docs/operations.md 里没有「名称填 {env_name}」这一步 —— "
        "照文档配的人会建出另一个同义不同名的 Environment，审批静默失效"
    )
    assert f"`environment: {env_name}`" in docs, (
        f"docs/operations.md 没有引用工作流里那一行 `environment: {env_name}`，读者对不上号"
    )
    # 工作流与文档必须是同一个名字（这是整条护栏的核心）
    assert f"environment: {env_name}" in LIVE_WF.read_text(encoding="utf-8")


def test_live_workflow_declares_how_to_enable_approval():
    """工作流自己也要说明"审批怎么开"。

    只在 workflow 里写 `environment: live-eval` 是不够的：`environment` 这一行
    本身**不产生任何审批**，审批来自该 Environment 上的 required reviewers 配置。
    不写清楚，下一个人会以为"写了 environment 就有审批了"。
    """
    text = LIVE_WF.read_text(encoding="utf-8")
    # 必须给出**可照做的步骤**，而不只是出现关键词：
    # 关键词太容易在无关句子（如本文件另一处注释）里被匹配到。
    assert "Required reviewers" in text, (
        "工作流注释里没有提到 Required reviewers —— "
        "`environment:` 本身不产生审批，审批来自 Environment 设置"
    )
    assert "Settings → Environments" in text, "工作流注释里没写去哪里配，等于没说"
    assert "Waiting for review" in text, "没说明配好后会看到什么 —— 读者无法判断审批是否真的生效"
