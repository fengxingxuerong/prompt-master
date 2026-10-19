"""评委校准：在锚点样本上对比「评委分」与「人工专家分」，回答"评委打 8 分可信吗"。

为什么需要它：整个闭环的质量上限是评委模型的水位，而"防放水机制"只保证评委
**不虚高**，不回答"它与人类专家的判断差多远"。校准集 = 一批（提示词 × 测试输入 ×
输出 × 人工分）锚点：用与主管道完全相同的评估管线（EVALUATOR_SYSTEM + 同一渲染 +
同一 finalize 加权）跑一遍真实评委，计算偏差与排序一致性——换评委模型、调评分
提示词之后跑一次，"分数可信"就从设计主张变成实测数据。

用法（两种入口，同一个引擎）：
    python run.py calibrate --repeat 3          # 主管道入口（推荐；结果进漂移账本）
    python -m pm.calibration --write-template   # 从示例生成 samples.json（已存在则跳过）
    python -m pm.calibration --judge evaluator_b

流程：
1. 人工给 `judge_calibration/samples.json` 里的每条锚点打 human_score（1-10）；
2. 本工具逐条调用真实评委，得到代码加权分（与主管道同口径）；
3. 输出逐条 Δ、MAE、平均偏差（正=偏松/放水倾向，负=偏严）、Pearson 排序一致性，
   并给对症结论。退出码：0=完成分析（结论好坏都算完成），1=全部评估失败，2=样本问题。

诚实边界：校准告诉你偏差**多大**，不替你提升评委能力；样本 <5 条时数字只有方向性。

2026-09-25 从仓库根 `calibrate_judge.py` 搬进包里：它是 `run.py calibrate` 的引擎，
但作为根目录脚本不会随 `pip install .` 装走 —— 于是装包之后 `calibrate` 子命令是坏的
（当时的兜底是"往 sys.path 塞仓库根再按名 import"，那只在这份检出里有效）。
"""


# 2026-10-19：单文件 1474 行拆成包。这里**重导出全部顶层符号**（含私有），
# 因为既有调用方（pm/cli/calibrate.py 与 7 个测试文件）一律 `from pm import calibration`
# 后按属性访问 —— 少导出任何一个都是静默断裂。`X as X` 是显式重导出，ruff 不报 F401。

