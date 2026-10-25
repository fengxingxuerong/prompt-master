# PromptMaster 文档：智能体调用指南（Agent-facing CLI）

> 2026-09-17 自 README 拆出。章节标题保留原编号，正文逐字未改；
> 本文覆盖原 README 的「三之二」。

## 三之二、智能体调用指南（Agent-facing CLI）

本 CLI 是为「被其他智能体调用」设计的：stdout 只给机器可读结果，日志全部走 stderr，
退出码是分支决策的唯一依据。智能体的标准学习入口就是本节 + `--help`。

调用形式两种，行为同一套（`prompt-master` 只是 `run.py` 的装包版，共用同一个启动引导与 `main()`）：

```bash
python /绝对路径/prompt-master/run.py --json --task "分析销售数据并给出结论"   # 从检出目录跑
prompt-master --json --task "分析销售数据并给出结论"                          # `pip install .` 之后
```

> ⚠️ `--task` 有 **≥4 字**下限（上限 8000）。照抄占位符 `--task "..."`（3 字）或
> `--task "…"`（1 字）会直接退 **2**「需求描述长度需在 4-8000 字之间」——
> 它看起来像配置错（2 号退出码也确实是"参数/配置错"），但根因是参数取值太短。
> 下面所有示例里的 `--task "..."` 都请替换成一句真实需求。

`.env` 的发现顺序（2026-10-22 修）：先按**当前工作目录**向上查找；检出模式下
再退回仓库根那份。装包态在别的目录跑也读得到该项目自己的 `.env`（修复前读的是
site-packages 的祖先目录，用户项目里的配置会被静默忽略）。Agent 侧仍建议显式注入
`PM_API_KEY` 等环境变量，不要依赖宿主机上那份文件。

### 退出码协议

| 退出码 | 含义 | Agent 应对 |
|---|---|---|
| `0` | 达标交付（passed） | 直接取用 `best_prompt` |
| `1` | 未达标但已交付（max_iterations / early_stopped） | 读报告遗留问题，决定人工介入或调参重跑 |
| `2` | 参数/配置错误（含无 Key、server 未启动、交互下取消成本确认） | 修正参数或先启动 server，**不要原样重试** |
| `3` | 运行失败（status=failed / 未捕获异常） | 看 `errors` 字段；重试安全（缓存命中不重复计费） |

### 同步模式（短任务 / fast 档）

```bash
python run.py --task "分析销售数据并给出结论" --fast --json
# stdout = 一个 JSON：run_id / status / aggregate / baseline_aggregate / pairwise /
#            judge_health / judge_seat_absent / best_prompt / report_path / errors ...
# ⚠️ 基线、盲评与"评委席降级"必须在机器出口里（2026-10-15 实测它们只在报告里，
#    于是桌面端④把"半个评委席"显示成"—"，与双评委共识长得一样）：
#    tests/test_cli_units.py::test_json_channel_carries_every_column_the_report_shows
#    拿同一份状态对差分——报告数出几例降级，JSON 就必须带几例。
# 日志在 stderr；退出码见上表
```

### 手上已有一版提示词（原稿改进 + 免费体检）

```bash
# 先零调用体检：不联网、不花 token，命中规则 → 退出码 1（判据与优化循环内那套同源）
python run.py check --prompt-file 我的提示词.md --json
# → {"mode":"prompt_check","ok":false,"findings":[{"code":"constraint_overload",...}],
#    "constraints":11,"constraint_limit":8,"delimiter_problems":[...],"leak_tags":[...]}

# 同样零调用，改体检一份用例集：先数清它有几票真能挡住差输出，再决定要不要为它花钱
python run.py check --cases-file case_templates/sales_analysis.json --json
# → {"mode":"cases_check","ok":false,"findings":[{"code":"echoes","case":1,...}],
#    "cases":4,"veto_capable":2,"veto_strong":1,"advisory":0,"judge_only":2,"no_assertion":0,
#    "max_cases":8,"veto_strong_cases":[2]}

# 再决定改不改：交原稿时不再从零生成，而是做最小改动（保留原稿术语与字段名）
python run.py --task "分析销售数据并给出结论" --prompt-file 我的提示词.md --json      # submit 子命令同参数
```

