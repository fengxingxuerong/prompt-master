"""路径与常量：本包的位置事实源。

2026-10-19 从 `pm/calibration.py`（1474 行）拆出。**注意层级**：`parents[2]` 才是仓库根
—— 本文件比原先的 `pm/calibration.py` 深了一层，写 `parents[1]` 会指向 `pm/`。
"""

from __future__ import annotations

from pathlib import Path

# 仓库根（本文件在 pm/calibration/ 下，parents[2] 才是检出根）：锚点目录 judge_calibration/ 在这里。
# ⚠️ 2026-10-19 拆包时这里从 parents[1] 改成 parents[2] —— 写错会静默指向 pm/，
# 表现是"找不到样本文件"而不是报错层级错。
_REPO_ROOT = Path(__file__).resolve().parents[2]


DEFAULT_SAMPLES = _REPO_ROOT / "judge_calibration" / "samples.json"


EXAMPLE_SAMPLES = _REPO_ROOT / "judge_calibration" / "samples.example.json"


ANCHOR_DIR = _REPO_ROOT / "judge_calibration"


JUDGE_ROLES = ("evaluator", "evaluator_b", "arbiter")
