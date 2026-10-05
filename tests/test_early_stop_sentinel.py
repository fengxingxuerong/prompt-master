"""`None` 哨兵必须活过噪声合成：单次采样时平台期规则必须**保持关闭**。

## 这个缺陷（2026-10-05 审 §十八·六 最后一个未审项时查出，**先于本会话存在**）

调用链（`pm/nodes/judge.py:993-997`）：

```python
target_noise = agg.noise if (agg.n_samples or 1) >= 2 else None   # k=1 → None
noise = measurement_noise(target_noise)                           # ← 这里
stop_reason = early_stop_reason(avg_scores, noise=noise)
```

`None` 在这条链路上有**具体语义**，不是空值：
`agg.noise` 表示"同一用例重复采样的平均极差"，单次采样时该量**测不出来**
（极差恒为 0），所以传 `None` 表示"噪声不可估"。两处消费它的地方都靠 `None` 做判断：

- `noise_margin(None)` → 退到 `UNESTIMATED_MARGIN`（0.5），比 2×实测噪声更宽；
- `early_stop_reason(..., noise=None)` → `if len(...) >= 3 and noise is not None`，
  **整个平台期规则被关掉**（`pm/schemas.py` 的 M4 修复）。

而 `measurement_noise()` 在 `JITTER > 0` 时**返回评委抖动那个数**，
`None` 哨兵就此被吃掉：单次采样 + 配了 `PM_JUDGE_JITTER` 的用户，
会拿到"平台期规则已启用、余量 2×抖动"这个**与设计相反**的结果。

实测两档（`git worktree` 取 HEAD 原始代码验过，非本会话引入）：

| 配置 | `measurement_noise(None)` | 平台期规则 |
|---|---|---|
| HEAD，`JUDGE_JITTER=0`（未配） | `None` | 关闭 ✅ |
| HEAD，`JUDGE_JITTER=2.6` | **2.6** | **启用** ❌ |

所以它一直潜伏着：默认不配抖动的人碰不到，配了的人才会中招，而那正是
"重复采样开不起、只能靠单次采样"的人 —— 最需要平台期规则关着的那批。

## 修法

`measurement_noise()` 必须**透传 `None`**：评委抖动可以加进"可估"那条路的带，
但它绝不能让一条"不可估"的路径变成"可估"。本模块钉死这个语义。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pm import schemas


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    for seat in ("PM_EVALUATOR_JITTER", "PM_EVALUATOR_B_JITTER", "PM_ARBITER_JITTER"):
        monkeypatch.delenv(seat, raising=False)
    schemas._measured_jitter.cache_clear()
    monkeypatch.setenv("PM_LOG_DIR", str(tmp_path))
    monkeypatch.setattr(schemas, "JUDGE_JITTER", 0.0)


def test_none_sentinel_survives_the_noise_combination(monkeypatch: pytest.MonkeyPatch) -> None:
    """核心判据：评委抖动不许把"不可估"变成"可估"。"""
    monkeypatch.setattr(schemas, "JUDGE_JITTER", 2.6)
    assert schemas.measurement_noise(None) is None, "None 哨兵必须透传"


@pytest.fixture
def logdir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """产物目录指到 tmp；`seal_calibration_ledger` 已经这么设过，这里只清 memo。"""
    monkeypatch.setenv("PM_LOG_DIR", str(tmp_path))
    schemas._measured_jitter.cache_clear()
    return tmp_path


def test_measured_jitter_also_may_not_swallow_it(logdir) -> None:
    """账本实测出来的抖动同样不能吞掉哨兵（换条出处，语义不变）。"""
    import json

    (logdir / "judge_calibration_history.json").write_text(
        json.dumps([{"judge": "evaluator", "rep_range_mean": 0.54, "ts": "2026-10-05"}]),
        encoding="utf-8",
    )
    schemas._measured_jitter.cache_clear()
    assert schemas.measurement_noise(None) is None


def test_plateau_rule_stays_off_on_the_single_sample_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """复现缺陷的完整调用链：单次采样 + 配了抖动 ⇒ 平台期必须仍然关闭。"""
    monkeypatch.setattr(schemas, "JUDGE_JITTER", 2.6)
    noise = schemas.measurement_noise(None)  # judge.py:993-996 的原样两步
    assert noise is None
    assert schemas.early_stop_reason([8.6, 8.5, 8.4], noise=noise) is None, (
        "单次采样下'连续两轮没超过'完全可能是噪声，平台期规则必须关着"
    )


def test_regression_rule_still_fires_when_noise_is_estimated() -> None:
    """可估那条路上，回退/平台期该停还得停 —— 修复不许把手脚伸到它身上。"""
    # 判据是 `best_prev - cur > margin`（严格大于）。noise=0.1 ⇒ margin=0.2。
    # 落差 0.1 小于余量 ⇒ 不算回退。
    assert schemas.early_stop_reason([8.5, 8.4], noise=0.1) is None
    # 噪声大 ⇒ 余量宽 ⇒ 同样落差更不该停
    assert schemas.early_stop_reason([8.5, 8.4], noise=2.0) is None
    # 落差确实超过余量 ⇒ 必须停
    assert "修订回退" in (schemas.early_stop_reason([8.5, 7.0], noise=0.1) or "")


def test_none_path_still_keeps_the_wider_unestimated_margin() -> None:
    """`None` 走 UNESTIMATED_MARGIN(0.5) 而不是 2×实测噪声 —— 这条语义不变。"""
    assert schemas.noise_margin(None) == max(schemas.PLATEAU_MARGIN, schemas.UNESTIMATED_MARGIN)


def test_estimated_path_still_combines_in_quadrature(logdir) -> None:
    """可估那条路的方和根合成不受影响（这是修复要保住的行为）。"""
    import json
    import math

    (logdir / "judge_calibration_history.json").write_text(
        json.dumps(
            [
                {
                    "judge": "evaluator",
                    "rep_range_mean": 0.54,
                    "ts": "2026-10-05",
                    # §二十九：缺 model 的条目会被仪表身份过滤挡在噪声带之外
                    "model": schemas.current_judge_model("evaluator"),
                }
            ]
        ),
        encoding="utf-8",
    )
    schemas._measured_jitter.cache_clear()
    got = schemas.measurement_noise(1.5)
    assert got == round(math.hypot(1.5, 0.54), 3)
    assert got > max(1.5, 0.54), "可估时仍要保守"
