"""提示词改动的 CI 回归门禁：只拦「这次改动**新引入**」的规则问题。

为什么不能把 `check` 直接搬进 CI —— 这是本模块存在的全部理由：
`pm/quality.py` 的规则是给**交付给目标模型的提示词**设计的，而本仓库里现存的
17 个节点提示词模板**有 16 个天然命中规则**（它们是模板，本身就含 `<<占位符>>`
与 `<标签>`，且 OPTIMIZER/REVISER 这类模板的正文里必然出现"不要输出元说明"一类
抑制式措辞）。把绝对判据套上去，门禁从第一天起就永久假红。
而**一个永远红的门禁等于没有门禁**——它会被绕过、被无视，最后变成
"本地跑过、CI 也绿"的错觉（本仓库对"配置存在但从未真跑"的状态有过专门记录，
见 .pre-commit-config.yaml 顶部注释）。

所以这里只回答一个问题：
**相对基线版本，这次改动有没有引入新的规则失败模式？**

三条设计约束：
1. **基线从 git 取**（`git show <ref>:<path>`），不 import 基线版本——基线代码
   可能与本版本不兼容，也可能根本不该被执行。用 AST 静态抽取字符串常量。
2. **改好了不算回归**（`fixed` 非空不判失败）：门禁不回答"够不够好"，
   只回答"有没有变坏"——后者才是唯一有客观判据的那一半（与 `run.py diff` 同一立场）。
3. **拿不到基线要吵**，不许静默放行：「比不了」不等于「没回归」。唯一的例外是
   首次推送（全零 SHA，客观上不存在基线），那种情况显式打横幅说明并跳过。

判据与 `run.py diff` 同源（都用 `pm.promptdiff`），`tests/test_promptgate.py`
有交叉断言钉住"门禁的判定 == run.py diff 的判定"，两处不会漂移。

用法：
    python run.py gate                     # 自动探测基线（origin/main → … → HEAD~1）
    python run.py gate --base origin/main   # 显式指定
    python run.py gate --json               # 机器可读（CI 消费）
"""

from __future__ import annotations

import ast
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .promptdiff import _items_delta, rule_delta, structure_delta

# 默认门禁对象：仓库里唯一的"提示词资产"文件。
# case_templates/*.json 是**测试用例**（input/expected）不是提示词；
# releases/ 下的是套件运营文件。这个默认值是"事实源"而不是"顺手挑的"，
# 改动它之前先确认提示词模板还在这一个文件里。
DEFAULT_PATHS: tuple[str, ...] = ("pm/prompts.py",)

# 自动探测基线的顺序：CI 之外的本地手工跑也要能开箱可用
_DEFAULT_BASE_CANDIDATES: tuple[str, ...] = (
    "origin/main",
    "origin/master",
    "main",
    "master",
    "HEAD~1",
)

# 全零 SHA：GitHub 在"新分支首次推送"时给 github.event.before 的值。
_ZERO_SHA = "0" * 40


class GateError(Exception):
    """门禁**自己没跑起来**（拿不到基线、抽取不到模板…），与"改动有问题"分开。

    两者必须分开报：前者要人修门禁（或 CI 配置），后者要人改提示词。
    混成一个退出码会让"门禁坏了"看起来像"代码有毛病"。
    """


class NoBaseline(Exception):
    """客观上不存在可对比的基线（首次推送）——这是唯一允许跳过的情形。"""


@dataclass
class ChangedTemplate:
    """一个文本有改动的模板及其规则得失。"""

    name: str
    path: str
    introduced: list[str] = field(default_factory=list)
    fixed: list[str] = field(default_factory=list)
    still: list[str] = field(default_factory=list)
    constraint_delta: int = 0
    # 已命中规则上新增的命中项（code 集合看不出的一类回归）
    added_hits: dict[str, list[str]] = field(default_factory=dict)


def _git(args: list[str], cwd: Path) -> tuple[int, str]:
    """跑一条 git 命令，返回 (rc, stdout)；异常一律折成 rc=127（门禁不该因 git 抖崩掉）。"""
    try:
        proc = subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(cwd),
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return 127, ""
    return proc.returncode, proc.stdout or ""


