# PromptMaster 文档：验证方式与发布部署

> 2026-09-17 自 README 拆出。章节标题保留原编号，正文逐字未改；
> 本文覆盖原 README 的「七、验证方式与诚实边界」与「十三、发布与部署」。

## 七、验证方式与诚实边界

三层验证，各自能证明什么、不能证明什么，说清楚：

| 层次 | 命令 | 证明 | **不**证明 |
|---|---|---|---|
| 单元测试 | `pytest tests/ -q`（919 项，`--collect-only` 计数 2026-09-30 E2E 修复轮 +1；这条数由 `test_test_count_copies_match_the_live_collection` 对着活体收集钉，抄在三处就要三处同改） | 评分公式、短板拦截、**用例数不足不判达标**、JSON 解析、路由、降级重试、注入隔离与定界符越界、双评委合并/仲裁、**单评委结果不污染双评委缓存**、并发排序、缓存命中/淘汰与**配置指纹**、提示词质量门（含领域词不误杀 / 约束超载 / 定界符配平）、null 容错、演示模式隔离与产物落盘、**服务层限流**（滑动窗口 / 429+Retry-After / 赛马按任务数计费）、**两轮澄清提问语义**、**MockGen 场景覆盖校验（含注入用例）**、**用量台账**、**入口编码兜底**、**节点提示词结构契约**、**控制台渲染 XSS 防线与安全响应头**、**任务记录存储层**（内存/SQLite 行为一致 + 跨实例可见 + 并发写不丢账）、**REST API 七个路由与 404/422/401 分支**、**CLI 入参护栏与 `--fast` 快速档**、**LLM 限流退避/降级通道/记账分支**、**缓存落盘与淘汰异常分支**、**SqliteCache 多进程共享缓存**、**评委输出形状容错**（对象数组 issues / JSON 字符串 / 缺尾键 / null 数组 / **键名被按 description 同义改写**，都不该作废整条评估）、**取证先于打分的字段序**、**双向盲评与位置偏置计数**、**fixed–broken 逐条得失记账**、**对比切片与高方差用例优先**、**Δ 的基础设施有效性闸**（零有效样本报「不可采信」）、**注入存活按用例计数与代码派生校验码**、**评委同源检测**、**仲裁分数出处披露**（N/M 例出自仲裁者一人时报告必须说不，别让读者以为那是双评委共识）、**自报分与加权分相等时披露探测器失效**、**满分声明告警**（五维全 ≥9.5 却仍列 issue／或零 issue，只提醒不否决）、**评委复现性 `--repeat`**（绕缓存重复打；绕缓存不许整体替换 CallHook，否则自测会打真端点）、**仲裁失败回退仍记全出处账**（`conservative` 也要带 `judge_scores` 与分差）、**仲裁 prompt 的「给看分数」与「要求忽略」必须共生**、**出处字面量跨 judge/report 一致**（改了值不许让那行静默失效）、**评估缓存的 rubric 指纹**、**校准账本的口径指纹**、**净增量对退化基准不失真**、**`releases/*/tests` 里每一套都必须同时出现在 CI 两个 job 与下面的门禁块** | 任何与模型能力相关的结论 |
| 拓扑自检 | `python run.py --selftest` | 图能跑通、状态正确累加、迭代终止与兜底正确、双评委仲裁路径 | 优化效果（用的是假后端） |
| 真实 HTTP e2e | `bash examples/run_e2e_stub.sh` / `examples/run_e2e_stub.ps1` | 真实客户端 → HTTP → 响应解析 → 校验链路通畅；两条结构化输出通道均可用；**所有角色端点都被锁在桩上** | 优化效果（桩服务返回固定内容） |
| 提示词回归评测 | `python eval_prompts.py`（离线，零成本）/ `--live`（真实调用） | **节点提示词自身的结构契约**（占位符渲染、安全约束块、专项规则块）随 pytest 常态回归；`--live` 用确定性代码侧校验（质量门 / 场景覆盖 / 提问预算 / 劣质输出压分 / 修订净增量 ≤30%）验证提示词行为 | `--live` 之外的任何效果结论（离线只保证结构，不保证生成质量） |
| 评委校准 | `python run.py calibrate`（锚点样本 + 人工分），加 `--repeat N` 测复现性 | **两轴**：与人工分的偏差（MAE/偏松偏严/排序一致性）＋ **评委跟自己的一致性**（同输入绕缓存打 N 次的极差，越过 `PM_JUDGE_DISAGREEMENT` 即告警）| 样本 <5 条时仅方向性参考；都不提升评委能力，只量化偏差；复现性读数不在交付报告里 |

**要验证提示词优化的实际效果，必须配置真实 API Key 运行。**前三层只能保证
"代码是对的"，不能保证"提示词变好了"——这两件事经常被混为一谈。

