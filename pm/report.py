"""render_report：章节编排（2026-10-19 拆包后本文件只剩这一层 + 兼容重导出）。

原来的 `pm/report.py` 有 1079 行：取值/判定、43 处跨段依赖、15 个章节渲染函数。
现在：`report_helpers.py`（取值与判定）、`report_sections.py`（逐章节渲染）、
本文件（编排 + 重导出全部符号，保证 `from .report import X` 的既有调用方零改动）。
"""

from __future__ import annotations

from typing import Any

from .report_helpers import (
    BASELINE_DISCRIMINATION_FLOOR as BASELINE_DISCRIMINATION_FLOOR,
)
from .report_helpers import (
    ReportState as ReportState,
)
from .report_helpers import (
    _aggregate as _aggregate,
)
from .report_helpers import (
    _base_word as _base_word,
)
from .report_helpers import (
    _baseline_aggregate as _baseline_aggregate,
)
from .report_helpers import (
    _calibration_disclosure as _calibration_disclosure,
)
from .report_helpers import (
    _cases_short_note as _cases_short_note,
)
from .report_helpers import (
    _delta_avg as _delta_avg,
)
from .report_helpers import (
    _evaluations as _evaluations,
)
from .report_helpers import (
    _human_source_note as _human_source_note,
)
from .report_helpers import (
    _infra_invalid_cases as _infra_invalid_cases,
)
from .report_helpers import (
    _pairwise as _pairwise,
)
from .report_helpers import (
    _report_safe_int as _report_safe_int,
)
from .report_helpers import (
    _seed_arm as _seed_arm,
)
from .report_helpers import (
    _test_runs as _test_runs,
)
from .report_helpers import (
    _verdict_conflict as _verdict_conflict,
)
from .report_helpers import (
    pick_best as pick_best,
)
from .report_sections import (
    _FAILED_JUDGE_RE as _FAILED_JUDGE_RE,
)
from .report_sections import (
    _assertion_lines as _assertion_lines,
)
from .report_sections import (
    _baseline_lines as _baseline_lines,
)
from .report_sections import (
    _ceiling_claim_lines as _ceiling_claim_lines,
)
from .report_sections import (
    _conflict_lines as _conflict_lines,
)
from .report_sections import (
    _empty_deliverable_lines as _empty_deliverable_lines,
)
from .report_sections import (
    _final_prompt_lines as _final_prompt_lines,
)
from .report_sections import (
    _header_lines as _header_lines,
)
from .report_sections import (
    _injection_lines as _injection_lines,
)
from .report_sections import (
    _jitter_provenance_lines as _jitter_provenance_lines,
)
from .report_sections import (
    _judge_bias_lines as _judge_bias_lines,
)
from .report_sections import (
    _judge_source_lines as _judge_source_lines,
)
from .report_sections import (
    _noise_band_lines as _noise_band_lines,
)
from .report_sections import (
    _open_issues_lines as _open_issues_lines,
)
from .report_sections import (
    _pairwise_lines as _pairwise_lines,
)
from .report_sections import (
    _quality_warning_lines as _quality_warning_lines,
)
from .report_sections import (
    _restate_ratio_lines as _restate_ratio_lines,
)
from .report_sections import (
    _revision_delta_lines as _revision_delta_lines,
)
from .report_sections import (
    _run_errors_lines as _run_errors_lines,
)
from .report_sections import (
    _score_overview_lines as _score_overview_lines,
)
from .report_sections import (
    _seed_lines as _seed_lines,
)
from .report_sections import (
    _uncertainty_lines as _uncertainty_lines,
)
from .report_sections import (
    _unresolved_lines as _unresolved_lines,
)
from .report_sections import (
    _usage_lines as _usage_lines,
)
from .report_sections import (
    _verdict_lines as _verdict_lines,
)
from .report_sections import (
    _version_table_lines as _version_table_lines,
)
from .report_sections import (
    _wasted_delivery_lines as _wasted_delivery_lines,
)
from .report_sections import (
    judge_seat_absent as judge_seat_absent,
)


def render_report(state: ReportState) -> tuple[str, dict[str, Any]]:
    """渲染 Markdown 交付报告，返回 (报告文本, 选定的交付版本)。

    这里只做**章节编排**：每章的渲染拆在下面，都是「读 state → 返回若干行」的纯函数。
    原先 500 行挤在一个函数里（圈复杂度 57），改一句措辞要在几十层缩进里找位置，
    而"这一章该不该出现"的前提（`if agg` / `if base` / `if pw`）全靠人眼记住。
    """
    lines: list[str] = []
    lines += _header_lines(state)
    lines += _verdict_lines(state)
    lines += _score_overview_lines(state)
    lines += _uncertainty_lines(state)
    lines += _baseline_lines(state)
    lines += _pairwise_lines(state)
    lines += _conflict_lines(state)
    lines += _version_table_lines(state)
    lines += _unresolved_lines(state)
    lines += _open_issues_lines(state)
    lines += _run_errors_lines(state)
    lines += _usage_lines(state)
    lines += _assertion_lines(state)
    lines += _injection_lines(state)
    lines += _quality_warning_lines(state)
    lines += _seed_lines(state)
    lines += _final_prompt_lines(state)
    return "\n".join(lines), pick_best(state)[0]
