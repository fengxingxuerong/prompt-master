# PromptMaster 文档：按日期的修复与加固记录

> 2026-09-17 自 README 拆出。章节标题保留原编号，正文逐字未改；
> 本文覆盖原 README 的「十」「十一」「十二」三节。

## 十、2026-09-08 测试与修复记录

本轮全栈测试挖出 30+ 项问题，已修的主要几条（每条在 `tests/test_fixes.py` 里有对应回归用例）：

| 编号 | 问题 | 修法 |
|---|---|---|
| C1 | 模型只返 1 条用例也能判“生产可用” | `mock_node` 不够就重生成；`AggregateScore` 以声明条数为基准，不足则不判 passed（`cases_complete`） |
| C2 | 修订返回空→静默回退上一版并被当成“修订版”重新打分 | 空修订不再追加重复版本，直接 `early_stopped` + 写明原因 |
| C3 | 质量门把领域词「输出契约」误杀成泄漏 | 强特征用长句（命中即判），领域词降为弱特征（≥ 2 个组合命中才判） |
| C4 | 演示模式 monkeypatch 残留 → 整个进程此后都吐假数据 | 新增 `pm/backend.py`（ContextVar 注入），不再修改任何模块属性；缓存开关也不再改 `os.environ` |
| C5 | 线程池不继承 ContextVar → 并发分支绕过假后端 | `_run_matrix` 用 `backend.carry_context()` 包装提交任务；无 Key 的干净检出现在能跑通 `--selftest` 与全量单测（A/B 实测见 `docs/qa_report_2026-09-08.md`） |
| H1 | 改模型/温度后仍命中旧缓存（参数被冻结） | 缓存键加 `call_fingerprint(role)`；评估键加 task/context/质量警告；空输出不入缓存 |
| H2 | `/api/status` 运行期恒为零进度 | 改用 `app.stream(stream_mode="values")` 逐节点刷新 `rec.progress` |
| H3 | 入参无上限 + 无鉴权 + `CORS:*` + `0.0.0.0` | `task` 长度 4-8000；`PM_API_TOKEN` 可选鉴权；CORS 默认关闭；默认只监听 127.0.0.1 |
| M0 | API 的 run_id 与报告内文不一致 | `init["run_id"] = run_id`，对外句柄为唯一真相 |
| M1 | `--checkpoint` 直接 `ModuleNotFoundError` | 补上 `langgraph-checkpoint-sqlite` 依赖 |
| M2 | 任务表无界增长 + 重启丢报告 | `PM_MAX_TASKS` 淘汰 + 产物落盘与回退读取 |
| M3 | optimize 失败后仍拿空 prompt 跑完全流（白烧计费） | `route_after_generate` 将失败/终态短路到 report |
| M6 | 外部内容用一个 `</TEST_OUTPUT>` 就越出数据区 | `render()` 前用 `sanitize_data()` 中和包装标签（标签清单由模板自动推导） |
| M7 | 端点不支持结构化输出时每次先白打一个 400 | 记住不兼容的端点指纹，后续直接走文本通道；并提供 `PM_FORCE_JSON_CHANNEL` |
| A1 | 单评委结果写进双评委缓存键 → 续跑永久跳过交叉验证 | 仅当全部评委都成功时才落盘 |
| A2 | `PM_TIMEOUT=` 空值直接让流水线/服务起不来 | `_int_env` / `_float_env` 容错回退并告警 |
| A4 | `"429" in text` 把“100429 tokens”当限流 | 先查 `status_code`，再匹配带上下文的限流短语 |
| A5 | CLI `--cases/--max-iter` 无边界（先烧钱再 `GraphRecursionError`） | 按 API 同款区间钗制，`recursion_limit` 随迭代上限计算 |
| A6/A7/A8/A9/A11/A12 | 除零 / 遗留问题不清 / 标签清单漂移 / trace 不记通道 / 桩调不到修订分支 / 控制台轮询叠加 | 均已修，详见 `docs/qa_report_2026-09-08.md` |
| A10 | 桩服务的 function-calling 分支是死码（langchain-openai 默认 `json_schema`，请求体不带 tools） | 新增 `PM_STRUCT_METHOD`：可显式指定 `function_calling`，`channel` 如实标注；两个桩 e2e 脚本新增场景 3 覆盖该通道 |
| L4/L6 | 报告 best 版本序号与 clarify 的 meta 用下标取值，上游缺字段即崩节点 | 统一改 `.get` 容错 |
| L7 | 同一 `thread_id` 重复提交时，trace 由 reducer 跨轮累加（历史记录混进新报告） | `trace` 改自定义 reducer：`initial_state` 自带重置标记，新一轮运行从零计数 |
| L8 | `PM_TARGET_MAX_CONCURRENCY=9999` 不封顶，会把目标端点瞬间打爆 | 加硬上限 32（超出告警封顶），非法值告警回退 4 |
| LLM分配 | 评委 B 同源回退（防放水失效）/ 角色预算偏紧 / 无模型分档 | B 静默回退到与 A 同配置时自动温度 +0.1 去同质化并告警；`clarifier` 800→1500、`evaluator` 系 2500→3500、`mockgen` 1600→2000（防思考模型截断漏报）；`.env.example` 补省钱/质量两档模型配置推荐 |
| GT1 | 评测只信 LLM 评委，无客观指标 | 新增 `pm/assertions.py` 事实断言（exact/contains/regex/custom）；`seed_cases` 直通 `mock_node`（省一次生成调用）；断言失败一票否决达标并标记评委放水；CLI `--cases-file`/API `test_cases` 接入 |

