"""零调用能力的**三态裁决**：`confirmed` / `needs_validation` / `rejected`。

## 为什么要有这个模块（2026-10-26 实测缺陷）

`check` / `diff` / `gate` 三个零调用命令的 `--json` 出口原本只有一个布尔 `ok`，
它把两件**不同的事**混成了一个字段：

- 「比过了，没问题」；
- 「没比成」（没有基线、新增模板没有对照物、考卷里一张否决票都没有）。

实测取证（`gate --base <全零 SHA>`，即新分支首次推送）：

```json
{"skipped": true, "skip_reason": "…客观上没有可对比的版本…", "regressed": false, "ok": true}
```

**门禁一个模板都没比，却报 `ok: true`。** 消费方（Agent / CI 面板 / 别的脚本）
只读 `ok`，就会把"没检查"读成"检查通过" —— 这与本项目"如实说清哪里还不行"
的产品线直接相反，而且它是**静默**的：退出码 0、日志无异常。

## 三态定义（措辞借用 cloudflare/security-audit-skill 的裁决词表）

| 裁决 | 含义 | 硬约束 |
|---|---|---|
| `confirmed` | **真的核验过**，且没发现问题 | 必须真的比过，不能是"跳过" |
| `needs_validation` | **没能核验** | **必须**带至少一条 `unvalidated`，每条给"确切未决事实"，且**不给程度** |
| `rejected` | 核验过，且发现问题 | — |

「必须带确切未决事实、且不给程度」这两条是从 Cloudflare 那份技能的 `needs_validation`
抄来的（它明写：`needs_validation has an exact unresolved fact and no severity`）：
给了程度就会被人当成"轻微问题"直接放过，而不给事实则没人知道该去补什么。

## 与 `ok` / 退出码的分工（这是本模块唯一的语义变更）

- **`ok`** 回答「这次核验结论成立吗」⇒ **只有 `confirmed` 才是 `True`**。
- **退出码** 回答「要不要把 CI 弄红」⇒ 维持原样（`gate` 没有基线时默认放行、`--require-baseline` 才红）。

两者**故意不同步**：门禁没比成，构建不该红（否则首次推送永远红），
但 `ok` 绝不能是 `true`（否则"没查"变成"查过了"）。这条差异写在
`docs/agent-cli-guide.md` 的 JSON 契约里，并由 `tests/test_verdict.py` 钉住。
"""

from __future__ import annotations

import json
from typing import Any

VERDICT_CONFIRMED = "confirmed"
VERDICT_NEEDS_VALIDATION = "needs_validation"
VERDICT_REJECTED = "rejected"

VERDICTS: tuple[str, ...] = (VERDICT_CONFIRMED, VERDICT_NEEDS_VALIDATION, VERDICT_REJECTED)

#: `unvalidated` 条目**只许**有这两个键。程度/严重度/评分一律不许出现 ——
#: 一旦允许"轻微"，读者就会据此放行，而这个裁决的全部价值恰恰是"不许放行"。
UNVALIDATED_KEYS: frozenset[str] = frozenset({"code", "fact"})


class VerdictError(ValueError):
    """裁决载荷自相矛盾（例如 `needs_validation` 却没给出未决事实）。"""


def unvalidated(code: str, fact: str) -> dict[str, str]:
    """构造一条「确切未决事实」。`fact` 要能回答"到底哪个东西没被核验"。"""
    if not code.strip():
        raise VerdictError("unvalidated 的 code 不能为空：消费方要靠它做分支")
    if not fact.strip():
        raise VerdictError(f"unvalidated[{code}] 的 fact 不能为空：没给事实就没法补料")
    return {"code": code, "fact": fact}


def decide(*, rejected: bool, unvalidated_facts: list[dict[str, str]]) -> str:
    """三态裁决。**顺序敏感**：真发现问题优先于"没核验成"。

    「既有新引入、又有一部分没比成」的组合是真实存在的（相对基线改了 3 个模板、
    另有 2 个是新增的）。此时报 `rejected` 是对的：已经确认有问题，
    补料与否都不改变"这次改动引入了问题"这个结论。
    """
    if rejected:
        return VERDICT_REJECTED
    if unvalidated_facts:
        return VERDICT_NEEDS_VALIDATION
    return VERDICT_CONFIRMED


def attach(payload: dict[str, Any], verdict: str) -> dict[str, Any]:
    """把裁决写进 JSON 载荷，并让 `ok` 严格等于「真的核验过且没问题」。

    ⚠️ 这里会**覆盖**调用方原先写的 `ok`。那是本模块存在的理由：
    `ok` 只有一个布尔，而它此前在"跳过"分支上恒为 `True`。
    """
    if verdict not in VERDICTS:
        raise VerdictError(f"未知裁决 {verdict!r}，只允许 {VERDICTS}")
    facts = payload.get("unvalidated") or []
    if verdict == VERDICT_NEEDS_VALIDATION and not facts:
        raise VerdictError("needs_validation 必须带至少一条 unvalidated（确切未决事实）")
    if verdict == VERDICT_CONFIRMED and facts:
        raise VerdictError(
            "confirmed 不许带 unvalidated：有未决事实就说明没核验成，"
            "那是 needs_validation —— 这两件事不能同时成立"
        )
    payload["verdict"] = verdict
    payload["ok"] = verdict == VERDICT_CONFIRMED
    return payload


