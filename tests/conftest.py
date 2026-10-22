"""测试通用夹具。

测量层（重复采样 / 基线 / 成对盲评）默认开启后会成倍增加 LLM 调用数，
而多数既有测试是在数调用次数、比排序、验证控制流。这里把它们固定回旧口径，
让新行为由 tests/test_measurement.py 专门覆盖 —— 免得回归测试变成"跟着实现改数字"。

需要在单测里启用新口径时，在测试体内 monkeypatch.setenv 覆盖即可（测试体晚于夹具执行）。
"""

from __future__ import annotations

import os as _os
from pathlib import Path
from typing import Any

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


@pytest.fixture(autouse=True, scope="session")
def seal_log_dir_for_the_whole_session(tmp_path_factory: pytest.TempPathFactory) -> Any:
    """整个会话期间把 `PM_LOG_DIR` 钉在 tmp 下 —— 单条用例撤销 monkeypatch 之后的**兜底值**必须是 tmp。

    真实污染（2026-10-15 查 `run.py history` 时撞见）：`test_task_table_is_bounded` 只等队尾
    `run-5` 终态就返回，`run-3`/`run-4` 的工作线程还在跑；用例一结束 `seal_calibration_ledger`
    的 monkeypatch 把 `PM_LOG_DIR` 撤成"未设"，那两个线程随后落盘就写进了**仓库 `logs/`**
    （`run_run-4.json` mtime 01:38 正对上那趟 pre-commit 的 pytest，`task: "t"` 与 submit 参数逐字一致）。
    后果不是多了几个脏文件，而是多了几行脏读数：`history` 会把它们当成真运行混进"最近 N 轮"
    与 Δ 统计，而 `logs/` 在 `.gitignore` 里 ⇒ 那条 `git diff --exit-code -- releases/*/data/*` 永远看不见。

    为什么不能只修那一条用例：失败形式是"某个线程活得比它的 monkeypatch 久"，
    下一个用 worker 线程的人一样会踩。封在会话级，忘了等也只是写进 tmp。
    """
    session_dir = tmp_path_factory.mktemp("pm-log-seal")
    guard = pytest.MonkeyPatch()
    guard.setenv("PM_LOG_DIR", str(session_dir))
    try:
        yield
    finally:
        guard.undo()


@pytest.fixture(autouse=True, scope="session")
def fail_if_the_suite_touched_the_operating_logs() -> Any:
    """会话结束时验一件事：仓库 `logs/` 里没多出运行产物。上面那条密封漏了就这里红。

    ⚠️ 覆盖面是**两类**文件，不是一类（2026-10-17 补第二类）：
    - `run_*.json`：跑批产物，漏封会混进 `run.py history` 的 Δ 统计；
    - `notifications.jsonl`：运维告警的**唯一活体凭据**（README §三 那句「损坏累计 554 条」
      就是从它数出来的）。它此前只在 `notify` 里写死 `<repo>/logs`，于是任何触发
      告警路径的用例都会把**假告警**灌进去——实测单跑三个 modelhub 用例就从 727 涨到 731 行。
      只查一种文件名 = 下一个人换一种文件名就能静默写脏运营目录。
    """
    live = Path(__file__).resolve().parents[1] / "logs"
    watch = ("run_*.json", "notifications.jsonl")

    def _snapshot() -> set[str]:
        out: set[str] = set()
        if live.is_dir():
            for pat in watch:
                out |= {p.name for p in live.glob(pat)}
        return out

    before = _snapshot()
    yield
    leaked = sorted(_snapshot() - before)
    assert not leaked, (
        f"测试会话往运营目录 {live} 写了 {leaked} —— 说明某条用例的写路径没被 "
        "`PM_LOG_DIR` 管住（或被线程带出了会话密封）。`run_*.json` 会进 `run.py history` "
        "的读数；`notifications.jsonl` 是运维告警的证据流，假告警进去就分不清真假了。"
    )


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