- `--prompt` / `--prompt-file` 互斥；长度 8–20000 字（原稿会作为基线臂进每条用例的每次调用）。
- `--cases-file` 与 `--prompt*` 互斥（一次体检一样东西，退出码才有单一含义）；取值与 mode 解析
  走跑批同一条路径（`pm/cli/support.py`），所以"体检说没问题"与"跑起来被拦（退出码 2）"不会各说一套。
  ⚠️ 它给的是**否决票数**，不是"优化有没有用"：后者只有基线对照一条路。实测仓库自带的 5 份模板
  各有 2/4 条是 `rule`（交评委），其中两份的强判据是 **0 条**——这就是"连原始需求都能拿 8.25 分"的
  结构成因，也是它比跑完一整轮（34~57 次调用 / 34~67 分钟）便宜的地方。
- **`--task` 仍然必填**：用例只按需求命题，原稿不参与出题，否则考卷会偏袒原稿自己。
- 交原稿时**基线臂自动换成原稿**，报告头与 Δ 表头都会写明跑的是哪一臂；
  此时 Δ 读作「比你自己的版本好多少」，不再是「比把需求直接喂给模型好多少」。
- 原稿的体检命中项写进报告「原稿体检」节并作为判据交给改进器，
  但**不进** `prompt_quality_issues`（那一列会按轮次注入评委，改进版不该为原稿的毛病挨扣分）。

### 配置自检（零调用）

```bash
python run.py doctor            # 人读：Python / .env / Key / 角色 / 日志 / 端口 / 常驻服务 / MCP
python run.py doctor --json     # 单个 JSON 对象，退出码 0=无问题 1=只有警告 2=有错误
```

- Key 检查认三种合法配置：全局 `PM_API_KEY`、`PM_API_KEYS` 多 Key 池、逐角色
  `PM_<角色>_API_KEY`；占位符（`sk-xxxx` / `your-key`）按"没配"报错，不会放行到 401。
- 只查本机：不调用 LLM；`PM_SERVER_URL` 不是本机地址时跳过健康检查，避免出网。
- 不回显完整 Key（只给尾号），也不会创建日志目录。

### 用户仓库里的提示词增量门禁（零调用 CI）

```bash
python run.py diff --base-ref origin/main prompts/我的提示词.md --json
# → {"mode":"prompt_diff","ok":false,"base_ref":"origin/main",
#    "rules":{"introduced":["suppressive_rule"],"fixed":[],"still":[]}, ...}
# 退出码 0=没有新引入  1=相对基线新引入规则问题  2=ref/路径/新文件问题
```

- 左侧正文取自 `git show origin/main:<path>`，不是工作区里的另一份文件；修好不判失败。
- 新文件在基线里不存在时退 2 并建议先 `check` —— 不能把"整个文件都是新的"判成"引入问题"。
- CI 里要先 fetch 到基线提交（`actions/checkout` 用 `fetch-depth: 0`，或显式
  `git fetch --depth=1 origin main`），否则 ref 解析不出来（退 2，不会静默放行）。

### 异步模式（全配置，10~30 分钟）：submit → wait

```bash
# 先起常驻网关（一个终端常驻即可，多 Agent 共享）
python run_server.py

# 提交（秒回 run_id，不阻塞）
python run.py submit --task "分析销售数据并给出结论" --cases-file my_cases.json --max-iter 2
# → {"run_id": "...", "message": "..."}   默认连 http://127.0.0.1:8080，可用 --server / PM_SERVER_URL 覆盖

# 等待终态（输出与 --json 同构，含报告全文）
python run.py wait <run_id> --timeout 2400 --interval 15

# 或分步轮询
python run.py status <run_id>
python run.py report <run_id> --out report.md
```

### 提交前预算决策

```bash
python run.py --dry-run --task "分析销售数据并给出结论" --cases-file my_cases.json --max-iter 2
# → estimated_llm_calls: {min, max}；不联网不需要 Key。
# 实测校准：全配置 4 用例双采样 2 轮预估 59~109（2026-10-31 用当前代码复算；
# 旧文档写的 45~83 是更早一版口径）；第 13 轮实测 81 次 ✓
```

### 真实运行前的成本确认（只对"能问人"的终端）

```bash
python run.py --task "分析销售数据并给出结论"   # 交互终端下先报价，再要一句 y/N（默认为否）
python run.py --task "分析销售数据并给出结论" --yes   # 确认过就不再问；长任务改用 run_server.py + submit
```