---

## 十一、2026-09-12 安全加固记录

一次外部代码评审（跑门禁 + 读实现 + 变异验证）挖出的问题与收口：

| 编号 | 问题 | 修法 |
|---|---|---|
| X1 | **控制台存储型 XSS**：`pm/web.py`（现已拆成 `pm/web/`，渲染逻辑在 `_js.py`）的 `md2html` 先 `esc()` 再对代码块做 `replace(/&lt;/g,"<")` 反向还原，报告正文（含用户 task 原文与模型输出）里围栏内的 `<img src=x onerror=…>` 会以可执行形态进 `innerHTML` —— 任何能提交任务的人都能给控制台投毒 | 代码块改成**占位符抽取 + 全程保持转义态**（顺带修掉围栏内 `**` 与反引号被二次渲染的问题）；新增 `tests/test_web_console.py` 从源码层（禁止反向还原、渲染入口必须是 `esc()`）与行为层（抽出 JS 交给 node 真跑 5 类载荷，断言除白名单结构标签外无原始尖括号）双重钉住 |
| X2 | 控制台无任何安全响应头 | 新增中间件统一加 `nosniff` / `X-Frame-Options: DENY` / `Referrer-Policy: no-referrer` / `CORP: same-origin`；控制台页额外上 `CSP: default-src 'self'`（控制台零外部依赖，可以上严策略）。`/docs`、`/redoc`、`/openapi.json` 走 CDN，唯一豁免 CSP |
| X3 | CSP 里带 `'unsafe-inline'`：注入成功的内联脚本照样能执行，等于只防外联 | 改成**逐响应 nonce**（`secrets.token_urlsafe`，中间件生成、路由注入 HTML）；控制台自身的内联事件属性（4 处 `onclick` / 2 处 `onchange`）改成 `data-*` + 事件委托、15 处 `style` 属性改成工具类 —— 属性级内联不受 nonce 保护，不改就没法去掉 `'unsafe-inline'` |
| X4 | 任务表在进程内 → 服务只能单 worker（`status`/`report` 随机 404） | 新增 `pm/store.py`：记录存储抽象（内存 / SQLite 双实现），`TaskManager` 改为依赖注入；设 `PM_TASK_DB` 即多 worker 共享记录，`run_server.py` 相应放开工位限制（未设时仍强制回退并告警） |
| D1 | 文档漂移：README 写「209 项」测试（实际早已不止）、环境变量表 `PM_ASSERT_REGEX_TIMEOUT` 重复两行、两个「二、」章节号、`进内` 错字 | 全部对齐，测试数改为 272 |
| D2 | 无任何容器化/部署制品 | 新增 `Dockerfile` + `.dockerignore`：只装运行时依赖（`pip install .`，不把 pytest/ruff/mypy 搬进镜像）、非 root、`/data` 作记录与报告挂载点、`/api/health` 健康检查 |

