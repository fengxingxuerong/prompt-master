# PromptMaster

把 `prompt-engineer-agent.md` 里的 6 个提示词，实现成一个**真正能跑**的 LangGraph 应用：
需求澄清 → 提示词生成 → 构造测试输入 → 真实调用目标模型 → 多维度评估 → 定向修订 → 交付报告。

原文档是设计稿，直接照抄会踩不少坑。本项目在落地过程中把这些问题逐一修掉了，
并在代码注释里标注了每一处「原文档为什么不行」。

---

## 文档索引

细节章节已迁出至 docs/（正文逐字未改，章节编号沿用原文档口径，主 README 跳号即已迁出）：

| 文档 | 内容（原 README 章节） |
|---|---|
| [docs/design-notes.md](docs/design-notes.md) | 一、相对原文档修了什么；二、真实缺陷记录（C*/M*/H*/L* 编号定义在这里） |
| [docs/agent-cli-guide.md](docs/agent-cli-guide.md) | 三之二、智能体调用指南（退出码协议 / 异步 submit-wait / 记忆层子命令 / MCP） |
| [docs/rest-api.md](docs/rest-api.md) | 四、REST API 服务与容器化部署 |
| [docs/evaluation.md](docs/evaluation.md) | 六、评估口径；六之二、测量层 |
| [docs/operations.md](docs/operations.md) | 七、验证方式与诚实边界；十三、发布与部署 |
| [docs/fix-log.md](docs/fix-log.md) | 十~十二、按日期的测试修复 / 安全加固 / 提示词评审记录 |
| [docs/agent-skill.md](docs/agent-skill.md) | OpenClaw Agent Skill 说明 |
| [docs/qa_report_2026-09-08.md](docs/qa_report_2026-09-08.md) | 2026-09-08 真实端到端 QA 报告 |

---

## 三、安装与运行

> Windows 下把 `.venv/bin/python` 换成 `.venv\Scripts\python.exe`，
> 并建议先 `$env:PYTHONIOENCODING="utf-8"`（否则中文日志重定向到文件时会被 GBK 码页弄乱）。

```bash
cd prompt-master
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt
# 只想装包本身（pm/）与依赖：`pip install .` 现在可用；
# 开发工具（pytest/ruff/mypy）在 `pip install ".[dev]"` 里。
# 无 API Key 也能先跑一遍自检：.venv/bin/python run.py --selftest

cp .env.example .env     # 填入 API Key
```