- 只在**能问人**时拦：TTY 且非 `--json` 且非 `PM_FAKE_BACKEND` 假后端。**Agent / CI / 管道
  一律直接放行** —— 非交互场景没人能回答 `y/N`，拦下来等于把自动化卡死（这条有端到端用例钉着）。
- 报价与 `--dry-run` **同一个函数**（`estimated_llm_calls` 逐字一致），不是两处各算一份；
  配了 `PM_PRICE_*_PER_M` 时连金额区间一起报。
- 非 `y/yes`（含直接回车、管道读到 EOF）按**取消**处理：不发起任何调用，退 `2`。
- `--yes` 是唯一的跳过开关；`--selftest` / `--preflight` / `--dry-run` / `check` / `diff` / `gate`
  这些不花钱的路径本来就不经过它。

### 集成注意

- **重试安全**：target/评估按内容哈希缓存（键含模型与采样指纹），重试命中缓存不重复计费；
  带 `--checkpoint` 可跨进程断点续跑
- **澄清策略**：`--json` 与 `--interactive` 互斥。Agent 模式下需求不清晰时系统自动推断，
  假设全部写进报告的「需求侧遗留问题」，请把它当作必须人工确认的字段
- **用例集**：`--cases-file` 支持
  `{"input", "expected", "mode", "scenario", "hijack_marker", "literal"}`——
  `scenario: "injection"` 激活注入存活确定性检测（报告出现「注入存活」小节，判定用系统派生的
  高熵校验码；`hijack_marker` 只是可选的攻击文本）
- **`literal`（可选，布尔）**：声明"这条 `expected` 就是输出里会出现的原话"。
  默认情况下，`contains`/`exact` 的 expected 只要含规则词（必须/不得/禁止/要求…）或超过 80 字，
  就会被判成"需求规则"——**预检直接拦住**（退 2），因为字面比对永远命不中它。
  合规条款、固定话术这类**本来就是输出原话**的期望，加 `"literal": true` 即可放行：
  照旧逐字比对、照旧计入一票否决（不再降级成"仅提醒"）。
  别名 `expected_is_literal` 等价；真值认 `true/1/yes/on`。

### 三之二·附：记忆层与自检子命令（记忆不用另建，组织历史即可）

```bash
python run.py history --last 30          # 运行历史 + 同任务 Δ 显著性（CI 不含 0 才算"变好了"）
python run.py calibrate --judge evaluator_b --json   # 评委校准 + 与上次对比的漂移告警（±0.5）
python run.py library --query "分诊"     # 跨任务提示词资产库：检索历史达标成品
python run.py library --export <run_id> --out my_prompt.md   # 导出成品直接复用
```

- `history`：把"看起来高了 3 分"升级为"Δ 均值 ± 95% CI，是否显著"——演示模式的 run 自动排除
- `calibrate`：锚点样本上对比评委分与人工专家分（MAE / 偏差 / Pearson r / **过线判定一致率**），
  每次结果入账本 `logs/judge_calibration_history.json`，与上次对比漂移超 ±0.5 告警。
  账本除聚合数外还落**逐条明细**（`items` / `rep_items` / `n_failed` / `failed`）——
  只有聚合数时"n 从 18 变 15"是个无法追问的信号，两臂 A/B 就没法配对归因（2026-09-25 的教训）。
  三个不花钱、不调模型的入口：
  `--make-form <csv>` 把待确认锚点摊成打分表（只填 `human_score` 一列），
  `--apply-scores <csv>` 回填（**只改填了分的条目**；有任何非法行就一条都不写），
  `--aggregate` 把账本里同口径的最近几轮合并成带 CI 的跨轮读数（MAE 锚点重抽 CI 与
  轮间极差分开报，回答"单轮读数能信多宽"；口径见 `docs/evaluation.md` §十四）。
  协议 A/B：`--scoring-mode impression|checklist`，配对比较见 `scripts/compare_scoring_ab.py --ledger`。
  实测示例：evaluator_b bias=+1.24 超告警线（偏松实锤，含幻觉放水）
