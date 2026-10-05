"""两本读者必须对同一本账本给同一个答案（§二十九）。

## 缺陷

`calibration.aggregate_history` 按 `model` 过滤，只合并**当前仪表**的轮次；
而 `_measured_jitter` 曾经只按 `judge` 读，把账本里**任何**一条带
`rep_range_mean` 的记录都当成当前这把评委的实测抖动。

账本现存那唯一一条恰好是**没有 `model` 字段**的历史记录，于是同一个 0.54：

| 读者 | 结论 |
| --- | --- |
| `aggregate_history`（校准自己的跨轮聚合） | `None` —— 不是当前仪表，不参与聚合 |
| `_measured_jitter`（噪声带） | `measured` —— 当成实测，计入噪声带与 `ci_lower` |

**两本读者给出相反答案，而噪声带是产品侧唯一会引用的那个。**

## 为什么这是 §十八 的同一类错误

§十八 挡的是"**配置常数**冒充实测"；这次是"**另一把尺子**的实测冒充当前尺子的实测"。
两者都是：拿一个不属于当前仪表的数，去参与当前仪表的不确定度计算。

换型（换评委模型 / 换 rubric）之后旧记录留在账本是**正常的**，不该删——
该做的是不把它们算进当前仪表的噪声带。

## 判据

1. 仪表身份不匹配的条目一律不进噪声带（`model` 缺失同样不算：无法证明就不许当证据）；
2. 本模块与 `calibration.aggregate_history` 对"这条算不算当前仪表"必须给**同一个**答案；
3. 身份读不到时退化成"不算"，绝不允许"读不到就当算"。
"""

from __future__ import annotations

import json

import pytest
from pm import schemas
from pm.calibration import aggregate_history


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    for seat in ("PM_JUDGES", "PM_JUDGE_DISAGREEMENT"):
        monkeypatch.delenv(seat, raising=False)
    monkeypatch.setenv("PM_LOG_DIR", str(tmp_path))
    monkeypatch.setenv("PM_JUDGE_DISAGREEMENT", "2.0")
    monkeypatch.setattr(schemas, "JUDGE_JITTER", 0.0)
    schemas._measured_jitter.cache_clear()


def _write(entries, tmp_path) -> None:
    (tmp_path / "judge_calibration_history.json").write_text(
        json.dumps(entries, ensure_ascii=False), encoding="utf-8"
    )
    schemas._measured_jitter.cache_clear()


def _model(seat: str = "evaluator") -> str:
    return schemas.current_judge_model(seat)


# ---------------------------------------------------------------------------
# 判据一：身份不匹配（含缺失）一律不进噪声带
# ---------------------------------------------------------------------------
def test_entry_without_model_is_not_current_instrument(tmp_path) -> None:
    """现存账本那条 09-29 记录的形状：没有 `model`。"""
    _write([{"judge": "evaluator", "rep_range_mean": 0.54, "ts": "2026-09-29"}], tmp_path)
    prov = schemas.jitter_provenance("evaluator")
    assert prov.jitter == 0.0, "缺 model ⇒ 无法证明是当前尺子量的 ⇒ 不许当实测"
    assert prov.measured is False
    assert schemas._measured_jitter_band() == 0.0, "也不许进噪声带"


def test_entry_of_another_model_is_not_current_instrument(tmp_path) -> None:
    """换型前的旧记录留着是对的，但不得算进新尺子的噪声带。"""
    _write(
        [
            {
                "judge": "evaluator",
                "rep_range_mean": 0.54,
                "ts": "2026-09-29",
                "model": "某个-换型前的旧型号",
            }
        ],
        tmp_path,
    )
    assert schemas.jitter_provenance("evaluator").measured is False
    assert schemas._measured_jitter_band() == 0.0


def test_matching_model_is_used(tmp_path) -> None:
    """同型号就要用 —— 过滤不是为了把带清零。"""
    _write(
        [
            {
                "judge": "evaluator",
                "rep_range_mean": 0.54,
                "ts": "2026-10-05",
                "model": _model("evaluator"),
            }
        ],
        tmp_path,
    )
    assert schemas.jitter_provenance("evaluator").jitter == 0.54
    assert schemas._measured_jitter_band() == 0.54