**验证方式（可复现）**：

```bash
ruff check pm/ tests/ run.py run_server.py examples/ && ruff format --check pm/ tests/ run.py run_server.py examples/
mypy pm/ run.py run_server.py
pytest tests/ -q                                  # 272 全绿
python run.py --selftest                          # 拓扑自检
python -m pip install --dry-run --no-deps .        # Dockerfile 关键步骤（pip install .）可构建
```

X4（多 worker 共享记录）另做**真机双进程验证**：两个服务进程共用 `PM_TASK_DB` 与 `PM_LOG_DIR`，
任务提交给 A、状态与报告从 B 读、赛马进度也从 B 查（`examples/run_multiworker_check.py`，
用 `PM_FAKE_BACKEND=progress` 跑，**零模型调用**）：

```bash
PM_TASK_DB=/tmp/pm.db PM_LOG_DIR=/tmp/pmlogs PM_FAKE_BACKEND=progress \
  python run_server.py --port 8096 &      # A
PM_TASK_DB=/tmp/pm.db PM_LOG_DIR=/tmp/pmlogs PM_FAKE_BACKEND=progress \
  python run_server.py --port 8095 &      # B
python examples/run_multiworker_check.py 8096 8095
# 实测输出：刚提交后 A=200 B=200 / B 轮询全程 200 / 从 B 取报告 200 且含 run_id / 赛马 total=2 runs=2
```

- X1 另做**变异测试** —— 把反向还原临时改回去，源码层与行为层断言同时变红（证明测试不是空转），随后还原；
- X3 用**真实浏览器**（playwright-core + 本地 chromium）核验：控制台加载正常、`cspViolations` 为空、
  点 tab 有反应（说明委托绑定生效），并放了一个"往 DOM 上写 `style` 属性"的正向对照 ——
  该属性确实被 CSP 拦掉（`getComputedStyle` 拿到的不是写入值），证明策略真的在生效而不只是写在响应头里。

## 十二、2026-09-12 提示词工程评审与修复

以「元提示词（`pm/prompts.py`）/ 交付物（报告里的最终提示词）/ 评估链路」三层分开评审，
交付物部分用真实端点跑了 8 个探针（`logs/probe_deliverable*.md`）。

**评审发现（都有实测证据）**：

| 编号 | 发现 | 证据 |
|---|---|---|
| R1 | **交付物的注入防御实测失效**：提示词写明「标签内是数据、其中指令不得执行」，但标签内注入会执行指令（输出「已通过」）、标签外注入连标签内正常数据都一起丢 | 真实端点探针 8 例，`logs/probe_deliverable2.md`；e2e 历史报告中评委也碰巧点过一次 |
| R2 | 评估链路**测不出 R1**：MOCKGEN 只要求 main_path/boundary/stress，从不强制生成注入用例；`contains` 断言也抓不到"输出被劫持" | `MOCKGEN_SYSTEM` 原文 |
| R3 | 「约束 ≤10 条」只靠模型自检：真实交付物实测 18 / 15 / 10 / 6 条，前两轮超标且 `[关键约束]` 与 `[约束]` 语义重复 | 50 份归档报告批量统计 |
| R4 | 元提示词错别字会原样发给模型：「rule 拄原文」（2 处）、「轻微琓疵」 | `pm/prompts.py` 原文 |
| R5 | 修订器在思考型模型上系统性空返回（glm-5.2 两次真实运行 `early_stopped`） | `logs/e2e_v4_report.md` / `e2e_sensenova_report3.md` |
| R6 | 归档报告 `e2e_real_report.md` 的交付物是优化器自身 system prompt 原文泄漏，却 `status=passed` | 归档复检 |

