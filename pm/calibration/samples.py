"""锚点加载、校验与人工分出处。

2026-10-19 从 `pm/calibration.py` 拆出：加载/出处识别只跟样本文件打交道，
与统计、渲染、CLI 无依赖。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .paths import ANCHOR_DIR, EXAMPLE_SAMPLES

# ---- 人工分的**出处**（2026-10-03 补）----
# 48 条候选里有 25 条不是产品所有者打的，是 AI 按所有者已确认判例外推的（GLM-5.3，
# 与评委 B glm-5.2 同族）。这件事此前只写在提交说明和 score_form.csv 的备注栏里，
# 锚点 JSON 里**没有机读位** ⇒ 报告那句"64 条人工锚点、bias +3.09"拆不出严格人工那一半。
# 同源披露本来就是这个项目的习惯（评委同源要检测要报警），锚点侧同源却是口头承认——补平。
HUMAN_SOURCE_OWNER = "owner"


HUMAN_SOURCE_AI_PROXY = "ai_proxy"


# 仓库自带示例锚点：分数是演示编造的，只用于"第一次把流程跑通"。
# 2026-10-19 之前这 6 条在 samples.json 里标的是 `owner` —— 于是一批编出来的分数
# 冒充"所有者亲判"进了 bias 的严格人工那一半。现在它们有自己的桶，不再冒充。
HUMAN_SOURCE_DEMO = "demo"


HUMAN_SOURCE_UNKNOWN = "unknown"


def classify_human_source(item: dict[str, Any]) -> str:
    """单条锚点的人工分出处。缺字段一律 `unknown`，**不当成 owner**——
    把"没标注"读成"人打的"正好把这个洞重新盖回去。"""
    prov = item.get("provenance")
    raw = ""
    if isinstance(prov, dict):
        raw = str(prov.get("human_score_source") or "")
    elif isinstance(prov, str):
        raw = prov
    if not raw:
        return HUMAN_SOURCE_UNKNOWN
    low = raw.lower()
    if HUMAN_SOURCE_DEMO in low:
        return HUMAN_SOURCE_DEMO
    if HUMAN_SOURCE_OWNER in low or low.startswith("owner"):
        return HUMAN_SOURCE_OWNER
    if HUMAN_SOURCE_AI_PROXY in low or "代判" in raw:
        return HUMAN_SOURCE_AI_PROXY
    return HUMAN_SOURCE_UNKNOWN


def render_human_source_split(pooled: dict[str, Any]) -> list[str]:
    """把合并读数按人工分出处拆开写。只有一组有数据时不啰嗦（那说明没得拆）。"""
    groups = pooled.get("by_human_source") or {}
    known = {k: v for k, v in groups.items() if v.get("n_pairs")}
    if len(known) < 2:
        return []
    label = {
        HUMAN_SOURCE_OWNER: "所有者亲判",
        HUMAN_SOURCE_AI_PROXY: "AI 代判（与评委 B 同族）",
        HUMAN_SOURCE_DEMO: "仓库示例（分数为编造，不可引用）",
        HUMAN_SOURCE_UNKNOWN: "未标注出处",
    }
    out = ["", "- 人工分出处拆分（同一批配对重算，零调用）："]
    for src in (
        HUMAN_SOURCE_OWNER,
        HUMAN_SOURCE_AI_PROXY,
        HUMAN_SOURCE_DEMO,
        HUMAN_SOURCE_UNKNOWN,
    ):
        g = known.get(src)
        if not g:
            continue
        bias = g.get("bias")
        bias_s = f"{bias:+.2f}" if isinstance(bias, (int, float)) else "-"
        out.append(
            f"  - {label.get(src, src)}：{g['n_anchors']} 条锚点 / {g['n_pairs']} 条配对，bias {bias_s}"
        )
    ai = known.get(HUMAN_SOURCE_AI_PROXY, {}).get("n_anchors", 0)
    # 分母只算"真的人工分"两桶：示例锚点的分数是编的、未标注的连来路都不知道，
    # 把它们并进分母会把「代判占比」稀释成一个更小的数，警示就被自己调轻了。
    tot = sum(
        g.get("n_anchors", 0)
        for k, g in known.items()
        if k in (HUMAN_SOURCE_OWNER, HUMAN_SOURCE_AI_PROXY)
    )
    if ai and tot and ai / tot >= 0.5:
        out.append(
            f"  - ⚠️ 合并 bias 里 {ai}/{tot} 条锚点的人工分来自 AI 代判 —— 那一部分量的是"
            "**家族一致性**，不是与人一致性；引用整句 bias 时必须带上这一句。"
        )
    return out


def example_anchor_ids() -> set[str]:
    """仓库自带**示例**锚点集的 id 集合（`samples.example.json`）。

    为什么要有这个：示例锚点是给"第一次跑通流程"用的，人工分是**编的**。
    拿它跑出来的校准会被写进活账本（`logs/judge_calibration_history.json`），
    之后 `calibrate --aggregate` 照常把它并进合并读数 —— 实测活账本里唯一那条
    带数字的校准行（2026-10-10，n=11）里 **6 条是示例锚点**（`demo-good`、
    `demo-hallucinated`、`demo-vague`、`anchor-fab-trend`、`anchor-fab-attribution`、
    `anchor-mixed`，与示例文件逐字同名），剩下 5 条才是 `real-*`。
    而 README §三 引用的 MAE/bias 正是从这条账本重算的 ⇒ "量具准不准"这句结论
    有一半压在编出来的锚点上，且读的人无从得知。
    这与 `test_history_mock_filter.py` 那次是同族：mock 臂不许混进 Δ 统计，
    示例锚点也不许**静默**混进校准聚合。
    """
    try:
        data = json.loads(EXAMPLE_SAMPLES.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    rows = (
        data
        if isinstance(data, list)
        else (data.get("samples") if isinstance(data, dict) else None)
    )
    if not isinstance(rows, list):
        return set()
    return {str(r["id"]) for r in rows if isinstance(r, dict) and r.get("id") is not None}


def count_example_anchors(pairs: list[dict[str, Any]]) -> int:
    """这批配对里有几条落在示例锚点集上（0 = 全是真锚点）。"""
    ex = example_anchor_ids()
    if not ex:
        return 0
    return sum(1 for p in pairs if str(p.get("id")) in ex)


def render_example_anchor_disclosure(pooled: dict[str, Any]) -> list[str]:
    """合并读数里掺了示例锚点时必须当场说清，一条都不掺就别啰嗦。"""
    n = int(pooled.get("example_anchors") or 0)
    if not n:
        return []
    tot = int(pooled.get("n_anchors") or 0)
    return [
        "",
        f"- ⚠️ 合并读数里有 **{n}/{tot} 条锚点来自仓库自带的示例集**"
        "（`judge_calibration/samples.example.json`）——那些人工分是编的，用来跑通流程，"
        "不是所有者的判例。引用这条 MAE/bias 时必须带上这一句；想拿能站住的读数，"
        "得先用真锚点重跑一轮 `run.py calibrate`。",
    ]


def load_human_sources(anchor_dir: Path | None = None) -> dict[str, str]:
    """扫锚点目录，返回 `{锚点 id: owner|ai_proxy|unknown}`。

    跨轮聚合只消费账本里的 id，所以这里要把所有样本文件并起来看；
    同一 id 在多个文件里出现时以**先出现的非 unknown** 为准（确定性顺序，不靠 dict 迭代随机）。
    """
    base = anchor_dir or ANCHOR_DIR
    out: dict[str, str] = {}
    try:
        paths = sorted(p for p in base.glob("samples*.json"))
    except OSError:
        return out
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        rows = (
            data
            if isinstance(data, list)
            else (data.get("samples") if isinstance(data, dict) else None)
        )
        if not isinstance(rows, list):
            continue
        for item in rows:
            if not isinstance(item, dict) or item.get("id") is None:
                continue
            sid = str(item["id"])
            src = classify_human_source(item)
            # demo 是**强证据**：同一个 id 只要在任一锚点文件里被标成示例，
            # 它就不可能是人工判例。原先只按"先出现的非 unknown 为准"，
            # 而字典序上 samples.candidates.json 排在 samples.example.json 之前
            # ⇒ 候选集里那句 owner 会把示例标记盖掉（实测：11 条里 6 条示例
            # 只有 1 条被认出来，另 5 条冒充了"所有者亲判"）。
            if out.get(sid, HUMAN_SOURCE_UNKNOWN) == HUMAN_SOURCE_UNKNOWN or (
                src == HUMAN_SOURCE_DEMO
            ):
                out[sid] = src
    return out


def split_by_human_source(
    pairs: list[dict[str, Any]], sources: dict[str, str]
) -> dict[str, dict[str, Any]]:
    """把逐锚点配对按人工分出处分组，各算 bias/MAE。纯算术，零调用。"""
    groups: dict[str, dict[str, Any]] = {}
    for p in pairs:
        src = sources.get(str(p.get("id")), HUMAN_SOURCE_UNKNOWN)
        g = groups.setdefault(src, {"pairs": [], "anchors": set()})
        try:
            human = float(p["human"])
            judge = float(p["judge"])
        except (KeyError, TypeError, ValueError):
            continue
        g["pairs"].append(judge - human)
        g["anchors"].add(str(p.get("id")))
    out: dict[str, dict[str, Any]] = {}
    for src, g in sorted(groups.items()):
        deltas = g["pairs"]
        n = len(deltas)
        out[src] = {
            "n_anchors": len(g["anchors"]),
            "n_pairs": n,
            "bias": round(sum(deltas) / n, 3) if n else None,
            "mae": round(sum(abs(d) for d in deltas) / n, 3) if n else None,
        }
    return out


class Anchors(list[dict[str, Any]]):
    """`load_samples` 的返回值：只装**可校准**条目，未确认的挂在 `.pending` 上。

    为什么用 list 子类而不是改成返回二元组：调用方（`run.py calibrate` 与本文件 main）
    以及它们的回归测试都按 list 消费这个返回值，改签名会连带漂移指纹的语义。
    """

    pending: list[dict[str, Any]]

    def __new__(cls, iterable: Any = (), *, pending: Any = ()) -> Anchors:
        # 注意：`super().__new__(cls, iterable)` 里那个 iterable 是被 object.__new__ 丢掉的
        # （实测：只调 __new__ 得到的实例是空 list）。真正填充发生在随后自动执行的
        # list.__init__(obj, iterable) —— 所以这里不传它，语义一字不变，
        # 但少了 mypy 的"参数过多"假错。参数本身要留着：它是调用方传进来的样本。
        obj = super().__new__(cls)
        obj.pending = list(pending)
        return obj


# --------------------------------------------------------------------------
# 样本加载
# --------------------------------------------------------------------------
def _confirmed(item: dict[str, Any]) -> bool:
    """这条锚点算不算"人工确认过"。

    缺省 True 是刻意的向后兼容：存量 `samples.json` 那 11 条本来就是人工分的，
    这个字段引入之前它们没有 `confirmed`，判成未确认等于把现有校准一次性作废。
    新采集的候选由 `harvest_anchors.py` 显式写 `confirmed: false`。
    """
    flag = item.get("confirmed", True)
    if isinstance(flag, str):
        return flag.strip().lower() in {"1", "true", "yes"}
    return bool(flag)


def load_samples(path: Path) -> Anchors:
    """加载并校验锚点样本。纯注释项（只有 _comment 等说明键）跳过；真实条目缺字段直接报错。

    注意跳过条件：只跳过「除说明键外什么都没有」的条目。
    像 {"id": "a"} 这种带真实键但缺关键字段的条目是**写错的样本**，
    静默跳过会让用户以为它参与了校准——必须报错。

    `confirmed: false` 的条目进 `.pending` 而不是返回值：人工分没落地之前，候选分数
    与被校对象同源，让它进统计就是拿被校对象当标准答案。这类条目允许 `human_score: null`
    （等人来填）；已确认条目填 null 属写错，报错。
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("样本文件顶层必须是数组")
    samples: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    required = ("id", "original_task", "prompt", "test_input", "test_output", "human_score")
    skip_keys = {"_comment"}
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            raise ValueError(f"第 {i + 1} 条样本必须是对象")
        # 只有说明键、没有任何真实字段 → 注释项，跳过
        if set(item.keys()) <= skip_keys:
            continue
        missing = [k for k in required if k not in item]
        if missing:
            raise ValueError(f"第 {i + 1} 条样本缺字段：{missing}")
        if not _confirmed(item):
            pending.append(item)
            continue
        if item["human_score"] is None:
            raise ValueError(
                f"第 {i + 1} 条样本（{item.get('id')}）confirmed=true 但 human_score 为 null"
            )
        score = float(item["human_score"])
        if not 1.0 <= score <= 10.0:
            raise ValueError(f"第 {i + 1} 条样本 human_score={score} 超出 1-10")
        samples.append(item)
    ids = [str(s["id"]) for s in samples] + [str(s["id"]) for s in pending]
    if len(ids) != len(set(ids)):
        raise ValueError("样本 id 重复")
    return Anchors(samples, pending=pending)