def _check_fact_item(index: int, item: Any) -> list[str]:
    """单条 `unvalidated` 的形状：只许 code/fact，且两个都不能空。

    （复杂度棘轮曾把 `validate` 判到 CC=12 > 上限 10；按仓规**拆函数**而不是登记豁免。
    把逐条校验分出来的好处不只是分数：报错信息能精确到第几条哪一项。）
    """
    if not isinstance(item, dict):
        return [f"unvalidated[{index}] 必须是对象"]
    problems: list[str] = []
    extra = sorted(set(item) - UNVALIDATED_KEYS)
    if extra:
        problems.append(
            f"unvalidated[{index}] 带了不该有的键 {extra}：只许 code/fact，"
            "不给程度（给了就会被当成「轻微」放行）"
        )
    for key in sorted(UNVALIDATED_KEYS):
        if not str(item.get(key) or "").strip():
            problems.append(f"unvalidated[{index}].{key} 不能为空")
    return problems


def _check_verdict_facts_consistency(verdict: str, facts: list[dict[str, Any]]) -> list[str]:
    """裁决与未决事实的**同时成立性**：这是三态的全部意义所在。"""
    problems: list[str] = []
    if verdict == VERDICT_NEEDS_VALIDATION and not facts:
        problems.append("needs_validation 却没给出任何 unvalidated：读者无从知道缺什么")
    if verdict == VERDICT_CONFIRMED and facts:
        problems.append("confirmed 却带 unvalidated：有未决事实就不是 confirmed")
    return problems


def validate(payload: dict[str, Any]) -> list[str]:
    """校验一份 JSON 载荷是否自洽。返回问题清单（空 = 通过）。

    给消费方一个可编程的判据：他们不必相信我们的文档，直接调它。
    """
    verdict = payload.get("verdict")
    if verdict not in VERDICTS:
        return [f"verdict 缺失或非法：{verdict!r}（只允许 {VERDICTS}）"]

    facts = payload.get("unvalidated")
    if facts is None:
        facts = []
    if not isinstance(facts, list):
        return ["unvalidated 必须是数组"]

    problems: list[str] = []
    for i, item in enumerate(facts):
        problems.extend(_check_fact_item(i, item))

    if payload.get("ok") is not (verdict == VERDICT_CONFIRMED):
        problems.append(
            f"ok={payload.get('ok')!r} 与 verdict={verdict!r} 不自洽："
            "ok 必须严格等于「confirmed」（没核验成就不许说 ok）"
        )
    problems.extend(_check_verdict_facts_consistency(verdict, facts))
    return problems


def main(argv: list[str] | None = None) -> int:
    """`python -m pm.verdict <payload.json>`：校验别的程序产出的裁决载荷。

    退出码：0 = 自洽，1 = 有问题（逐条打在 stdout），2 = 用法/读文件错。
    """
    import sys
    from pathlib import Path

    # 必须早于任何打印：中文 Windows 控制台是 GBK，本模块的输出全是中文，
    # 不重配置就会在**报错路径上**崩成 UnicodeEncodeError（一个校验器自己崩掉，
    # 比不校验更糟）。本仓对这条有专门门禁（tests/test_console_gbk_entry.py 要求
    # 交付面的 `__main__` 入口统一走这个 helper，不许各写一遍）。
    from .bootstrap import ensure_utf8_stdio

    ensure_utf8_stdio()

    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("用法：python -m pm.verdict <payload.json>（或 - 从 stdin 读）", file=sys.stderr)
        return 2
    try:
        if args[0] == "-":
            # stdin 可能是别处管道过来的 UTF-8 字节；Windows 控制台的 stdin 默认按本地码页解码，
            # 中文会在这里就烂掉（本仓已有同类事故：tests/test_subprocess_encoding.py）。
            raw = sys.stdin.buffer.read().decode("utf-8-sig")
        else:
            raw = Path(args[0]).read_text(encoding="utf-8-sig")
        payload = json.loads(raw)
    except (OSError, ValueError) as e:
        print(f"读不懂这份载荷：{e}", file=sys.stderr)
        return 2
    if not isinstance(payload, dict):
        print("载荷必须是 JSON 对象（本命令校验的是 CLI --json 的单对象出口）", file=sys.stderr)
        return 2

    problems = validate(payload)
    if problems:
        for p in problems:
            # ⚠️ 不用 ✅/❌ 这类符号：中文 Windows 的控制台是 GBK，印它们会
            # UnicodeEncodeError —— 一个校验器自己在报错时崩掉，比不校验更糟。
            print(f"[不通过] {p}")
        return 1
    print(f"[通过] 裁决自洽：verdict={payload['verdict']}，ok={payload['ok']}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