```bash
# 真实运行
.venv/bin/python run.py --task "帮我写个 prompt 让 AI 分析销售数据" \
                        --target-model "deepseek-v3"

# 只看流程、不管成本（回到旧口径：不采样、不跑基线与盲评）
.venv/bin/python run.py --task "..." --samples 1 --no-baseline --no-pairwise

# 带 ground-truth 的用例集（expected 会参与确定性断言，不满足则一票否决）
.venv/bin/python run.py --task "..." --cases-file cases.json --assert-mode contains

# 需求不清晰时向你提问（人在回路）
.venv/bin/python run.py --task "写个 prompt" --interactive

# 从文件读取需求，指定报告输出
.venv/bin/python run.py --task-file req.txt --out report.md

# 带事实断言（ground-truth）运行：提供测试集 + 期望输出，
# 确定性校验失败会一票否决"达标"（评委放水会被标记）
.venv/bin/python run.py --task "解析销售数据 CSV" \
                        --cases-file cases.json --assert-mode contains
#   cases.json 支持逐条覆盖模式（一份用例集里混写两类才是常态）：
#   cases.json: [
#     {"input": "华东,120,华南,98", "expected": "华东"},                       # 跟全局：contains
#     {"input": "缺货", "expected": "数据缺失", "mode": "exact"},                # 字面片段
#     {"input": "含糊提问", "expected": "必须先列出缺失信息再回答", "mode": "rule"}  # 交评委核验
#   ]
#   API 侧同名字段是 `test_cases[].assert_mode`（逐条）与 `assertion_mode`（全局）。
#   字面类模式 + 规则形状的 expected 会被 CLI 当场拦住（也会在运行时降级为 ⚠️ 仅提醒）。
#   ⚠️ `expected` 有两种写法，别再弄混（两种都是支持的，但语义完全不同）：
#   • 字面片段 → 用 `contains` / `exact` / `regex` / `custom:<name>`，确定性校验，失败**一票否决**；
#   • 需求规则 → 用 `--assert-mode rule`，它会被当作核对清单注入评估器（<RULES>），
#     评委逐条给 satisfied + evidence 写进 `rule_checks`；默认**只提醒不否决**（`PM_RULE_VETO=1` 可改）。
#     拿规则去跑 contains 永远命不中，所以 CLI 会直接拦住这种写法。

# 启动 REST API 服务（FastAPI）
.venv/bin/python run_server.py --port 8080

# 无 API Key 演示模式：服务全流程用假后端跑通（不消耗真实模型）
PM_FAKE_BACKEND=progress .venv/bin/python run_server.py --port 8080

# 自检与单测都不依赖网络：在没有任何 .env 的干净检出里同样全绿
# （修 C5 之前并非如此：并发分支会绕过假后端去连真实端点）

# 查看图结构、跑自检、跑本地 e2e
.venv/bin/python run.py --mermaid
.venv/bin/python run.py --selftest

# 节点提示词回归评测：离线结构契约（零成本，已纳入 pytest）；
# 改动 pm/prompts.py 后先跑这个，提示词改坏了结构会立刻红
.venv/bin/python eval_prompts.py
.venv/bin/python eval_prompts.py --node clarifier
# 真实调用模式（花真钱，手动跑；用确定性代码侧校验而非 LLM 自评）
.venv/bin/python eval_prompts.py --live

# 用例集模板库（case_templates/）：带 ground-truth 的领域模板，字面断言与
# 语义规则混写，可直接作为 --cases-file 使用；跑批前建议先做端点预检
.venv/bin/python run.py --preflight
.venv/bin/python run.py --task "让 AI 分析销售数据" \
                        --cases-file case_templates/sales_analysis.json

# OpenClaw/AutoClaw 任务族（2026-09-16）：飞书日报 / 定时简报 / 浏览器采集，
# 每模板含 1 条注入用例（hijack_marker 确定性劫持检测）；建议目标模型用
# GLM-5-Turbo（Pony-Alpha-2，OpenClaw 场景专属档案）：
.venv/bin/python run.py --task "把当日工作记录整理成飞书日报消息" \
                        --target-model glm-5-turbo \
                        --cases-file case_templates/openclaw_feishu_daily.json
# 达标运行会在报告同目录顺手产出 SKILL.md（OpenClaw 可部署 Skill 文件）；
# 注入用例被劫持时：达标结论被门禁压制，且拒绝渲染 SKILL.md（落 .blocked 说明）

# 耗时档位（2026-09-13 实测口径，DeepSeek-V4-Flash / n=3）：
#   --fast  ≈ 8-10 次调用 / 3-6 分钟   → 只回答"这版能不能用"
#   默认档  ≈ 25-35 次调用 / 20-30 分钟 → 多出"比不优化好多少"（基线 Δ）
#   全量档  = 默认档 + 盲评 + 多采样    → 分钟数×2 以上（实测 47 分钟 / 58 次调用）
# 快速档：关基线、关盲评、单采样、1 轮修订（显式传 --samples/--max-iter 时以显式值为准）
.venv/bin/python run.py --task "让 AI 抽取销售数据中的城市与金额" --fast

# CLI 入参护栏与 REST 同口径（2026-09-13 补齐）：task 4-8000 字、--cases 1-8、
# --max-iter 0-10、--assert-mode 白名单；超限在解析阶段就报错，不会烧到一次模型调用

# 评委校准（回答"评委打 8 分可信吗"）：给锚点样本打人工分后对比评委分，
# 输出 MAE / 系统偏松偏严 / 排序一致性；换评委模型或改评分提示词后跑一次
.venv/bin/python calibrate_judge.py --write-template   # 生成锚点样本模板
.venv/bin/python calibrate_judge.py                    # 校准评委 A
.venv/bin/python calibrate_judge.py --judge evaluator_b
bash examples/run_e2e_stub.sh          # Linux / macOS
.venv\Scripts\python.exe -m pytest tests/ -q
# Windows 等价的桩服务 e2e（自动挑端口、跑前清缓存、断言两条通道）：
powershell -NoProfile -ExecutionPolicy Bypass -File examples/run_e2e_stub.ps1
```

环境变量（除模型/Key 外的行为开关）：