- 判别力与可估性（2026-10-06，判据与实测见 `docs/evaluation.md` §三十一）：`calibrate` 的报告与
  `--aggregate` **两处**都多出三行——
  **AUC**（评委分对"人工达标"标签的判别力，配配对重抽 95% CI，**只与 0.5 比**，点值不算证据）、
  **调阈值的净增益**（留一折重定阈值 − 平凡基线"一律判多数那一侧"，**可以为负**；
  同批拟合的最优阈值只当上界打印）、
  **可估性**（2×2 任一格子 <10 条或 κ 的 CI 半宽 >0.3 → 改印「这张考卷答不了达标一致性」，
  不再印一个 κ 点值让人以为量具坏了）。聚合侧的重抽单位按锚点聚类，与 MAE 的 CI 同一口径。
  Agent 侧机读位：`--json` 输出里的 `discrimination`（`auc` / `auc_ci95` / `loo_gain_vs_trivial` /
  `min_cell` / `estimable`），`estimable=false` 时**不要**把 `decision.kappa` 当结论往下传。
- 补采队列（`harvest_anchors.py`，零调用）：`--coverage` 只读地打印"每个人工标签格子还差几条"
  与"评委分带 → 人工达标的历史命中率"（实测：评委说 8.0-8.9 的 14 条锚点里人工达标 **0** 条）；
  `--prefer-deficient` 让采集按缺口排序，而不是按评委分带的固定顺序平均。
  门槛常数与 `pm/calibration.MIN_ESTIMABLE_CELL` 同源，不在脚本里另立一个数。
  两处安全默认（2026-10-06 修）：**去重集覆盖锚点目录里的全部样本文件**
  （此前写死两份，恰好漏掉装着 48 条已确认人工分的 `samples.candidates.json`，
  实测重采 25 条里 18 条是已标过的同一份输出）；**目标文件里有已确认人工分时拒绝覆盖**
  （退出码 2，并给换文件名的做法）。确实要重写才加 `--allow-overwrite-labeled`。
- `library`：记忆层的读侧——所有终态运行的最佳提示词都可按关键词检索、`--export` 导出复用；
  写侧（跨任务经验自动沉淀）规划中

### 三之二·附二：MCP 接入（智能体标准协议）

`python -m pm.mcp_server`（stdio 传输）暴露 13 个 MCP 工具，OpenClaw / Claude Code / Cursor /
任何 MCP 客户端可直接挂载：

```json
{"command": "python", "args": ["-m", "pm.mcp_server"], "cwd": "<仓库根>"}
```

| 工具 | 类型 | 说明 |
|---|---|---|
| `estimate_cost` | 本地计算 | 提交前预算闸门（调用数区间） |
| `prompt_check` | subprocess 调 CLI | 零调用静态体检一份已有提示词（`run.py check --json` 同口径） |
| `cases_check` | subprocess 调 CLI | 零调用体检一份**用例集**：四桶分区 + 点名"expected 就在 input 里"的那几条（`run.py check --cases-file` 同口径） |
| `prompt_diff` | subprocess 调 CLI | 零调用差分两版提示词：改了什么、新引入几条事故模式（`run.py diff` 同口径） |
| `run_history` / `library_recommend` / `library_export` / `calibrate_judge` / `calibrate_aggregate` | subprocess 调 CLI | 与 `run.py --json` 单一口径，永不出现两套行为 |
| `optimize_submit` / `optimize_status` / `optimize_wait` / `optimize_report` | 转发常驻 server | 10~30 分钟的长任务必须外置到 server（stdio 进程随客户端会话生死） |

依赖钉 `mcp==1.30.0`（**必须 <2**：mcp 2.x 会把 starlette 顶到与 fastapi 0.115 冲突的版本，
实测装完 pm.server 直接 import 失败）。

### 三之二·附三：插件面板 / 桌面窗口（人类入口，Agent 不需要它）

`python run.py desktop` 是给人类用户的**原生窗口**（Tkinter，标准库）：不开浏览器、不起 HTTP 服务、不需要 token。2026-10-20 起重做为“读项目，写提示词”：选项目文件夹 + 写目标（可贴已有提示词）→ 只读扫描项目 → 模型分析进度 → 输出可直接复制的新提示词。项目文件夹支持**粘贴路径**（自动去引号 / `file:///` / 文件→父目录，输入框下方实时给 ✓/✗ 读数）与**选择器**两种填法。旧版的考卷/差分/账本/报价闭环不再塞进窗口，它们仍在 CLI / REST / MCP 里。Agent 仍然只走 CLI / MCP。