# 原单文件 `pm/calibration.py` 顶部 import 进来的外部名字，也属于它的"命名空间"：
# `from pm.calibration import PASS_THRESHOLD` 这类写法在仓里真实存在（harvest_anchors.py
# 与 scripts/probe_checklist_latency.py）。拆包时漏过一次 —— 表现是 collection ImportError。
# 这里一并重导出，保持"模块属性集合"与拆包前一致。
from .. import backend as backend
from ..llm import structured_call as structured_call
from ..prompts import (
    EVALUATOR_SYSTEM as EVALUATOR_SYSTEM,
)
from ..prompts import (
    EVALUATOR_USER as EVALUATOR_USER,
)
from ..prompts import (
    render as render,
)
from ..schemas import (
    PASS_THRESHOLD as PASS_THRESHOLD,
)
from ..schemas import (
    EvaluationResult as EvaluationResult,
)
from ..schemas import (
    judge_disagreement_threshold as judge_disagreement_threshold,
)
from ..scoring import has_empty_deliverable_profile as has_empty_deliverable_profile
from .cli import (
    main as main,
)
from .history import (
    AGGREGATE_BOOTSTRAP as AGGREGATE_BOOTSTRAP,
)
from .history import (
    AGGREGATE_CI_WIDTH_NOTE as AGGREGATE_CI_WIDTH_NOTE,
)
from .history import (
    AGGREGATE_LAST_ROUNDS as AGGREGATE_LAST_ROUNDS,
)
from .history import (
    _paper_identity as _paper_identity,
)
from .history import (
    _paper_identity_lines as _paper_identity_lines,
)
from .history import (
    _round_mode as _round_mode,
)
from .history import (
    aggregate_history as aggregate_history,
)
from .history import (
    latest_pooled as latest_pooled,
)
from .history import (
    render_aggregate as render_aggregate,
)
from .paths import (
    _REPO_ROOT as _REPO_ROOT,
)
from .paths import (
    ANCHOR_DIR as ANCHOR_DIR,
)
from .paths import (
    DEFAULT_SAMPLES as DEFAULT_SAMPLES,
)
from .paths import (
    EXAMPLE_SAMPLES as EXAMPLE_SAMPLES,
)
from .paths import (
    JUDGE_ROLES as JUDGE_ROLES,
)
from .render import (
    _ROLE_LABEL as _ROLE_LABEL,
)
from .render import (
    _bias_verdict as _bias_verdict,
)
from .render import (
    _decision_verdict as _decision_verdict,
)
from .render import (
    _r_verdict as _r_verdict,
)
from .render import (
    render_discrimination as render_discrimination,
)
from .render import (
    render_provenance as render_provenance,
)
from .render import (
    render_repeatability as render_repeatability,
)
from .render import (
    render_report as render_report,
)
from .run import (
    _evaluate_checklist as _evaluate_checklist,
)
from .run import (
    calibrate as calibrate,
)
from .run import (
    evaluate_sample as evaluate_sample,
)
from .run import (
    repeatability as repeatability,
)
from .samples import (
    HUMAN_SOURCE_AI_PROXY as HUMAN_SOURCE_AI_PROXY,
)
from .samples import (
    HUMAN_SOURCE_DEMO as HUMAN_SOURCE_DEMO,
)
from .samples import (
    HUMAN_SOURCE_OWNER as HUMAN_SOURCE_OWNER,
)
from .samples import (
    HUMAN_SOURCE_UNKNOWN as HUMAN_SOURCE_UNKNOWN,
)
from .samples import (
    Anchors as Anchors,
)
from .samples import (
    _confirmed as _confirmed,
)
from .samples import (
    classify_human_source as classify_human_source,
)
from .samples import (
    count_example_anchors as count_example_anchors,
)
from .samples import (
    example_anchor_ids as example_anchor_ids,
)
from .samples import (
    load_human_sources as load_human_sources,
)
from .samples import (
    load_samples as load_samples,
)
from .samples import (
    render_example_anchor_disclosure as render_example_anchor_disclosure,
)
from .samples import (
    render_human_source_split as render_human_source_split,
)
from .samples import (
    split_by_human_source as split_by_human_source,
)
from .stats import BANDS as BANDS
from .stats import (
    BIAS_ALERT as BIAS_ALERT,
)
from .stats import (
    CORR_BAD as CORR_BAD,
)
from .stats import (
    CORR_GOOD as CORR_GOOD,
)
from .stats import (
    CUT_SCAN_STEP as CUT_SCAN_STEP,
)
from .stats import (
    DISCRIMINATION_BOOTSTRAP as DISCRIMINATION_BOOTSTRAP,
)
from .stats import (
    DISCRIMINATION_SEED as DISCRIMINATION_SEED,
)
from .stats import (
    KAPPA_CI_HALF_WIDTH_MAX as KAPPA_CI_HALF_WIDTH_MAX,
)
from .stats import (
    LARGE_DEVIATION as LARGE_DEVIATION,
)
from .stats import (
    MIN_BANDS_COVERED as MIN_BANDS_COVERED,
)
from .stats import (
    MIN_ESTIMABLE_CELL as MIN_ESTIMABLE_CELL,
)
from .stats import (
    MIN_PER_DECISION_SIDE as MIN_PER_DECISION_SIDE,
)
from .stats import (
    MIN_SAMPLES_FOR_R as MIN_SAMPLES_FOR_R,
)
from .stats import (
    MIN_SAMPLES_FOR_VERDICT as MIN_SAMPLES_FOR_VERDICT,
)
from .stats import (
    _auc_vs_label as _auc_vs_label,
)
from .stats import (
    _band_hist as _band_hist,
)
from .stats import (
    _best_cut as _best_cut,
)
from .stats import (
    _cuts_in_range as _cuts_in_range,
)
from .stats import (
    _decision_ci as _decision_ci,
)
from .stats import (
    _decision_stats as _decision_stats,
)
from .stats import (
    _kappa_and_agree as _kappa_and_agree,
)
from .stats import (
    _loo_cut_accuracy as _loo_cut_accuracy,
)
from .stats import (
    _percentile as _percentile,
)
from .stats import (
    _ranks as _ranks,
)
from .stats import (
    analyze as analyze,
)
from .stats import (
    discrimination_stats as discrimination_stats,
)
from .stats import (
    pearson as pearson,
)
from .stats import (
    spearman as spearman,
)