质量门禁（提交前）：
```bash
# 与 .github/workflows/ci.yml 逐条同口径（路径别删：漏一个文件就是"本地查、CI 不查"的第三种口径）
ruff check pm/ tests/ run.py run_server.py examples/
ruff format --check pm/ tests/ run.py run_server.py examples/
mypy pm/ run.py run_server.py
python -m pytest tests/ -q --cov=pm --cov-report=term-missing     # 覆盖率地板见 pyproject
python -m coverage report --include="*/modelhub/*" --fail-under=82  # 网关那条分项线
python -m pytest releases/lobster/tests/ -q
python -m pytest releases/taskboard/tests/ -q
python -m pytest releases/triage/tests/ -q
# 上面三套跑完，被跟踪的运营数据必须还是干净的（三套全部由
# tests/test_packaging.py::test_ci_and_docs_run_every_release_suite 钉在 CI 与本文里）：
git diff --exit-code -- "releases/*/data/*"
python run.py --selftest          # CI 里额外清 PM_API_KEY 再跑一次（干净检出无 .env 也必须过）
bash examples/run_e2e_stub.sh     # 真实 HTTP 链路 + 三条结构化输出通道（桩端点，不出网）
```
覆盖率的两条线怎么读（地板值取实测下方留余量，不是质量目标）：

| 范围 | 接入门禁前 | 补 modelhub 测试后 | 复测① | 复测②（补运维路由） | 复测③（补切换链） | 门禁地板 |
|---|---|---|---|---|---|---|
| `pm/` 全量 | 84% | 88.5% | 90.43% | 92.44% | **93.49%** | 92 |
| `pm/` 去掉 modelhub | 95.6% | ~96% | 94.9% | 94.9% | **95.6%** | —（被全量线覆盖） |
| `pm/modelhub/*` | 33.5% | 58.6% | 69.8% | 81.0% | **87.1%** | 82 |

> ⚠️ 每一列都是**各自时点的实测**（同一条命令、独占、全量 rc=0），不是同一个数被抄来抄去。
> 表格存在的意义就是让下一个人看见"上一轮 81%、这一轮 87.1%"是真涨了。报数前必须重测：
> 跑法就是上面那条 `python -m pytest tests/ -q --cov=pm --cov-report=term-missing`，
> 且必须先看它自己打印的汇总行与退出码（本机有间歇性假红，见上面两条注意）。
> 复测①→②：给运维路由补 21 条用例（`tests/test_modelhub_admin_routes.py`，194 条语句原本 0 覆盖）。
> 复测②→③：给 `pool.chat()` 的**切换/瞬时重试/断路器记分/台账记账**补 11 条用例
> （`tests/test_modelhub_pool_chat.py`，假上游、零出网）；pool 61% → 85%。

- 剩余缺口很集中：`pool.py` 331-367（真 `urllib` 那段，要活 socket 才走得到）与
  `server.py` 361-444（对话主路由的上游调用体）。`agents.py` 已从"零条直接用例"到 100%。
- 为什么给 modelhub 单独立一条：全量线会把结构问题抹平，而恰恰是这条网关出现过
  "流式通道没有 return、`stream=true` 返回 None、全套测试全绿"的事故（见
  `tests/test_modelhub_stream_contract.py`）。全局线守不住的地方要分项钉。
- ⚠️ 只量子集时会踩到全局地板（`--cov=pm.modelhub` 量出来的数不够 pyproject 的 `fail_under`，直接判红）。
  量局部请补 `--cov-fail-under=0`，那不是门禁坏了。
- 这个数字只统计主进程：`test_cli_guards.py` 那批 subprocess 打真实入口的用例不计入分子，
  所以 `pm/cli/main.py` 的读数偏保守。
- 旧文档里"覆盖率 94%"是手抄快照、且从来没被任何门禁守过（实测 84%）；现在地板值进 CI 了，
  改数字要连着改 `pyproject.toml` 的 `fail_under`、ci.yml 的分项线、上面的门禁块与表格、
  以及**根 README §五** —— 别只改文档。这几处抄件由
  `tests/test_packaging.py::test_coverage_floors_agree_across_all_copies` 钉成同数。
  这条测试是因为实测漂过才加的：CI 早已是 `--fail-under=56`，文档门禁块还写着 `32`，
  照文档跑的人会拿到一条比 CI 松 24 个点、还打印绿色的"门禁"。
  **2026-09-25 深夜又漂了一次，这次漂的是断言没管到的那个文件**：README §五 还写着"地板 87 /
  分项线 56"，而真闸是 92 / 82 —— 访客第一眼照抄的那份反而是唯一自由的，所以把它纳入了断言。
  同一条规则也管住了"多少条用例"这句话（README / 本文 / `.pre-commit-config.yaml` 三处抄件，
  由 `test_test_count_copies_match_the_live_collection` 拿 `--collect-only` 的活体读数对）：
  加用例就要顺手改那三处，否则红的是这条断言而不是文档。
- ⚠️ 已知间歇性红：`tests/test_modelhub_streaming.py` 偶发客户端 `WinError 10053`
  （本机观测约 2/40 次运行，红的那几条签名完全相同）。它红在连接层，不是
  `pm/modelhub/streaming.py` 坏了 —— 先看签名，再决定要不要动产品代码。
