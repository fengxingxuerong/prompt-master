"""测试通用夹具。

测量层（重复采样 / 基线 / 成对盲评）默认开启后会成倍增加 LLM 调用数，
而多数既有测试是在数调用次数、比排序、验证控制流。这里把它们固定回旧口径，
让新行为由 tests/test_measurement.py 专门覆盖 —— 免得回归测试变成"跟着实现改数字"。

需要在单测里启用新口径时，在测试体内 monkeypatch.setenv 覆盖即可（测试体晚于夹具执行）。
"""

from __future__ import annotations

import os as _os
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# W16 修复（2026-09-22）：顺序依赖根因。
# pm.llm 在模块级 load_dotenv() 会把宿主机 .env 的 PM_JUDGE_JITTER=2.6 灌进进程
# 环境；pm.schemas 又在 import 时把该值冻结进模块常量 JUDGE_JITTER。于是
# "谁先被 import" 决定测量层用例结果：单跑 test_measurement 时 schemas 先于
# llm 加载 → 0.0（绿）；先跑 test_api 时 llm 先灌 env → 2.6（红）。
# 这里在任意测试模块加载前把常量钉回旧口径 0.0；需要测抖动的用例一律
# monkeypatch.setattr(schemas, "JUDGE_JITTER", ...) 显式覆盖（既有用例已如此）。
# ---------------------------------------------------------------------------
_os.environ.pop("PM_JUDGE_JITTER", None)

from pm import schemas as _pm_schemas

_pm_schemas.JUDGE_JITTER = 0.0


@pytest.fixture(autouse=True)
def legacy_measurement_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PM_SAMPLES_PER_CASE", "1")
    monkeypatch.setenv("PM_BASELINE", "0")
    monkeypatch.setenv("PM_PAIRWISE", "0")


@pytest.fixture(autouse=True)
def pin_force_json_channel_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """宿主机 .env 的 PM_FORCE_JSON_CHANNEL=1（2026-09-30：sensenova 结构化输出引擎
    全程 500，本地强制走文本通道）会经 load_dotenv 灌进测试进程，让默认走通道 A 的
    用例被静默切到文本通道（假 _ToolLLM 没有 .invoke，直接炸）。钉回默认 0；
    需要强制文本通道的用例自己 setenv（test_llm_branches 已如此）。
    """
    monkeypatch.setenv("PM_FORCE_JSON_CHANNEL", "0")