| 变量 | 默认 | 作用 |
|---|---|---|
| `PM_PASS_THRESHOLD` | `8.0` | 达标阈值。平均分需 ≥ 阈值、最低分 ≥ 阈值-1、均分下界 ≥ 阈值-1，且用例数完整 |
| `PM_SAMPLES_PER_CASE` | `2` | 每条用例重复采样次数（上限 5）：中位数定分、极差当噪声。设 1 回到旧口径 |
| `PM_BASELINE` | `1` | 是否跑基线（原始需求直喂 target）。关掉就只会有“绝对分”，没有 Δ |
| `PM_PAIRWISE` | `1` | 是否做成对盲评（优化版 vs 基线，A/B 随机映射） |
| `PM_UNSTABLE_SPREAD` | `1.5` | 同用例采样分差超过此值 → 报告点名为“结论不稳” |
| `PM_UNESTIMATED_MARGIN` | `0.5` | `PM_SAMPLES_PER_CASE=1` 时拿不到噪声：早停余量退到这个值，且不判平台期 |
| `PM_JUDGE_BIAS_ALERT` | `1.0` | 评委自报分系统性高于代码加权分的告警线 |
| `PM_ASSERT_NORMALIZE` | `1` | `contains` 断言忽略大小写 / 全半角 / 空白差异（veto 下不误杀排版差异）；`0` 回到严格字面 |
| `PM_ASSERT_REGEX_TIMEOUT` | `2` | 有风险形状的正则（量词包住分组）在子进程里跑，超预算即杀并按未通过处理 |
| 断言口径防呆 | — | `contains`/`exact` 的 `expected` 若含“必须/不得/应……而非……”这类规则词或超过 80 字，判为“写错了对象”：仍显示失败但标 `⚠️ 仅提醒`，**不计入否决**（`regex`/`custom:*` 不受此限制） |
| `PM_RULE_VETO` | `0` | `rule` 模式下评委判定的“规则未满足”是否也一票否决。默认只提醒：语义判定带噪声，拿它否决等于把主观伪装成客观 |
| `PM_FORCE_JSON_CHANNEL` | 关 | 置 1 则跳过原生结构化输出通道（不置也行：撞过 400 后会记住端点指纹自动跳过） |
| `PM_STRUCT_METHOD` | 空 | 结构化输出方法：空 = langchain 默认（`json_schema`）；端点只认 function calling 时设 `function_calling`（trace 的 `channel` 会如实标为 `function_calling`） |
| `PM_JUDGES` / `PM_JUDGE_DISAGREEMENT` | `2` / `2.0` | 评委数与分差阈值 |
| `PM_TARGET_MAX_CONCURRENCY` | `4` | 目标模型并发上限（硬上限 32，超出自动封顶并告警） |
| `PM_EVAL_CACHE` / `PM_TARGET_CACHE` / `PM_CACHE_DIR` | 开 / `logs/` | 本地结果缓存（键含模型与采样参数指纹） |
| `PM_LOG_DIR` | `logs/` | 报告与状态快照的输出目录 |
| `PM_MAX_TASKS` | `200` | 服务里保留的任务数上限（按插入序淘汰；SQLite 存储时是库内条数上限） |
| `PM_TASK_DB` | 未设 | 任务/赛马记录的 SQLite 路径。设了才允许 `--workers > 1`（各 worker 共享记录） |
| `PM_API_TOKEN` | 未设 | 设了则 `POST /api/*` 必须带 `X-API-Key` |
| `PM_ALLOW_ORIGINS` | 未设 | 逗隔列表；**不设则不开 CORS** |
| `PM_FAKE_BACKEND` | 未设 | `progress\|stall\|dispute\|unclear`：无 Key 的演示模式（任务级隔离，不会污染进程） |
| `PM_MAX_LLM_CALLS` | 未设 | 任务级调用总闸：下一轮修订的预估调用数会突破该值时立即止损，按 `early_stopped` 交付当前最佳（reason 写明是预算而非收敛）。自治/IM 场景的成本保险丝 |

运行产物：
- `logs/report_<run_id>.md` —— 交付报告（最终提示词 + 评分 + 版本曲线 + 遗留问题）
- `logs/run_<run_id>.json` —— 完整状态与 trace，便于回溯和离线分析

---

## 五、拓扑

```mermaid
graph TD;
	__start__([start]) --> clarify
	ask_user --> clarify
	clarify -.-> ask_user
	clarify -.-> optimize
	clarify -.-> compare
	optimize -.-> mock
	optimize -.-> compare
	mock --> baseline
	baseline --> test
	test --> evaluate
	evaluate -.-> revise
	evaluate -.-> compare
	revise -.-> test
	revise -.-> compare
	compare --> report
	report --> __end__([end])
```

- `clarify` → `ask_user`：需求不清晰且开启了交互模式时中断向用户提问
- `evaluate` → `revise`：未达标且未超轮次；`revise` → `test` 复用同一批测试用例重新验证
- `optimize` / `revise` 硬失败时不再往下跑：`route_after_generate` 直通 `compare → report`，不会拿着空 prompt 继续烧评估预算
- `baseline`：把**原始需求原样喂给 target**，与主路同口径采样 + 同口径评委，产出“不做优化能到几分”的参照点
- `compare`：成对盲评（优化版 vs 基线），A/B 按 run_id+用例号确定性随机映射，消除位置偏好又保证可复现