def extract_templates(source: str) -> dict[str, str]:
    """静态抽取顶层「全大写名字 = 字符串常量」，返回 {名字: 正文}。

    只认 `ast.Constant` 的字符串（隐式拼接会被解析器折成一个常量，仍然取得到）。
    f-string / 拼接表达式不抽取——它们不是可以直接对比的静态文本，
    而"抽取不到"必须在 `gate()` 里被显式检出来（否则门禁会因为看不见而变绿）。

    不做 import：基线版本的代码可能与本版本不兼容，也可能有 import 副作用。
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        raise GateError(f"源码无法解析（{e.msg}，第 {e.lineno} 行）") from e

    out: dict[str, str] = {}
    for node in tree.body:
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets = [node.target]
        else:
            continue
        value = node.value
        if not (isinstance(value, ast.Constant) and isinstance(value.value, str)):
            continue
        for t in targets:
            if isinstance(t, ast.Name) and t.id.isupper():
                out[t.id] = value.value
    return out


def read_revision(ref: str, path: str, cwd: Path) -> str | None:
    """取 `ref:path` 的正文；该版本没有这个文件时返回 None（不是错误）。"""
    rc, out = _git(["show", f"{ref}:{path}"], cwd)
    return out if rc == 0 else None


def resolve_base(ref: str | None, cwd: Path, head_sha: str | None = None) -> tuple[str, str]:
    """把 --base 解析成 commit SHA，返回 (sha, 来源说明)。

    - 显式给了 ref 却解析不出来 → GateError（**不许假装通过**）；
    - 全零 SHA → NoBaseline（首次推送，客观上没有基线）；
    - 没给 ref → 按 _DEFAULT_BASE_CANDIDATES 依次探测，并**跳过等于 HEAD 的候选**。

    为什么自动探测要跳过 HEAD 自己（实测缺陷，2026-10-02）：单提交仓库里
    `master` 是能解析的，而它**就是 HEAD**——拿它当基线，diff 出来的永远是"没有改动"，
    门禁静默通过。这比"报错"危险得多：它长得像一次成功的检查。
    显式 `--base HEAD` 仍然允许（本地对比"已提交版 vs 工作区"是正当用法），
    只有自动探测这一条路要求基线真的是"更早的状态"。
    """
    explicit = (ref or "").strip()
    if explicit == _ZERO_SHA:
        raise NoBaseline("基线是全零 SHA：本次为新分支首次推送，客观上没有可对比的版本")

    candidates = (explicit,) if explicit else _DEFAULT_BASE_CANDIDATES
    saw_head_itself = False
    for cand in candidates:
        if not cand or cand == _ZERO_SHA:
            continue
        rc, out = _git(["rev-parse", "--verify", f"{cand}^{{commit}}"], cwd)
        if rc != 0 or not out.strip():
            continue
        sha = out.strip()
        if not explicit and head_sha and sha == head_sha:
            saw_head_itself = True
            continue
        how = "显式 --base" if explicit else f"自动探测（{cand}）"
        return sha, how

    if explicit:
        raise GateError(
            f"基线 {explicit!r} 解析不出来（git rev-parse 失败）。"
            "CI 上要先 fetch 到该提交（actions/checkout 需要 fetch-depth: 0）；"
            "本地跑请给一个存在的 ref，例如 --base origin/main"
        )
    if saw_head_itself:
        raise GateError(
            "自动探测到的候选分支全都等于当前 HEAD（仓库可能只有一个提交，"
            "或本地分支没有落后的上游）。没有「更早的状态」可比，请用 --base <ref> "
            "显式指定一个真正的历史提交"
        )
    raise GateError(
        "找不到可用的基线，已试过：" + "、".join(_DEFAULT_BASE_CANDIDATES) + "。"
        "请用 --base <ref> 显式指定（如 --base origin/main）"
    )


def _repo_relative(path: str, root: Path) -> str:
    """把 --path 归一化成 git 侧的仓库相对路径（POSIX 分隔符）。

    为什么必须有这一步（实测缺陷，2026-10-02）：传绝对路径时，
    `git show <ref>:<绝对路径>` 永远失败 → 基线取不到 → 所有模板都被算成
    "本次新增" → **判定为空、静默放行**。它长得像"没有回归"，
    实际是"根本没比"。CI 里路径是相对的所以看不出问题，但本地用绝对路径
    调试的人会得到一个假的绿灯。

    若路径不在仓库内，返回 POSIX 化的原样路径（git 那边取不到就取不到，
    由调用方按"基线文件不存在"处理 —— 那种情况仍会被显式披露）。
    """
    p = Path(path)
    if not p.is_absolute():
        return p.as_posix()
    try:
        return p.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return p.as_posix()


def gate(
    base_ref: str | None = None,
    paths: tuple[str, ...] = DEFAULT_PATHS,
    root: Path | None = None,
) -> dict[str, Any]:
    """执行门禁，返回结果字典（不抛"回归"异常，由调用方按 regressed 定退出码）。

    `skipped=True` 表示客观上没有基线（首次推送），不是"没发现问题"——
    调用方与报告都必须把这两件事说清楚。
    """
    root = root or Path.cwd()
    result: dict[str, Any] = {
        "mode": "prompt_gate",
        "paths": list(paths),
        "base": None,
        "base_how": "",
        "skipped": False,
        "skip_reason": "",
        "changed": [],
        "unchanged_count": 0,
        "added_templates": [],
        "removed_templates": [],
        "notes": [],
        "regressed": False,
        "ok": True,
    }

    try:
        rc_head, head_out = _git(["rev-parse", "HEAD"], root)
        head_sha = head_out.strip() if rc_head == 0 and head_out.strip() else None
        base_sha, how = resolve_base(base_ref, root, head_sha=head_sha)
    except NoBaseline as e:
        result["skipped"] = True
        result["skip_reason"] = str(e)
        result["notes"].append("本次未做回归判定。注意：这是「没有基线可比」，不是「检查通过」。")
        return result

    result["base"] = base_sha
    result["base_how"] = how

    for path in paths:
        head_file = root / path
        if not head_file.exists():
            raise GateError(f"{path} 在本次检出里不存在，门禁对象配置有误")
        # git 侧要的是仓库相对路径：传绝对路径会让 `git show <ref>:<path>` 永远失败，
        # 于是所有模板被算成"新增"、判定为空、**静默放行**（实测踩过）。
        rel = _repo_relative(path, root)

        head_src = head_file.read_text(encoding="utf-8")
        if base_src := read_revision(base_sha, rel, root):
            base_templates = extract_templates(base_src)
        else:
            # 基线版本没有这个文件 = 整个文件都是新增的：没有可比的基线，
            # 全部按"新增模板"披露，不做规则判定（否则会把既有命中全报成新引入）。
            # ⚠️ 但这**也可能是路径写错**（绝对路径 / 拼错文件名）——那种情况下
            # 判定同样为空，看起来完全一样。所以这里必须显式记进 notes，
            # 让"没比对成"与"比对后没发现问题"在输出里分得开。
            base_templates = {}
            result["notes"].append(
                f"⚠️ 基线（{base_sha[:12]}）里找不到 `{rel}`：无法与历史版本对比，"
                "本次对该文件**未做任何回归判定**。若这个文件在基线里本该存在，"
                "说明 --path 写错了（注意必须是仓库相对路径）"
            )

        head_templates = extract_templates(head_src)
        if not head_templates:
            # 这是门禁最危险的失效方式：抽取不到任何模板 → 判定为空 → 永远绿。
            # 宁可报错也不能让它绿着。常见成因：模板被挪到别的文件 / 改名规则变了。
            raise GateError(
                f"从 {path} 里抽取不到任何「全大写 = 字符串常量」模板；"
                "门禁没有检查对象就不会发现问题。若模板已挪位置，请同步更新 --path"
            )

        for name in sorted(set(head_templates) - set(base_templates)):
            result["added_templates"].append(f"{name}（{path}）")
        for name in sorted(set(base_templates) - set(head_templates)):
            result["removed_templates"].append(f"{name}（{path}）")

        for name in sorted(set(head_templates) & set(base_templates)):
            before, after = base_templates[name], head_templates[name]
            if before == after:
                result["unchanged_count"] += 1
                continue
            d = rule_delta(before, after)
            sd = structure_delta(before, after)
            # 同一 code 下命中项的新增：只保留"该 code 本来就命中"的那些
            # （本来是干净的 code 会走 introduced 通道，不必重复报）
            items = _items_delta(before, after)
            added_hits = {c: v["added"] for c, v in items.items() if v["added"] and c in d["still"]}
            result["changed"].append(
                {
                    "name": name,
                    "path": path,
                    "introduced": d["introduced"],
                    "fixed": d["fixed"],
                    "still": d["still"],
                    "constraint_delta": sd["constraints"]["delta"],
                    "chars_delta": sd["chars"]["delta"],
                    "added_hits": added_hits,
                }
            )

    introduced_total = sum(len(c["introduced"]) for c in result["changed"])
    added_hits_total = sum(len(v) for c in result["changed"] for v in c["added_hits"].values())
    result["introduced_total"] = introduced_total
    result["added_hits_total"] = added_hits_total
    result["regressed"] = introduced_total > 0 or added_hits_total > 0
    result["ok"] = not result["regressed"]

    if result["added_templates"]:
        result["notes"].append(
            f"{len(result['added_templates'])} 个模板是本次新增的，**没有基线可比，不参与规则判定**"
            "（把它们的既有命中报成「新引入」会是假红）。请人工过一眼："
            + "、".join(result["added_templates"][:6])
        )
    if result["removed_templates"]:
        result["notes"].append(
            f"{len(result['removed_templates'])} 个模板在本次改动中被删除或改名："
            + "、".join(result["removed_templates"][:6])
        )
    return result


def render_text(result: dict[str, Any]) -> str:
    """人读版输出：先说结论，再给逐模板证据。"""
    lines: list[str] = ["提示词改动回归门禁", ""]

    if result["skipped"]:
        lines.append("⏭️  跳过：" + result["skip_reason"])
        lines.append("")
        lines.append("> 「比不了」不等于「没回归」。本次没有任何回归判定被做出。")
        return "\n".join(lines)

    lines.append(f"基线：{str(result['base'])[:12]}（{result['base_how']}）")
    lines.append(f"检查对象：{', '.join(result['paths'])}")
    lines.append(f"未改动模板：{result['unchanged_count']} 个")
    lines.append("")

    if result["regressed"]:
        lines.append("❌ 本次改动**新引入**了确定性规则问题：")
        for c in result["changed"]:
            if not c["introduced"]:
                continue
            lines.append(
                f"  - {c['name']}（{c['path']}）：{', '.join(c['introduced'])}"
                f"｜字符 {c['chars_delta']:+d}"
            )
        extra = sum(len(v) for c in result["changed"] for v in c["added_hits"].values())
        if extra:
            lines.append(
                f"  - 另有 {extra} 处在**已命中的规则上继续加重**"
                "（code 集合前后没变，只比 code 的判据看不见这类回归）："
            )
            for c in result["changed"]:
                for code, added in c["added_hits"].items():
                    for item in added:
                        lines.append(f"      · {c['name']} [{code}] 新增「{item}」")
        lines.append("")
        lines.append(
            "> 这些不是「这版不够好」，而是**这次改动把一个已知事故模式带了回来**。"
            "每条规则都对应一次真实事故，判据与 `run.py diff` 同源。"
        )
    else:
        lines.append("✅ 未新引入任何确定性规则问题。")

    fixed_rows = [c for c in result["changed"] if c["fixed"]]
    if fixed_rows:
        lines.append("")
        lines.append("（顺带修掉的问题，不计入判定）：")
        for c in fixed_rows:
            lines.append(f"  - {c['name']}：{', '.join(c['fixed'])}")

    if result["changed"]:
        lines.append("")
        lines.append("本次有文本改动的模板：")
        for c in result["changed"]:
            lines.append(
                f"  - {c['name']}：字符 {c['chars_delta']:+d}，"
                f"约束条目 {c['constraint_delta']:+d}，"
                f"新引入 {len(c['introduced'])} / 修好 {len(c['fixed'])}"
            )

    for n in result["notes"]:
        lines.append("")
        lines.append(f"ℹ️  {n}")
    return "\n".join(lines)