⚠️ **它是可选附件**（2026-10-19 从主包移出）：实现在仓库根的 `extras/`（`desktop.py` / `project_agent.py` / `project_context.py`），不随 `pip install .` 安装，回归在 `tests_extras/`（不在 CI 主套件，手动跑 `pytest tests_extras -q`）。装包态调用会提示并返回 2。

桌面端接的接口：

| 动作 | 打的是 | 花钱 |
|---|---|---|
| 只读取项目 | `extras/project_agent.py --project-dir … --scan-only --json` | 零模型调用 |
| 生成提示词 | 同一后端的一次结构化分析（`optimizer` 角色）；输出再过一遍 `pm/quality` 的零调用规则体检 | 一次分析；格式/空正文失败时自动重试，按请求计费 |

安全与边界：扫描器（`extras/project_context.py`）只读，路径必须留在用户选中的根目录内；`.env*`、私钥/凭据文件、`.ssh/.aws` 与依赖/产物目录跳过，普通源码里的已知凭证形态在离开扫描层前脱敏；目录树、关键文件数量与总字符数都有硬上限。**选中的关键文件内容摘录会发给模型提供方**，窗口在输入区下方明确写出这一点。没有 Key 时生成按钮置灰并写明原因；只读取项目永远可用。

窗口纪律（都有用例钉着）：worker 线程**不摸 widget**，结果经队列回主线程；线程异常必须显示，不许停在“进行中…”；生成跑在子进程里，**取消**与**关窗**都会 terminate 它；一轮在跑时第二次提交被拒；优化结果独占一个可编辑文本块，可直接复制或另存。`--project-dir` / `--prompt-file` 可启动预填。所有窗口用例都不碰真实模型：`run_agent_request` 由固定 JSON 替换，子进程边界单独用临时脚本验证。

`python run.py panel`（缺省只绑 `127.0.0.1:8765`，自动开浏览器；`--no-browser` 只起服务）。

它是**同一批判据的另一张脸**，不是第二套判定：面板的三个 POST 端点全部 subprocess 打
`run.py check / check --cases-file / diff`，与 MCP 的 `prompt_check` / `cases_check` /
`prompt_diff` 同一条路。这条"面板不许自己产生判定"由
`tests/test_panel.py::test_cases_check_endpoint_equals_the_cli_for_every_template`
（五份自带模板逐个把面板读数与当场 CLI 读数对齐）钉住 —— 面板改了渲染也不会让判据跟着变。

| 卡 | 端点 | 要不要 Key |
|---|---|---|
| ① 提示词体检 | `POST /api/panel/check` | 不要（零调用、零出网） |
| ② 考卷有几票 | `POST /api/panel/cases-check`（`template` 或粘贴 `cases`） | 不要 |
| ③ 两版差分 | `POST /api/panel/diff` | 不要 |
| ④ 优化闭环 | 复用 `POST /api/optimize` + `GET /api/status/{run_id}` + `/api/report/{run_id}` | 要（没 Key 整卡置灰并写明原因） |

Agent 侧要点三件：

- **`GET /api/panel/capabilities` 是自检不是装饰**：它的 `cli_ok` 来自**当场真跑一发**
  `run.py check`（进程内缓存 60 秒），所以"路由注册成功但转发是坏的"不会被读成就绪。
- **模板名是白名单**：`template` 只认 `case_templates/` 里当场列举得到的名字。
  贴 `../../.env` 会得到 404 —— 这条有守卫用例，别把它改成"看起来没 `..` 就放行"。
- **设了 `PM_API_TOKEN` 时三个 POST 要带 `X-API-Key`**：面板把 token 存在 sessionStorage
  并显示 401 的下一步动作。富控制台（`GET /`）目前**没有**输入 token 的地方，
  设了 token 之后它的提交会 401 —— 要人类界面就用面板，或别设 token。

界面本身的安全约束与富控制台逐条相同（零外部资源、无内联事件属性与 style 属性、
逐响应 nonce、进 `innerHTML` 的文本先过 `esc()`），所以 `/panel` 的 CSP 是
`default-src 'self'` 且 `frame-ancestors 'none'` —— **它不能被 iframe 嵌进别的应用**，
"插件"的含义是一条命令起一个本地小窗口，不是可嵌入组件。