---

## 八、目录结构

```
prompt-master/
├── run.py                      CLI 入口（薄壳：只保留启动顺序，逻辑下沉 pm/cli/）
├── run_server.py               REST API 服务启动入口（FastAPI + uvicorn）
├── eval_prompts.py             节点提示词回归评测（离线结构契约 / --live 真实校验）
├── calibrate_judge.py          评委校准（锚点样本人工分 vs 评委分：MAE/偏差/排序一致性）
├── judge_calibration/          锚点样本目录（samples.example.json → 复制为 samples.json 打分）
├── prompt_eval/cases.json      节点提示词评测用例集（离线用 vars 与模板占位符对应）
├── case_templates/             领域用例集模板（带 ground-truth，可直接作 --cases-file）
├── pm/
│   ├── backend.py              可注入的调用钩子（演示模式 / 自测的任务级隔离）
│   ├── ratelimit.py            服务层滑动窗口限流（提交入口成本护栏，默认 10 次/分钟）
│   ├── bootstrap.py            入口引导（Windows 控制台 UTF-8 编码兜底）
│   ├── assertions.py           事实断言（ground-truth）：exact/contains/regex/自定义，一票否决达标
│   ├── state.py                LangGraph 状态与 reducer
│   ├── schemas.py              Pydantic 数据契约（含加权评分、评委元信息、用例数完整性、断言聚合）
│   ├── prompts.py              6 个提示词（system/user 分离 + 注入隔离 + 定界符卫生）
│   ├── llm.py                  LLM 工厂 + 双通道结构化输出 + Key 池轮换 + 环境变量容错
│   ├── cli/                    CLI 实现包（run.py 逻辑下沉：main / pipeline / selftest / history / agent_mode / calibrate / library / support）
│   ├── quality.py              提示词质量门（特征句 + 自动推导的标签清单，不误杀领域词）
│   ├── cache.py                本地结果缓存（键含模型与采样参数指纹，线程安全 + 上限淘汰）
│   ├── nodes/                  图节点包（导入面保持 pm.nodes 兼容，按节点族拆分）
│   │   ├── clarify.py          Node 1/1b 需求澄清 + 向用户提问
│   │   ├── optimize.py         Node 2 提示词优化（生成质量门，revise 复用）
│   │   ├── execute.py          Node 3/4 模拟输入生成 + 测试执行（并发/缓存/断言）
│   │   ├── judge.py            Node 5 评估（双评委 + 仲裁 + 聚合；用例数不足不判达标）
│   │   ├── revise.py           Node 6 定向修订
│   │   ├── baseline.py         Node 6b/6c 基线对照 + 成对盲评
│   │   ├── report.py           Node 7 交付报告
│   │   └── common / profiles   节点公共设施 + 目标模型家族档案
│   ├── graph.py                图装配与路由（生成失败短路到 report，不空转计费）
│   ├── scheduler.py            后台任务调度器（实时进度 + 赛马编排 + 产物落盘 + 有界任务表）
│   ├── store.py                任务/赛马记录存储（内存 / SQLite；后者是多 worker 的前提）
│   ├── server.py               FastAPI 路由与请求/响应模型（入参上限 + token 鉴权 + nonce CSP）
│   ├── web.py                  Web 控制台（单 HTML 页面，内联 CSS/JS + nonce 占位符）
│   └── testing.py              假后端（拓扑自检与演示模式共用）
├── examples/
│   ├── openai_stub_server.py    OpenAI 兼容桩服务
│   ├── run_e2e_stub.sh          双通道本地 e2e（Linux / macOS）
│   ├── run_e2e_stub.ps1         同上，Windows 版（额外做端口避让、缓存隔离与端点自检）
│   └── run_multiworker_check.py 跨进程共享记录验证（两个服务进程，一个提交一个查询）
├── tests/                      回归测试（pytest 460 项，口径见 docs/operations.md 第七节）
├── Dockerfile / .dockerignore  服务镜像（只装运行时依赖，非 root，/data 挂载点）
├── .github/workflows/ci.yml    CI：ruff + mypy + pytest × 3 个 Python 版本
└── pyproject.toml              ruff / mypy / pytest 配置
```

---

## 九、已知限制

- `examples/openai_stub_server.py` 依赖 fastapi/uvicorn（已在 `requirements.txt` 里，因为服务本身也需要）。
  两个 e2e 脚本都**必须**把全部角色端点指到桩：否则 `PM_EVALUATOR_B_*` / `PM_ARBITER_*` 会从
  `.env` 注回来，“本地联调”会静默变成真实付费请求（`.ps1` 版有 ALL_LOCAL 自检，`.sh` 版靠显式覆盖）。
