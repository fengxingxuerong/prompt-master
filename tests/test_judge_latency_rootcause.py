"""评委慢的根因守卫（§三十·十六）

⚠️ 判据设计记录（这里换过三次判据，每次都是被真实数据打脸后改的）：

  v1「所有角色速率落在 [6,13]」  → 被打脸：clarifier(n=1) 15.9
  v2「放宽到 [6,21]」            → 又被打脸：clarifier(n=1) 43.3
  v3「n>=8 才参与」              → 又被打脸：target(n=16) 25.3
  v4（现版）「轮内相对比较」      → 不设绝对界

绝对界是错的判据：速率**跨时段**分档（§三十·十六·七实测），整轮都慢的样本
会把任何绝对界打脸，而放宽界去迁就离群点等于放弃检测能力。

真正稳定的判据是**关系**：
  · 轮内各角色速率**同档**（max/min 小）⇒ 生成速率与角色无关
  · `evaluator_b` 会**席位特定劣化**（同轮里只有它慢）
这两条都不依赖端点在某个时段的绝对快慢。

**放宽界迁就离群点**与**收紧界打到假阳性**都是错的，本文件是这个教训的现场。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

LOGS = Path(__file__).resolve().parents[1] / "logs"

# 判速率所需的最小调用数：n=1 被首 token 预热/端点抖动/偶发长尾主导
# （实测 clarifier n=1 在 8.4~43.3 之间横跳，那不是速率变了，是样本不够）
MIN_CALLS = 8

# 截断信号：出现时「秒/千输出token」不再是速率指标（§三十·十六·四）
# 实测 run_44086a49c100 因 reasoning 耗尽 max_tokens 截断，该指标高达 1248
TRUNCATION_MARKERS = ("reasoning 耗尽 max_tokens", "length limit was reached")

# 已查明为「席位特定劣化」的轮次（evaluator_b 端点异常，不是产品逻辑）。
# 这三轮不参与「轮内同档」判定，但由 test_second_seat_degrades 单独覆盖。
KNOWN_BAD_SEATS = {"9950147e2e40", "e76f9acfefab", "b92609933122"}

# 轮内离散度上限。实测 9 个正常轮次落在 1.36~1.70（最大 1.70），
# 取 2.5 留余量；真正的回归（某角色被系统性限速）会远超这个倍数。
WITHIN_RUN_SPREAD_MAX = 2.5

# 同模型（evaluator 与 comparator 同为 deepseek-v4-flash）的速率比上限。
# 实测 9 轮：中位 1.12，最大 1.70（那三轮 evaluator 7.5~7.8 比 comparator 12~13 快，
# 方向还是「同档」）。取 2.0 留余量。系统性限速会是 10 倍量级。
SAME_MODEL_RATIO_MAX = 2.0


def _runs() -> list[tuple[str, dict]]:
    out: list[tuple[str, dict]] = []
    if not LOGS.exists():
        return out
    for p in LOGS.glob("run_*.json"):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if len(d.get("test_runs") or []) >= 8 and (d.get("llm_usage") or {}):
            out.append((p.stem.replace("run_", ""), d))
    return out


def _truncated(d: dict) -> bool:
    blob = " ".join(str(x) for x in (d.get("errors") or []))
    return any(m in blob for m in TRUNCATION_MARKERS)


def _rates(d: dict) -> dict[str, float]:
    """各角色的秒/千输出 token（仅够大的未截断样本）。"""
    out: dict[str, float] = {}
    for role, v in (d.get("llm_usage") or {}).items():
        c = int(v.get("calls") or 0)
        o = int(v.get("output_tokens") or 0)
        lat = int(v.get("latency_ms") or 0) / 1000
        if c < MIN_CALLS or o < 200 or lat / c < 3:
            continue
        out[role] = lat / c / (o / c / 1000)
    return out


def test_generation_rate_is_role_independent() -> None:
    """钉住核心事实：同一轮内，各角色的生成速率同档。

    这是 §三十·十六 全部结论的地基。若端点真的给评委模型限速，
    轮内 evaluator 会显著高于 target ⇒ 这条变红。

    用**轮内相对比较**而不是绝对界：速率跨时段分档，绝对界必然被真实波动打脸。
    """
    checked = 0
    for rid, d in _runs():
        if rid in KNOWN_BAD_SEATS or _truncated(d):
            continue
        rs = _rates(d)
        if len(rs) < 2:
            continue
        spread = max(rs.values()) / min(rs.values())
        checked += 1
        assert spread <= WITHIN_RUN_SPREAD_MAX, (
            f"run_{rid} 轮内速率 max/min = {spread:.1f} 越界 "
            f"{WITHIN_RUN_SPREAD_MAX}（{rs}）——某角色可能被端点限速，"
            "§三十·十六 的归因要重查"
        )
    if not checked:
        pytest.skip("本机没有足够归档")


def test_same_model_different_role_same_rate() -> None:
    """comparator 与 evaluator 同为 deepseek-v4-flash，速率必须同档。

    这条否掉"端点对评委模型调度不同"那个候选（C）。
    """
    checked = 0
    for rid, d in _runs():
        if rid in KNOWN_BAD_SEATS or _truncated(d):
            continue
        rs = _rates(d)
        if "evaluator" not in rs or "comparator" not in rs:
            continue
        checked += 1
        ratio = max(rs["evaluator"], rs["comparator"]) / min(rs["evaluator"], rs["comparator"])
        assert ratio <= SAME_MODEL_RATIO_MAX, (
            f"run_{rid}: 同为 deepseek-v4-flash，evaluator {rs['evaluator']:.1f} 与 "
            f"comparator {rs['comparator']:.1f} 差 {ratio:.1f} 倍——"
            "『同模型同速率』这个前提变了，归因要重查"
        )
    if not checked:
        pytest.skip("本机没有可比归档")


def test_judge_writes_far_more_than_target() -> None:
    """钉住"评委写得多"这个根因的另一半。"""
    ratios = []
    for rid, d in _runs():
        if rid in KNOWN_BAD_SEATS or _truncated(d):
            continue
        u = d.get("llm_usage") or {}
        e, t = u.get("evaluator"), u.get("target")
        if not e or not t:
            continue
        ec, tc = int(e.get("calls") or 0), int(t.get("calls") or 0)
        if not ec or not tc:
            continue
        eo = int(e.get("output_tokens") or 0) / ec
        to = int(t.get("output_tokens") or 0) / tc
        if to:
            ratios.append(eo / to)
    if not ratios:
        pytest.skip("没有可比角色")
    med = sorted(ratios)[len(ratios) // 2]
    assert med >= 3.0, (
        f"评委输出 token 只有 target 的 {med:.1f} 倍——『输出过多』这个根因不再成立，归因要重查"
    )


def test_second_seat_degrades() -> None:
    """新发现：`evaluator_b` 会**席位特定劣化**——同轮里只有它慢。

    这不是"端点整体变慢"：e76f9acfefab 里 target 24.1 而 evaluator 只有 14.4
    （评委是快的），evaluator_b 却是 147.2。

    这条同时是 §三十·十三「evaluator_b 至今没跑通过一轮完整双评委」的最可能真因。
    """
    bad: list[tuple[str, float]] = []
    for rid in KNOWN_BAD_SEATS:
        p = LOGS / f"run_{rid}.json"
        if not p.exists():
            pytest.skip("已知劣化轮的归档不在本机")
        rs = _rates(json.loads(p.read_text(encoding="utf-8")))
        if "evaluator_b" not in rs:
            continue
        others = [v for k, v in rs.items() if k != "evaluator_b"]
        if not others:
            continue
        bad.append((rid, rs["evaluator_b"] / min(others)))
    if not bad:
        pytest.skip("已知劣化轮里没有 evaluator_b 的有效样本")
    worst = max(r for _, r in bad)
    assert worst >= 5.0, (
        f"已知劣化轮的 evaluator_b 只慢到 {worst:.1f} 倍——"
        "若降到个位数，说明那个模型已经恢复，"
        "§三十·十七『席位特定劣化』的前提要重查（不是坏事，但要重跑验证）"
    )


def test_second_seat_degradation_is_model_not_endpoint() -> None:
    """钉住：两个评委走**同一个端点**，差别只在模型。

    §三十·十七 我两次把劣化归给"glm-5.2 那个端点"，都错了。
    `.env` 里 `PM_EVALUATOR_B_BASE_URL` 与 `PM_BASE_URL` 都是
    `https://token.sensenova.cn/v1` —— 同端点，不同模型。
    ⇒ 该换的是二号位的**模型**，不是端点。

    ⚠️ 这条**必须读 .env 原文**，不能用 build_config：
    CI 把 `PM_*_BASE_URL` 全覆盖成同一个死地址（127.0.0.1:9），
    用 build_config 的话「同端点」恒真 ⇒ 守卫变装饰品。
    （这个坑踩过一次：改成不同端点跑，守卫仍然绿。）
    """
    dotenv = Path(__file__).resolve().parents[1] / ".env"
    if not dotenv.exists():
        pytest.skip("本机没有 .env")
    raw: dict[str, str] = {}
    for line in dotenv.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            raw[k.strip()] = v.strip()

    ev = raw.get("PM_BASE_URL", "")
    evb = raw.get("PM_EVALUATOR_B_BASE_URL", "")
    if not ev or not evb:
        pytest.skip(".env 里没有显式写这两个 BASE_URL，判据不适用")
    assert ev == evb, (
        f"两个评委现在走不同端点（{ev} vs {evb}）——"
        "若这是有意的（例如为了端点隔离），请把 §三十·十七 的诊断改写成"
        "『端点差异』，并同步更新这条守卫的意图"
    )
    assert raw.get("PM_EVALUATOR_MODEL") != raw.get("PM_EVALUATOR_B_MODEL"), (
        "两个评委现在用**同一个模型**——那『双评委』其实是同模型两次采样，"
        "制衡机制不成立。这是另一条更严重的问题，本条只负责提醒"
    )


def test_evaluator_max_tokens_is_not_lowered() -> None:
    """记录现状：上限 32000 没有给模型收口信号。

    ⚠️ 这是**现状记录**，不是推荐值。调低它有真实风险：
    `pm/llm.py:838` 明写「思考型模型把 reasoning 计入 max_tokens，
    触达上限 → 正文额度归零」，2026-09-14 的事故就是
    `evaluator max_tokens=6000` → **33% 的调用正文被截成空**。
    ⇒ §三十·十六 **不调**这个值，本条只是防止它被悄悄改掉。
    """
    from pm.llm import build_config

    cfg = build_config("evaluator")
    assert cfg.max_tokens >= 32000, (
        f"evaluator max_tokens 现在是 {cfg.max_tokens}——"
        "若被调小，先确认不会重现 2026-09-14 的正文截断事故"
    )