@pytest.fixture(autouse=True)
def no_shared_disk_cache(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """默认关掉落盘缓存，且**缓存目录本身**封进 tmp_path：不让开发机
    `logs/*_cache.json` 里的残留决定测试结果。

    真实场景：写一个断言"评委被调了一次"的用例，在自己机器上绿、在别人机器上红了 ——
    因为同样的 prompt/input/output 已经在缓存里。测试要验缓存时必须显式开
    （自带 tmp_path 当 PM_CACHE_DIR），否则一律走真调用。

    ⚠️ 只 `setenv("PM_EVAL_CACHE", "0")` 不够：2026-10-05 实测有个用例**自己**
    打开了缓存（它要验的就是缓存），于是 `PM_CACHE_DIR` 仍指向仓库 `logs/`，
    测试期间真的往运营目录里写了 `_cache.json`。两个开关都要封。
    """
    monkeypatch.setenv("PM_EVAL_CACHE", "0")
    monkeypatch.setenv("PM_TARGET_CACHE", "0")
    monkeypatch.setenv("PM_CACHE_DIR", str(tmp_path / "cache"))


@pytest.fixture(autouse=True)
def isolate_modelhub_write_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """ModelHub 的两个**写入**目标一律封进 tmp_path：测试不可能碰到运营文件。

    真实事故（2026-09-25，我自己造成的）：新写的台账用例把隔离变量记成了 `PMH_DATA_DIR`，
    而 `ledger.ledger_path()` 读的是 `PMH_LEDGER_PATH`（默认 `<repo>/logs/modelhub_ledger.jsonl`）
    —— 于是夹具"看起来设了"，实际一次 `write_text` 把线上台账（411 条调用事件，
    覆盖 09-21 21:20 轮换之后到今天）截断成 5 行测试数据。轮换件 `.1` 之前的历史还在，
    **那 411 条找不回来了**。

    为什么修在 conftest 而不是那个测试文件里：这类失败的形式是"某个用例忘了设一个变量"，
    code review 防不住，下一个写 modelhub 测试的人一样会踩。封在这里，忘了设也只是写进 tmp。
    只封写路径（台账 JSONL + 日聚合目录），不封 PMH_CONFIG：那是只读的，
    且把它指向不存在的路径会让"起网关就 ConfigError"的正常断言变成假红。
    """
    store = tmp_path / "modelhub-store"
    store.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("PMH_LEDGER_PATH", str(store / "modelhub_ledger.jsonl"))
    monkeypatch.setenv("PMH_DATA_DIR", str(store))


@pytest.fixture(autouse=True)
def seal_calibration_ledger(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """校准账本（`logs/judge_calibration_history.json`）连同整个产物目录封进 tmp_path。

    这条与上面那条同源，只是危险面不一样：账本是 A/B 协议判定的**唯一事实源**，
    而 `scripts/compare_scoring_ab.py --ledger` 取的是"该模式最近一条有逐条明细的记录"。
    也就是说——任何一个忘了设环境变量的进程内校准用例，只要成功入账一条形状完全正确的
    假记录，下一轮"要不要换评分协议"的结论就会安静地建立在测试数据上。

    2026-09-25 之前这里要逐模块 patch `LOG_DIR` / `_CALIB_HISTORY` 四个绑定点（漏一个就写进
    真实 `logs/`），而 `_CALIB_HISTORY` 是 import 期常量、`PM_LOG_DIR` 对它无效 ——
    第一版密封还漏了"用例体内才 import"那种情况（sys.modules 判定跑在夹具之后）。
    现在 `support.log_dir()` 是晚绑定的，一个环境变量就把产物、历史、库导出、账本全管住。
    """
    monkeypatch.setenv("PM_LOG_DIR", str(tmp_path))


@pytest.fixture(autouse=True)
def isolate_jitter_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """三个评委座位的抖动键必须清掉（2026-10-05 §十八·六）。

    宿主机 `.env` 里有 `PM_ARBITER_JITTER=4.43` 之类，会经 `pm.llm` 的模块级
    `load_dotenv()` 灌进测试进程。后果不是"数值不准"而是**方向反了**：
    之前仲裁线按三席合成被抬到 6.12，越过仲裁自身的极差 4.43 ——
    分歧再大也不会触发仲裁，而所有断言都还是绿的。

    与 `isolate_host_connection_env` 分开是因为性质不同：那些是"真实凭据不许进测试"，
    这些是"行为开关不许从宿主机漏进来"（同族还有 `isolate_behaviour_switches`）。
    """
    for k in (
        "PM_JUDGE_JITTER",
        "PM_EVALUATOR_JITTER",
        "PM_EVALUATOR_B_JITTER",
        "PM_ARBITER_JITTER",
    ):
        monkeypatch.delenv(k, raising=False)


@pytest.fixture(autouse=True)
def isolate_behaviour_switches(monkeypatch: pytest.MonkeyPatch) -> None:
    """把会改行为的 `PM_*` 开关钉到**默认值**，其余一律删干净。

    为什么单开一个夹具（2026-10-05 §二十四）：连接类夹具只按后缀清凭据与端点，
    而下面这些**行为**开关会从宿主机 `.env` 漏进来并静默改变单测语义。
    本机实测 4 个键在漏（评委席位数、目标并发、仲裁线、请求超时）：
    单跑一个文件绿、跟别的文件一起跑红，且**没有 traceback**指向环境变量。

    下面 `delenv` 的那批是"本仓读过、但默认就是关的"：留着它们等于允许宿主机
    把这些功能打开，而它们各自的测试都必须显式 setenv 才生效
    （`tests/test_env_isolation_meta.py` 用 AST 核验这份清单是**有效**删除，
    而不是只在注释里出现过 —— 那版元测试是假闸，删掉条目后它照样绿）。
    """
    # —— 钉到默认值（必须与 .env.example 一致）——
    # ⚠️ 这几行的**元组形状**被 tests/test_guard_mutation.py 当锚点引用：
    #    删掉任一条 → 那条元测试变红 → 证明隔离真的在执行、不是注释。
    for _k, _v in (
        ("PM_JUDGES", "2"),
        ("PM_TARGET_MAX_CONCURRENCY", "4"),
        ("PM_JUDGE_DISAGREEMENT", "2.0"),
        ("PM_TIMEOUT", "120"),
    ):
        monkeypatch.setenv(_k, _v)
    # —— 一律删干净（本仓读过、默认关；开着必须由用例自己显式开）——
    # ⚠️ 这里的**括号列表形状**被 tests/test_guard_mutation.py 当锚点引用
    # （删掉任一条 → 那条元测试变红 → 证明隔离是有效执行的、不是注释）。
    # 改写成多键一行或加括号会把锚点弄失效，届时变异测试会先以
    # "锚点已变"失败——那是好的失败，别顺手把锚点同步过去就算了。
    for k in (
        ("PM_FAKE_BACKEND", ""),  # 桩后端：会让"真的调了 LLM"这类断言失去意义
        ("PM_MAX_LLM_CALLS", ""),  # 调用预算：会让本该跑完的图提前收口
        ("PM_SCORING_MODE", ""),  # A/B 评分协议：改口径
        ("PM_RULE_VETO", ""),
        ("PM_MEMORY_HINT", ""),
        ("PM_MEMORY_SCAN_LIMIT", ""),
        ("PM_CACHE_BACKEND", ""),
        ("PM_TASK_DB", ""),
        ("PM_SERVER_URL", ""),  # 会让 server 用例打到开发机上真跑着的实例
        ("PM_LIVE_LOCK", ""),
        ("PM_LIVE_LOCK_WAIT", ""),
        ("PM_API_TOKEN", ""),
        ("PM_ALLOW_ORIGINS", ""),
        ("PM_RATE_LIMIT_PER_MIN", ""),
        ("PM_CALIBRATE_HOURS", ""),
        ("PM_CALIBRATE_JUDGE", ""),
        ("PM_DRAIN_TIMEOUT", ""),
    ):
        monkeypatch.delenv(k[0], raising=False)


@pytest.fixture(autouse=True)
def isolate_host_connection_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """单测与宿主机 .env 的连接类配置隔离：API Key / 端点 / 模型名一律清空。

    pm.llm 在模块级 load_dotenv() 会把真实 .env 灌进进程环境，而连接配置的解析
    全部走"角色级键优先"：_key_pool 角色键存在就不进全局池、get_llm 角色配置
    覆盖全局、preflight 表格按角色取 key——宿主机 .env 里的角色级 Key 一旦存在，
    测试 monkeypatch 的假值就被架空，单测结果随宿主机配置漂移
    （真实事故：key 池合并、anthropic 分支、preflight 掩码三个断言漏出真实 Key）。
    这里把连接类键全部清掉，测试从空白连接配置开始，需要时自己 setenv。
    行为开关（PM_JUDGES / PM_TIMEOUT 等）不在此夹具范围。
    """
    import os

    for k in list(os.environ):
        up = k.upper()
        if not up.startswith("PM_"):
            continue
        if (
            up.endswith("_API_KEY")
            or up.endswith("_BASE_URL")
            or up.endswith("_MODEL")
            or up in ("PM_API_KEYS", "PM_PROVIDER")
        ):
            monkeypatch.delenv(k, raising=False)