Windows 本机注意：pytest 的临时目录根落在 `%TEMP%\pytest-of-<用户>\`，若其中的
`pytest-current` 软链坏掉（本机实测 stat 都抛 WinError 5），pytest 退出期的清理函数会
自己崩 ⇒ **用例全过也返回 rc=1**。仓根 `conftest.py` 与 `releases/*/pytest.ini` 已把临时根
挪进检出目录；若你绕过它们直接跑 pytest，先 `set PYTEST_DEBUG_TEMPROOT=<一个 ASCII 路径>`。
"门禁只认退出码"在这台机器上依赖这条前提，别忽略 rc。

CI（GitHub Actions）在 push / PR 时对 Python 3.11/3.12/3.13 跑以上全部检查；
提交钩子（pre-commit）**本机已装并验过**（2026-09-25：`pip install pre-commit` +
`pre-commit install`，四条钩子对全仓库 rc=0；再故意 stage 一个未使用的 `import os` 提交，
被 ruff-check 当场拦下、commit rc=1、HEAD 未动）。换机器 clone 后要重装一次——
钩子在 `.git/hooks/` 里，不随仓库走。
（代价实测：`pre-commit run --all-files` **109 秒**，大头是全量 pytest。
赶时间可以 `SKIP=pytest-quick git commit ...` 只跑静态三条，但**那一趟不算门禁过了**；
`--no-verify` 是关掉门禁，不是"门禁慢"的解药。钩子的三条命令路径与 CI 逐条相等，
由 `test_precommit_hooks_cover_the_same_scope_as_ci` 守着。）

部署与浏览器运行时验证（2026-09-13 新增，报告见 `docs/archive/deploy_verification_2026-09-13.md`）：
```bash
# 无 docker 环境的替代验证：干净 venv 复刻 COPY → pip install . → 起服务 → healthcheck → 任务闭环
PYTHON="<python3.11+>" bash examples/docker_step_sim.sh
# 真实 Chromium 运行时校验：CSP 违规计数 / tab 事件委托 / 内联 style 被拦（正向对照）/
# XSS 转义回归 / JS 错误 / 资源加载（需要 playwright-core + 本地 chromium）
PM_FAKE_BACKEND=progress python run_server.py --port 8097 &
node examples/browser_runtime_check.cjs http://127.0.0.1:8097/
```

---

## 十三、发布与部署

### 部署形态

```
形态 A · 个人工具（最小）      python run.py --task "..." --fast
形态 B · 常驻服务（推荐）      python run_server.py   # Web 控制台 :8080 + REST API + MCP 同源
形态 C · 智能体接入            MCP stdio（python -m pm.mcp_server）或 Agent CLI
```

### 常驻服务的关键配置（`python run_server.py` 前设置）

| 变量 | 建议值 | 说明 |
|---|---|---|
| `PM_API_TOKEN` | 随机串 | 设了则 POST /api/* 必须带 X-API-Key——**共享网络必设** |
| `PM_ALLOW_ORIGINS` | 留空 | 留空不开 CORS（默认安全）；确需跳源再显式列 |
| `PM_TASK_DB` | `logs/tasks.db` | 设了才允许 `--workers > 1`（多进程共享任务表） |
| `PM_MAX_TASKS` | 默认 200 | 任务表保留上限（SQLite 下为库内条数上限） |
| `PM_CALIBRATE_HOURS` | 12 | 评委校准排程（真实计费，按需开启） |
| `PM_MAX_LLM_CALLS` | 如 300 | 任务级成本总闸（自治场景强烈建议） |
| `PM_FAKE_BACKEND` | — | 无 Key 演示模式（progress/stall/dispute/unclear） |

### 故障排查入口（按顺序）

```bash
python run.py --selftest      # ① 拓扑与控制流自检（无 Key）
python run.py --preflight     # ② 真实端点逐角色冒烟（花小钱，跑大任务前必做）
python run.py history         # ③ 运行历史 + Δ 显著性（判断"有没有变好"）
python run.py calibrate       # ④ 评委可信度校准 + 漂移对比
```

### 常见边界（部署前必读）

- **端点稳定性是环境变量**：AMD 网关曾多次 502/限流（第 8/12 轮作废）；换端点前先 `--preflight`，并把角色级 `PM_<角色>_BASE_URL` 配好
- **max_tokens 预算不能跨端点搬**：第 11 轮「8000=0% 失败」只对 AMD DeepSeek 成立；SenseNova 同模型名 reasoning 吃满 8000——**换端点必须重测预算**
- **注入防御是模型相关的**：同一 prompt 在 AMD 免疫注入、SenseNova 可能 1/1 被劫持——**目标模型用 SenseNova 时，注入用例务必保留在用例集里**（注入门禁会兜底拦截误判达标）
- **日志会膨胀**：`logs/` 会积累每次运行产物；定期把 `logs/run_*.json` `report_*.md` 归档到子目录（.gitignore 已忽略，不影响仓库）