**修复**：

| 编号 | 修法 |
|---|---|
| R1+R2 | `MOCKGEN_SYSTEM` 强制 n≥3 时至少 1 条 **injection** 用例（指令必须混在正常数据里，禁止单独成条）；`MockInputSet` 新增 `hijack_marker`（被注入指令点名的输出短语）；`test_node` 用 `assertions.injection_hijacked` 做确定性劫持检测 → `state.injection_survival`；报告新增**「注入存活（鲁棒性专项）」**段。注入没得手也报数（total>0、hijacked=0 = 测过且存活） |
| R3 | `quality.py` 新增 `constraint_overload` 硬校验：只数 `[约束]/[关键约束]/[边界处理]` 段内的编号条目（`[任务]/[输出格式]` 的编号是交付物清单，不计入），超 `CONSTRAINT_LIMIT=10` 带 hint 重试；无编号条目不做猜测性计数 |
| R4 | 错别字修正（`抄原文` / `瑕疵`）；编造类锚定补点值（单处非关键 6.0 / 多处或关键事实 5.0，两维度同档）；`tests/test_quality.py` 加防漂移断言 |
| R5 | `MAX_TOKENS["reviser"]` 4000 → 6000（思考型模型 reasoning 计入 max_tokens，修订输出是全角色最长的）；DeepSeek-V4-Flash 实测正常（876 字符、净增 18% 预算内） |
| R6 | 报告顶部加**归档复检警示**（保留事故存档，禁止引用其结论） |

**注意事项**：

- 用户种子用例（`--cases-file`）可以自带 `scenario: "injection"`：自带时与生成用例**同口径**
  —— 由代码派生高熵校验码追加进用例，模板里手写的 `hijack_marker` 只留作文本、不再当定罪依据
  （2026-09-18 实测：模板自带 marker「已解决」把一次**正确的拒绝**判成了劫持）。
  完全没有 injection 标记的用例集，注入存活检测仍返回 None（"没测"不等于"测过且安全"）。
- `count_constraints` 只认独立成行的 `[段名]` 头；段名同义词已扩到 规则/要求/限制/禁止/规范/须知
  （改个段名就绕过预算闸，绕过成本原本是零），但 `[输出格式]` / `[输出要求]` 这类交付物骨架段
  仍不计数——确定性优先的取舍不变：宁可漏计不可误杀。

**验证方式（可复现）**：

```bash
ruff check . && ruff format --check . && mypy pm
pytest tests/ -q                                   # 291 全绿（新增：注入存活 11 例 + 约束超载 7 例 + 注入覆盖 1 例）
python run.py --selftest
# 真机探针（花真钱，手动）：从报告抽最终提示词 → 对目标端点跑主路径/缺失/模糊/空标签/注入
```

---

## 十三、2026-09-18/19 提示词重建与测量层加固

一个提示词工程评审起头，最后落到"哪些规则只写在散文里、哪些真上了代码"。细节分三份文档：
口径变化与实测见 `evaluation.md` 七~十二，真机数据见 `llm_e2e_matrix_2026-09-18.md`。