# ---------------------------------------------------------------------------
# 判据一之二：`model=None`（说不清当前是谁）必须一条都不算
# ---------------------------------------------------------------------------
def test_none_model_reads_nothing(tmp_path) -> None:
    """这是**后门档**：判据写成 `!= (model or "")` 时，`model=None` 会让
    缺 model 的条目判成"匹配"——刚修掉的 bug 从参数默认值绕了回来。

    本轮第一版实现正是死在这里，所以必须有专门一条盯着它。
    """
    _write(
        [
            {
                "judge": "evaluator",
                "rep_range_mean": 0.54,
                "ts": "2026-09-29",
            }
        ],
        tmp_path,
    )
    assert schemas._measured_jitter(str(tmp_path / "judge_calibration_history.json")) == {}, (
        "说不清当前仪表是谁时，必须一条都不算 —— 不许因为参数默认值而不过滤"
    )
    assert schemas._measured_jitter(str(tmp_path / "judge_calibration_history.json"), "") == {}


# ---------------------------------------------------------------------------
# 判据二：与 calibration 自己的聚合口径一致（本轮缺陷的正身）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("stamped", [True, False])
def test_two_ledger_readers_agree_on_what_counts(tmp_path, stamped: bool) -> None:
    """同一本账本，两本读者对"这条算不算当前仪表"必须同答。

    钉在**两个读者各自的公开读数**上，不钉内部实现：任何一边改了口径，
    这条都会红 —— 而那正是本轮缺陷的形状。
    """
    entry = {
        "judge": "evaluator",
        "rep_range_mean": 0.54,
        "ts": "2026-10-05",
        "mode": "impression",
        "n": 10,
        # `items` 的键名必须与 `calibration._save_entry` 落盘的一致（`human`/`judge`，
        # 来自 analysis["pairs"]）：写成 human_score/model_score 会让
        # aggregate_history 认为"这轮没有可用的逐锚点明细"而返回 None ——
        # 那样这条测的就不是口径分歧，而是我自己造了个坏条目。
        "items": [{"id": "a", "human": 8, "judge": 8.3}],
    }
    if stamped:
        entry["model"] = _model("evaluator")
    _write([entry], tmp_path)

    agg = aggregate_history(
        json.loads((tmp_path / "judge_calibration_history.json").read_text(encoding="utf-8")),
        mode="impression",
        judge="evaluator",
        model=_model("evaluator"),
        rubric=None,
    )
    counted_by_calibration = agg is not None
    counted_by_band = schemas._measured_jitter_band() > 0

    assert counted_by_calibration == counted_by_band, (
        "两本读者对同一本账本给了相反答案："
        f"校准聚合={'算' if counted_by_calibration else '不算'}、"
        f"噪声带={'算' if counted_by_band else '不算'}。"
        "其中噪声带是产品侧唯一会引用的那个。"
    )


def test_the_real_ledger_agrees_now() -> None:
    """仓库里那本真实账本：读完必须两本读者都说"不算"。

    这条是对**当前事实**的钉死，不依赖任何 tmp 数据。

    ⚠️ 它必须在 `PM_LOG_DIR` 被夹具改到 tmp **之前**取路径：这里要读的正是
    仓库 `logs/` 下那本真实账本，而 `seal_calibration_ledger` 把它隔离走了
    （那正是它的职责——测试不许写真实账本）。所以这里显式从仓库根解析路径，
    而不是用 `_jitter_ledger_path()`。
    """
    from pathlib import Path

    real = Path(__file__).resolve().parents[1] / "logs" / "judge_calibration_history.json"
    if not real.exists():
        pytest.skip("仓库里没有校准账本")

    model = schemas.current_judge_model("evaluator")
    raw = json.loads(real.read_text(encoding="utf-8"))
    stamped = [
        e for e in raw if isinstance(e, dict) and e.get("model") == model and "rep_range_mean" in e
    ]
    entries = schemas._measured_jitter(str(real))
    assert len(entries) == len(stamped), (
        f"噪声带从真实账本读到 {len(entries)} 条，而账本里当前型号"
        f"（{model}）的实测有 {len(stamped)} 条"
    )
    for e in raw:
        if isinstance(e, dict) and "rep_range_mean" in e and not e.get("model"):
            assert e.get("judge") not in entries, (
                f"缺 model 的历史条目 {e!r} 仍被当成了当前仪表的实测"
            )