- 使用 SQLite 检查点时（`--checkpoint`，依赖 `langgraph-checkpoint-sqlite`），checkpoint 表会持续增长，长期运行需自行清理。
- 双评委交叉验证缓解了单 LLM 自评偏差，但评委仍可能同源同倾向（同公司模型家族）。
  若未配 `PM_EVALUATOR_B_*`，评委 B 会回落到与 A 相同的模型与端点——此时代码会自动把
  B 的温度 +0.1 去同质化并告警（每进程一次）；但这只是缓解采样相关性，
  交叉验证要名副其实，部署时请真的给 B 换一个模型家族（`.env.example` 有省钱/质量两档推荐）。
- 目标模型调用已支持并发（`PM_TARGET_MAX_CONCURRENCY`，默认 4），
  但并发上限受目标 API 速率限制约束，调高时请先确认配额。
- 限流退避与降级重试是**相乘**的：通道 A 耗尽 429 退避后会落入通道 B 再跑一轮完整退避。
  现在每个调用点有 `PM_RATE_LIMIT_MAX_WAIT`（默认 60s）的退避预算，超预算就停止重试直接上抛——
  宁叫失败不叫卡死。真端点实测过：配额紧时一个 2 用例的任务在旧行为下能跑 20+ 分钟。
- **真实端点的运维性格（2026-09-10 实测记录，供错峰参考）**：
  - `ip_rate_limit_exceeded` 是 **IP 级**限流，Key 池轮换无效；实测 90 秒、3 分钟冷却
    都不够，**5 分钟以上**才恢复。同机连续跑多个 `--live` / 批量任务时必须在节点间留足间隔。
  - 长纯文本生成调用（optimizer / reviser 一类的长输出）偶发 **504 Gateway Time-out**，
    属端点侧瞬时故障——单独错峰重跑即可，不代表提示词或代码有问题。
  - 晚高峰可能出现账号级全池 429。跑批前建议先 `python run.py --preflight` 做一次
    逐角色冒烟预检（64 token 成本可忽略）：全绿再开任务，限流类失败按建议错峰。
- 降级通道 B 的“每轮降温重试”策略只对普通模型有效：思考型模型（o 系 / reasoning 模式）
  会忽略 temperature，等于重试同样的输出。配思考型模型时建议直接调大 `PM_<ROLE>_MAX_TOKENS`
  并接受首轮即定，或换非思考模型做判定类角色。
- 目标模型温度默认 0.7 偏创意向：数据分析 / 代码等确定性任务建议调低
  `PM_TARGET_TEMPERATURE`（如 0.2），否则采样噪声会被评估器扣 robustness 分。
- 本地缓存（`logs/eval_cache.json` / `logs/target_cache.json`）按内容哈希命中，键里已包含
  **实际生效的模型 / 端点 / 采样参数指纹**与需求文本：
  - 目标输出缓存仍会把目标模型的非确定性「冻结」（同一配置重跑得到相同输出），
    需要真实变化时删缓存文件、或改 `PM_TARGET_*` 参数（改参数现在会自动换键）、或设 `PM_TARGET_CACHE=0`；
  - 缓存文件有容量上限（默认 200 条，插入序淘汰），不再需定期手动清理。
- 任务表与赛马表按 `PM_MAX_TASKS`（默认 200）淘汰；被淘汰后报告仍可从 `logs/` 读到。
- 质量门是确定性关键词/标签规则：能挡住“复读自身 system prompt”这类典型事故，
  挡不住改写过的元话语；它只负责兜底，不能当成语义级质量检测。
- 控制台的 CSP 用逐响应 nonce，但**渲染前转义仍是第一道防线**：CSP 只能拦住"执行"，
  拦不住数据已经进了 DOM。两层都要在，缺一层都不算完（`tests/test_web_console.py` 两层都测）。
- 多 worker 下每个进程各持一份本地结果缓存（`logs/*_cache.json`），命中率下降、可能重复调用
  同样的目标输出；要共享缓存得把 `pm/cache.py` 也换成外部存储。记录本身已共享，结论不受影响。
- `Dockerfile` 与 `.dockerignore` **未经本机构建验证**（开发机没有 docker）：
  其中风险最高的步骤 `pip install .` 已用 `pip install --dry-run .` 验证过能构建出
  `prompt-master-1.0.0`，但仍请首次构建后跑一次 `docker run` + `/api/health` 再上生产。

## 许可证

MIT（见 `LICENSE`）。

---