@pytest.fixture(autouse=True)
def no_ambient_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    """宿主机/CI 环境里的**代理变量**一律清掉（2026-10-12，第三类环境污染）。

    真实事故（本机实测，与上面那些 .env 污染同族、但键名不带 PM_ 前缀）：
    开发机 Shell 里 `http_proxy=http://127.0.0.1:65018`（Clash 之类）环境下，
    `tests/test_dns_outside_timeout.py::test_httpx_timeout_does_not_bound_dns`
    **假红**——httpx 默认 `trust_env=True`，请求被送进代理，探测域名
    `pm-dns-hang-probe.invalid` 从未经过本地 `socket.getaddrinfo`，
    "DNS 挂起跟满 timeout"这个前提整个没发生（实测 elapsed 1.14s < 判据 1.5s）。
    判据没错（库行为没变，§三十·二十三 的结论原样成立），错的是
    **测试进程继承了与结论无关的环境**：同一个用例，清掉代理变量后 elapsed 1.94s、
    照设计通过。

    为什么修在 conftest 而不是那个测试文件里：这条失败的形状是
    "某个用例忘了设 trust_env"，而**以后任何**新建 httpx/urllib 客户端
    却不关心代理的用例都有同一个雷——code review 防不住"没人想到代理"。
    清在这里，全部用例默认在无代理环境里跑；真要验代理行为的用例
    在测试体里自己 setenv（测试体晚于夹具执行，覆盖得动）。
    大小写都列：Linux（CI）上两个名字是各自独立的键，只清一半就漏一半；
    Windows 上 `os.environ` 大小写不敏感（实测：给 `http_proxy` 赋值，读到的
    键是 `HTTP_PROXY`），两条清的是同一个键——留着两条是为了"换到 Linux 还成立"。
    """
    for k in (
        "HTTP_PROXY",
        "http_proxy",
        "HTTPS_PROXY",
        "https_proxy",
        "ALL_PROXY",
        "all_proxy",
        "NO_PROXY",
        "no_proxy",
    ):
        monkeypatch.delenv(k, raising=False)


# ---------------------------------------------------------------------------
# 终端汇总：把「跳过数与原因」打成一眼可见的行（2026-10-23 补）
# ---------------------------------------------------------------------------
# 背景：pyproject 的 addopts 带一个 -q，CI 命令行曾再叠一个（=-qq），「N passed,
# M skipped」的汇总行从 CI 日志里彻底消失——而约百条「依赖本地 logs/ 归档」的
# 用例在干净检出（=CI）里会**合法跳过**，"1924 条"读起来就像"都跑了"。
# -qq 已在 2026-10-23 修掉；这一段是第二层：跳过是合法的（账本不在本机就不该
# 硬跑），但必须**可数、可读**。在 -qq 被谁顺手加回来时，它也是唯一的可见出口。
# ---------------------------------------------------------------------------
def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter) -> None:
    stats = getattr(terminalreporter, "stats", {}) or {}
    skipped = list(stats.get("skipped", []) or [])
    passed = len(stats.get("passed", []) or [])
    failed = len(stats.get("failed", []) or []) + len(stats.get("error", []) or [])
    terminalreporter.write_sep(
        "=", f"终端汇总：passed={passed} skipped={len(skipped)} failed/errored={failed}"
    )
    if not skipped:
        return
    # 原因聚合：skip 的 longrepr 是 (path, lineno, reason) 三元组；把个性化片段
    # （路径/计数）截掉只留头部，同因的合并计数。
    reasons: dict[str, int] = {}
    for rep in skipped:
        lr = getattr(rep, "longrepr", None)
        if isinstance(lr, tuple) and len(lr) == 3:
            reason = str(lr[2])
        else:
            reason = str(lr) if lr else "（无原因）"
        key = reason[:48]
        reasons[key] = reasons.get(key, 0) + 1
    terminalreporter.write_line("跳过原因（同因合并，前 48 字符）：")
    for reason, n in sorted(reasons.items(), key=lambda kv: (-kv[1], kv[0]))[:10]:
        terminalreporter.write_line(f"  ×{n}  {reason}")
