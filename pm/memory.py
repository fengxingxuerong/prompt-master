"""提示词资产记忆：从运行历史中检索与当前任务相似的达标提示词。

设计立场：
- **零拷贝**：资产的事实来源就是 logs/run_*.json，不另建存储（与 history/library 同族）；
- **只借鉴成功**：只推荐 passed 且（有基线时）Δ≥0 的运行——失败的借鉴只会带偏；
- **参与生成而非替代生成**：命中资产以"参考"身份注入 OPTIMIZER 的 context，
  由提示词注入块明确标注"可借鉴结构，不是标准答案"；PM_MEMORY_HINT=0 一键关闭。
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# 注入生成链路的资产正文上限：参考块太长会挤占 Optimizer 的注意力
_ASSET_CLIP = 1200
# 记忆检索默认只看最近 N 个运行记录（PM_MEMORY_SCAN_LIMIT 可覆盖）。
# 200 是"不拖慢主管道"的经验值，不是"历史只有 200 条"——超出部分必须告警披露。
_SCAN_LIMIT_DEFAULT = 200
# 相似度门槛：低于它说明历史里没有真正的同类任务，硬塞参考只会带偏。
# 0.10 是首版的教训（实测）：0.275 的"语义族邻居"（分诊 vs 情绪分类，都是
# 分类+提取）不足以保证结构可借鉴——OPTIMIZER 照抄了错误任务语义，v0 被评委
# 一致打 1.19（A/B：同任务无参考的修订轮 9.65）。语义族相似 ≠ 结构可借鉴，
# 门槛提到 0.35，宁可不注入也不冒带偏的风险。
_SIM_MIN = 0.35

_TEMPLATE_NOISE = ("帮我写", "写一个", "写个", "prompt", "提示词", "让ai", "让 ai")


def _text_bigrams(text: str) -> set[str]:
    t = "".join(text.split())
    if len(t) < 2:
        return {t} if t else set()
    return {t[i : i + 2] for i in range(len(t) - 1)}


def _dice(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return 2 * len(a & b) / (len(a) + len(b))


def _sim_grams(text: str) -> set[str]:
    cleaned = (text or "").lower()
    for noise in _TEMPLATE_NOISE:
        cleaned = cleaned.replace(noise, "")
    return _text_bigrams(cleaned)


def _scan_limit() -> int:
    """本次检索最多扫最近多少个 run_*.json（PM_MEMORY_SCAN_LIMIT，默认 200）。

    非法值回退默认并告警，而不是当成 0：这个数决定"历史有多少被看得见"，
    把它读成 0 等于静默关掉记忆层，与"历史里没有同类任务"长得一模一样。
    """
    raw = (os.getenv("PM_MEMORY_SCAN_LIMIT") or "").strip()
    if not raw:
        return _SCAN_LIMIT_DEFAULT
    try:
        val = int(raw)
    except ValueError:
        logger.warning("PM_MEMORY_SCAN_LIMIT=%r 不是整数，回退默认 %d", raw, _SCAN_LIMIT_DEFAULT)
        return _SCAN_LIMIT_DEFAULT
    if val < 1:
        logger.warning("PM_MEMORY_SCAN_LIMIT=%d 小于 1，回退默认 %d", val, _SCAN_LIMIT_DEFAULT)
        return _SCAN_LIMIT_DEFAULT
    return val


def find_similar_asset(
    task: str, exclude_run_id: str | None = None, log_dir: Path | None = None
) -> dict[str, Any] | None:
    """在运行历史里找与新任务最相似的达标资产；没有就返回 None。

    筛选口径（缺一不可）：
    - status == passed（未达标交付的提示词不做参考）；
    - 有基线时 Δ≥0（比不优化还差的"达标"没有借鉴价值）；
    - 演示模式（trace 里有 fake）与当前自身 run 排除；
    - Dice(task bigram) ≥ _SIM_MIN。
    """
    if not (task or "").strip():
        return None
    log_dir = log_dir or (Path(__file__).resolve().parent.parent / "logs")
    if os.getenv("PM_MEMORY_HINT", "1").strip().lower() in {"0", "false", "no", "off"}:
        return None

    new_grams = _sim_grams(task)
    if not new_grams:
        return None

    best: tuple[float, dict[str, Any]] | None = None
    files = sorted(log_dir.glob("run_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    # 扫描上限：记忆检索不该拖慢主管道，但"扫不动了"必须是看得见的信息而不是静默失忆。
    # 曾经这里是硬编码 200 且无任何输出：logs/ 涨到 1000+ 个 run 文件后，八成历史
    # 对检索不可见，表现只是"这次没找到参考"——和真的没有同类任务无法区分。
    limit = _scan_limit()
    if limit < len(files):
        logger.warning(
            "记忆检索被扫描上限截断：只看了最近 %d 个运行记录（共 %d 个，%.0f%% 未参与检索）；"
            "需要更大范围请调 PM_MEMORY_SCAN_LIMIT",
            limit,
            len(files),
            100 * (len(files) - limit) / len(files),
        )
    for f in files[:limit]:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if d.get("status") != "passed" or d.get("run_id") == exclude_run_id:
            continue
        channels = {str(e.get("channel")) for e in (d.get("trace") or [])}
        if "fake" in channels:
            continue
        agg = d.get("aggregate") or {}
        base = (d.get("baseline_aggregate") or {}).get("avg_score")
        if isinstance(base, (int, float)) and isinstance(agg.get("avg_score"), (int, float)):
            if agg["avg_score"] - base < 0:
                continue
        prompt = str(d.get("prompt") or "")
        if not prompt.strip():
            continue
        score = _dice(new_grams, _sim_grams(str(d.get("task") or "")))
        if score < _SIM_MIN:
            continue
        if best is None or score > best[0]:
            best = (
                score,
                {
                    "run_id": d.get("run_id"),
                    "similarity": round(score, 3),
                    "avg_score": agg.get("avg_score"),
                    "task": str(d.get("task") or ""),
                    "prompt_excerpt": prompt[:_ASSET_CLIP],
                },
            )
    return best[1] if best else None


def render_memory_hint(asset: dict[str, Any]) -> str:
    """把命中资产渲染成注入 OPTIMIZER context 的参考块。"""
    return (
        "\n\n<类似任务的历史高分提示词（供结构参考，不是标准答案）>\n"
        "使用纪律：参考只用于借鉴**格式骨架与约束写法**（如输出 JSON 骨架、边界分支、"
        "注入免疫约束的表达方式）。若参考的任务目标/输出结构与当前任务不一致，"
        "**以当前任务为准，禁止照搬参考的任务语义**（实测：照抄相近但不同任务的"
        "结构会让输出答非所问，首轮评分跌至 1 分档）。\n"
        f"原任务：{asset['task']}\n"
        f"该提示词实测平均分：{asset.get('avg_score')}\n"
        f"{asset['prompt_excerpt']}\n"
        "</类似任务的历史高分提示词>"
    )