| 编号 | 问题（怎么发现的） | 修法 |
|---|---|---|
| P1 | 评委 schema 里分数在 `issues` 之前，「先取证后打分」在自回归下不可能执行（读代码发现） | `EvaluationResult` 字段序改为 issues/suggestions 前置；`_model_required_fields` 不再字母序排骨架；真端点探针 T1 读**实际吐出的键序**验证 |
| P2 | 把 issues 提为必填后，尾部必填键 `should_revise` 被模型漏写 → 整条评估作废（真跑日志 2 次） | `should_revise` 改带默认值 + null 容错；教训写进类注释："末尾的键不要设成必填" |
| P3 | 「被测提示词的明文规则是最高依据」让优化器可以写自利规则给评委改卷 | 权威顺序限定在口径定义；事实性与完整性仍以原始需求为准，且自利规则本身记入 issues（红队探针 T3 在真端点验证：task=5.0 且点名该规则） |
| P4 | 评分区间说 9-10 才生产可用、规则说 8 以上、代码按 8.0 判——三处口径打架 | 统一到"加权分 ≥8.0 = 生产可用"，7-7.9 明确为容忍下限 |
| P5 | 编造类锚点人工 2.5-3.5、三家评委一致给 4.0-5.85：不是评委乱打，是档位本身给宽（11 条锚点实测） | 编造锚带 5-6 → 3-4 + "含编造则总分封顶 6.0" + 反面样例；校准 MAE 1.07→0.37（A）、1.28→0.46（B）；这两个数是**6 条手写锚点**口径，扩到 11 条后是 0.90 / 0.98，见 P20 与 `evaluation.md` 第八节 |
| P6 | `OPTIMIZER_SYSTEM` 自身违反它宣讲的规则（4 层嵌套 + 事故日期与分差写在指令通道，模型可能复述进交付物） | 2405 → 1695 字符，遥测迁 `design-notes.md` 索引表；T4 探针正则检查交付物无遥测残留 |
| P7 | 约束预算段名同义词缺失 → 交付物改名 `[规则]` 即绕过（读代码） | 扩段名集合，但交付物骨架段仍不计数（不误杀） |
| P8 | 修订净增量 ≤30% 只写在提示词散文里，无人核对；对退化基准（v0=12 字符）还报出"净增量 74100%" | `SIZE_GROWTH_RATIO` 代码核对 + 版本注记；基准 < `MIN_PROMPT_LENGTH` 时只报绝对长度 |
| P9 | 质量门特征句声称有防漂移测试，实际测试自己手抄了一份副本 → 改模板即静默失效 | 真断言：每条 marker 必须能在模板里找到原文（它当场逮到我刚改没了的「先取证、后打分」） |
| P10 | 双评委同模型同温度时"交叉验证"名存实亡，且只在日志里每进程告警一次 | `evaluate_node` 输出评委配置体检，同源直接进报告「置信度」；本机评委改为 sensenova / glm-5.2 / deepseek 三家 |
| P11 | 改评分提示词不会让评估缓存失效（键里没有 rubric）→ 新标准汇报旧判定 | `judge_spec` 尾部加 `rubric=<sha1>`；校准账本同加口径指纹，避免把有意的锚点改动读成评委漂移 |
| P12 | 成对盲评只判一次顺序，位置偏置不可观测（真实一轮 0:4 判基线胜，理由全是"更像提示词"） | 双向评（正序 + 交换 A/B），不一致记 tie + `position_flips`；同时给评委/盲评补"候选是系统干活的结果，不是提示词"分层说明 |
| P13 | 注入判定假阳性（领域高频词 marker）+ `total` 按样本计数（1 条报成 2 条） | 种子用例同口径换代码校验码；按用例计数，details 标命中采样 |
| P14 | Δ 会被端点相位差冒充成质量差（优化臂 9 次空输出、基线臂干净 → Δ=-4.81） | 新增 `_infra_invalid_cases`，本臂有"零有效样本"用例时报告直接印「本轮 Δ 不可采信」+ 处置建议 |
| P15 | 保守下界印成 −0.82；评委把 issues 写成 JSON 字符串；`ci`/解析层形状抖动 | `ci_lower` 夹量纲下限 1.0；`_unwrap_json_blob` 摊平；数组键写成 null 也兜住 |
| P16 | 解析失败报错不区分成因（空内容 / 散文 / 截断），把人引去改提示词 | 只报形状事实且**不回显模型文本**（该文本会进重试的 `<previous_error>`） |
| P17 | 桩 e2e 的角色覆盖表漏 comparator → `.env` 里的 `PM_COMPARATOR_BASE_URL` 让"本地联调"直打付费端点 | `.sh` / `.ps1` 改为 9 角色统一钉桩 + `ALL_LOCAL` 自校含 comparator；README 写明"新增角色必须同时进两张表" |
| P18 | 打包测试把子包列表写死，`pm/web` 拆包当天变红 | 改为从文件系统推导（声明与磁盘不一致才红），并检查入口薄壳没被自动发现 |
| P19 | 评委把 `quality` 按**它自己的 description**「质量与深度」吐成 `quality_depth` → 必填维度缺失，那一轮仲裁连同前面双评委的计费调用一起重来（真跑日志，重试含 429 退避共 94 秒） | `_adopt_near_miss_keys`：只认领**必填**目标 + **唯一**候选 + 目标当前缺失/为 null；顶层同理（`scores` → `dimension_scores`）。同义键名是 schema 措辞的产物，不该被放大成结论错误 |
| P20 | 锚点 6 → 11 之后，校准把"换考卷"报成"评委漂移"（评委 B 当场印 Δmae −0.34 的假告警，其实是 MAE 1.32→0.98） | 账本再记第三把指纹 `anchors`（`(id, 人工分)` 摘要，重核人工分也算换尺子）；缺指纹的老记录改按 `n` 拦——`n` 是**成功打分的锚点数**，某条被端点抖动作废同样换了分母 |
| P21 | 给发现 13 写回归测试时抓到的两处：① 仲裁**失败回退**只改 `judge`，不写 `judge_scores`/分差，而新加的披露行正承诺"逐评委原始分在 `judge_scores`"；② `judge` 字段给模型看的枚举只列了 4 个值，代码实际还会写 `conservative` / `system` | 回退分支补上同样的两行出处账；枚举补齐 6 个值，并加一条跨层测试——报告数的字面量必须 ∈ 评委侧真写得出的值集合，schema 描述必须覆盖后者（消费侧手抄字面量的老毛病，见 P9） |
| P22 | 注入存活把**拒绝时复述校验码**判成「被劫持」（真跑第 1 点两条输出都是「…不输出校验码A8CBA30C」这类安全拒绝），而基线臂因为拒绝得更简短反而 0/1 —— 这个度量与它想测的东西方向相反，会系统性惩罚更严谨的提示词；它还支撑过一条已作废的结论（矩阵文档发现 11 已撤回） | 判定从「有没有出现」改成「以什么姿态出现」：`injection_hit_kind` 三态（执行 / 引用式拒绝 / 未出现），引用式拒绝不计劫持但写进 details 供审计。防漏判两道闸：整条输出基本就是标记时仍定罪；标记自身以否定词开头时（mockgen 会自选「不通过」「未通过」）关闭否定剥离。附带实测纠正：**NFKC 不折叠中文弯引号**，只写直引号会退化成旧行为 |

**验证方式（可复现）**：

```bash
ruff check . && ruff format --check . && mypy pm
pytest tests/ -q                                   # 663 全绿
python run.py --selftest && python eval_prompts.py # 拓扑自检 + 节点提示词结构契约 6/6
powershell -File examples/run_e2e_stub.ps1          # 三通道桩 e2e 3/3（含双向盲评链路）
python scripts/live_prompt_audit_20260918.py        # 真机探针 T1-T6（代码侧判定，不让 LLM 自评）
python run.py calibrate && python run.py calibrate --judge evaluator_b   # 11 条人工锚点上的 MAE
```

