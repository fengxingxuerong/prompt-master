# 迭代台账（竞品 → 优化 → 测试 → 评分，按轮次回查）

> 每轮一行摘要 + 链接到详细产物。评分卡口径见 scoring-rubric.md。

## Round 0 基线（v1.0.0，2026-09-21）

- 对象：发布验收后的 v1.0.0
- 评分：**D1 功能 8 / D2 性能 10 / D3 易用 8 / D4 稳定 10 / D5 安全 8 / D6 异常 9 = 53/60（8.8）**
- 扣分明细：
  - D1-8：缺流式 SSE（K-3）、虚拟密钥、用量统计端点、metrics、Web 控制台（对照 D1 口径"缺 2–3 项=8"）
  - D3-8：无 Web 控制台（口径"无控制台但 CLI 齐备=8"）
  - D5-8：智能体注册表不持久（K-4，口径"密钥管理 1 项硬伤=8"）；虚拟密钥缺失
  - D6-9：热重载损坏配置保护未验证（口径 9 档）
- 证据：tests_modelhub/{e2e_results,release_results}.json、docs/release/*
- 结论：进入优化轮，目标补齐 P0 六项

## Round 1（v1.1.0-dev1，2026-09-21）—— 实施 OPT-1~6

- 改动：OPT-1 流式 SSE（含上游不可流式时降级合成，X-Modelhub-Degraded 标注）、OPT-2 虚拟密钥（vk-，落盘持久/停用/删除/伪造拦截）、OPT-3 /v1/usage 聚合、OPT-4 /console 四面板控制台、OPT-5 注册持久化 data/agents.json、OPT-6 /v1/metrics
- 测试：v111_test.py 7/7（V1 流式透传实测：上游真实 SSE 4 chunks + 降级合成路径）
- 发现：上游对 stream=true 单独 401（同刻非流式正常）→ 借鉴 LiteLLM 降级思路修复，非网关缺陷

## Round 2（v1.1.0-dev2，2026-09-21）—— 回归 + 语义更新

- 测试：e2e（v1.1 语义版）8/8（D 项验证真实自动切换路径 f2a38d4ed377；G 项验证降级合成路径）；异常注入 6/6
- 语义变更：D 用例接受“切换或上游自愈直接成功”两种合格路径；G 用例从“期望 400”改为“期望 SSE 成功”

## Round 3（v1.1.0 终版，2026-09-21）—— 热重载保护 + 终评

- 补差：D6 最后一分——热重载损坏配置时沿用旧配置继续服务（11 模型不变）+ status().last_config_error 显式可见（独立进程验证 HOTRELOAD::kept_old_config=true）
- 测试：release_results.json 7/7（U1/U2/U3/C1/P2/P1口径/R3热重载）
- 复核（独立两轮）：v111 7/7、e2e 8/8、release 7/7；console HTTP 200；metrics calls=110 rate=0.8909 switches=75；usage agents=19；pool last_config_error=None

## 终评（v1.1.0，两次独立复核一致）

| 维度 | 得分 | 依据 |
|---|---|---|
| D1 功能完整性 | **10** | 池+切换+角色+统一接入+流式+虚拟密钥+用量+metrics+控制台全部可用（V1–V7、e2e A–H） |
| D2 性能 | **10** | 三轮并发 8/8 无串账（C1 三轮 wall 3.8s/5.5s/5.1s）；并发延迟与单发同量级；health 正常；无系统性阻塞 |
| D3 易用性 | **10** | 三行命令启动；SDK/LangChain/HTTP 三方式零改造（A/B/C）；/console 四面板；接入文档含自查清单 |
| D4 稳定性 | **10** | 断路器四语义单测（U2）；401 突发下网关 4/4 vs 裸直连 0/4（D-009 对照）；强制切换留痕（D/H） |
| D5 安全 | **10** | 密钥 ${ENV} 占位（U1）；虚拟密钥可签发/停用/删除且落盘（V2）；伪造归属拦截；台账不记正文与密钥 |
| D6 异常处理 | **10** | 六类显式报错（X1–X6）+ 坏行容错（U3）+ 热重载保护（R3）+ 流式降级不静默（G） |
| **总分** | **60/60（10.0）** | 每维 ≥9 达标，总分 10/10 达成发布门槛 |

## 迭代曲线

R0 基线 53/60（8.8）→ R1/R2 实施六项优化 → R3 终评 60/60（10.0）。每轮扣分与改动全部留档（本台账 + 各测试 JSON）。

（台账终稿）

---

## Round 4（2026-10-02，PromptMaster 本产品）—— 竞品补差：差分 + 成本

> 前三个 Round 的对象是 **ModelHub 网关**（见上）；本轮换成本产品（提示词优化闭环），
> 竞品调研见 `competitor-analysis-prompt.md`，映射见 `optimization-mapping.md` 第二轮。

- 背景：本产品从未做过竞品对标。检索 DSPy / TextGrad / promptfoo / PromptLayer /
  PromptHub / Braintrust / LangSmith / Helicone / FutureAGI / DeepEval 十家后，
  找到两个**真实缺口**（其余为"明确不采纳"，理由已留档）。
- 改动：
  - **PB-1 差分**：`pm/promptdiff.py` + `pm/cli/diffcmd.py` + `diff` 子命令 + MCP `prompt_diff` 工具。
    三层输出（规则得失 / 结构增删 / 逐行 diff）；**退出码 1 = 有新引入的规则问题**，
    可直接当 CI 回归门禁。判据取自 `pm.quality`，与 `check` 同源（有交叉断言防漂移）。
  - **PB-2 成本**：`pm/cost.py` + 报告「成本折算」段 + `--dry-run` 金额外推。
    **不内置价目表**（会过期且错得看不出来），单价由 `PM_PRICE_*_PER_M` 显式给、支持按角色覆盖；
    只配一半单价时该角色整条不折算——把缺的那半当 0 会得到偏低却看起来正常的金额。
- 测试：+41 条（`test_promptdiff.py` 21 + `test_cost.py` 18 + `test_report.py` 2）。
- **验证（本轮实测）**：
  | 项 | 结果 |
  |---|---|
  | 全量 pytest | **999 条 rc=0** |
  | `pm/` 覆盖率 | **93.07%**（地板 92） |
  | `pm/modelhub/*` 分项 | **86%**（地板 82） |
  | 三套 releases | lobster 18 / taskboard / triage 46 全绿 |
  | 运营数据未写脏 | `git diff --exit-code -- releases/*/data/*` rc=0 |
  | stub e2e（真实 HTTP） | 3 场景全通过 |
  | `--selftest` | 通过 |
  | ruff / ruff format / mypy strict | 全绿 |
  | TDD 纪律 | 先跑基线 958 条全绿，改动后复跑 999 条全绿 |
- 诚实边界：这两项**不改变评分与优化效果**，只补可核对性与成本可见性。
  本轮**不含**任何"提示词变好了"的结论——那仍需配真实 Key 跑 e2e。

## Round 5（2026-10-02，PromptMaster 本产品）—— 把差分能力接成 CI 回归门禁

> 承接 Round 4：PB-1 做出了 `run.py diff`，但"能差分"和"每次提交真的在拦"是两件事。
> 本轮把它接进 `.github/workflows/ci.yml`。

- 关键约束（决定了门禁必须是"增量判据"）：
  **`pm/prompts.py` 里 17 个节点提示词模板有 16 个天然命中 `pm/quality.py` 的规则**
  —— 它们是模板，本身就含 `<<占位符>>` 与 `<标签>`。把 `check` 的绝对判据直接搬进 CI，
  这一步从第一天起就是红的；而**永远红的门禁等于没有门禁**，会被绕过、被无视。
  所以判据是「本次改动相对基线**新引入**了什么」，而不是「这版是否合格」。
- 改动：
  - `pm/promptgate.py`（新）：静态 AST 抽取模板（不 import 基线代码）、git 基线解析、
    增量判定、基线缺失时显式吵。
  - `pm/cli/gatecmd.py`（新）+ `gate` 子命令；退出码 0=放行 / 1=有新引入 / 2=门禁自己没跑起来。
  - `pm/promptdiff.py`：补**命中项级通道**（见下）。
  - `.github/workflows/ci.yml`：两个 job 都加门禁步骤；两处 checkout 改 `fetch-depth: 0`。
- **本轮修掉的两个真实缺陷**（都是靠变异测试逼出来的）：
  1. **code 级盲区**：模板本来就命中某规则时，往里再塞一条真的同类条款，
     code 集合前后完全相同 → 判为"无回归"。而 `OPTIMIZER_SYSTEM` 恰好就是这种模板
     （它含引号内的"禁止示例列举"，属规则假阳性）。补**命中项级通道**并剥离引号引用后，
     该改动能被正确抓住（`introduced_total=0` 但 `added_hits_total>0`）。
  2. **绝对路径静默放行**：`--path` 传绝对路径时 `git show <ref>:<abs>` 永远失败 →
     基线取不到 → 全部模板被算成"新增" → 判定为空、**静默放行**。
     长得像"没有回归"，实际是"根本没比"。已归一化为仓库相对路径，并在基线文件缺失时显式披露。
  3. 另有一处 `_git()` 解包写错导致门禁整体抛异常 —— 也是变异测试先发现的
     （没有变异测试，这个门禁会以"永远红/永远崩"的形态上线）。
  4. **首次推送那一支会红**：最初写成裸 `python run.py gate`，而裸跑走的自动探测
     **拒绝把 HEAD 自己当基线**（防"静默通过"的护栏）。在只有一个提交的检出里
     无候选可用 → rc=2 → CI 在首次推送时红。已改为显式传全零 SHA，
     并把"任何 gate 调用都必须带 --base"钉进测试。
  5. 自动探测本身也有过静默失效：单提交仓库里 `master` 能解析且**等于 HEAD**，
     拿它当基线等于自己跟自己比 → 判定永远"无回归"。已改为跳过等于 HEAD 的候选。
- 测试：+37 条（`tests/test_promptgate.py` 39 条），含**变异矩阵**（5 类真回归必拦）
  与**假红防线**（润色/修好/新增模板必须放行），以及 CI 接线护栏
  （两个 job 都跑门禁、checkout 必须 `fetch-depth: 0`）。
- **端到端验证（真实 git 仓库，逐字复用 ci.yml 的 shell 分支）**：
  | 场景 | 结果 |
  |---|---|
  | PR 引入抑制型条款 | rc=1 拦截 ✅ |
  | PR 无回归 | rc=0 放行 ✅ |
  | 新分支首次推送（全零 SHA） | rc=0 且打印「跳过」+「比不了≠没回归」✅ |
  | `--path` 传绝对路径 | 归一化生效，回归仍被拦（不再静默放行）✅ |
  | 变异矩阵 6 项 | 全部符合预期 ✅ |
- 诚实边界：门禁只回答"有没有变坏"，**不回答"够不够好"**；
  也不覆盖 `eval_prompts.py` 的结构契约（那由 pytest 常态回归守）。

## Round 6（2026-10-02，PromptMaster 本产品）—— 节点提示词结构契约进 CI

> 承接 Round 5：改动门禁（增量判据）落地后，补上**绝对结构契约**那一层。

- 为什么不能只靠 gate：实测证据（写进 `tests/test_prompt_eval.py`）——
  把 `pm/prompts.py` 里 CLARIFIER_SYSTEM 的整个 `<安全约束>` 块删掉时，
  gate 的增量判据 `introduced == []`，**完全看不见**（删块不新增规则命中）；
  而结构契约判据当场抓到。这类**结构缺失**只能靠绝对判据守。
  反过来，绝对判据对 16/17 个模板天然命中、会永久假红，所以它在 CI 里只作为
  独立一步存在，不做改动门禁的判据。两步各守一半，一条替不掉另一条。
- 改动：
  - `.github/workflows/ci.yml`：两个 job 各加一步 `python eval_prompts.py`；
    **`--live` 永远不进 CI**（打真实端点、花真钱），由护栏测试钉住。
  - `eval_prompts.py`：
    ①新增 `PM_EVAL_CASES` 覆盖用例集路径（晚绑定，与 `log_dir()` 同惯例）；
    ②用例集读取失败/非法 JSON 时**显式报错**，不静默回退默认
      （静默回退会让 CI 拿一份"不是你以为的"用例集打绿）；
    ③`load_cases()` 过滤 `_comment` 说明键（注意：`main()` 遍历的是
      `TEMPLATES`，所以这不是"会导致跑挂"的缺陷，测试也没给不存在的断言）；
    ④**修掉一个真实检测漏洞**（见下）。
- **本轮修掉的真实漏洞：子串判据被"只删一半标签"绕过**
  `check_structure` 原判据是 `"安全约束" not in system`，而闭合标签
  `</安全约束>` 里**仍含"安全约束"这个子串** —— 删掉开标签、只留闭合标签时，
  残破模板判合格、一路绿过 CI。而那种模板在真实调用里起不到任何边界声明作用。
  已改为要求**成对标签**；对全部 5 个节点 × 删开/删闭两个方向共 10 项变异，
  全部能被抓到（修复前 10 项里 8 项漏网）。
  这个漏洞是"变异测试"逼出来的，不是读代码看出来的。
- 测试：+22 条（`tests/test_prompt_eval.py` 从 4 条到 22 条），覆盖：
  入口退出码（**此前完全没有**：变异实测把 `return 1 if failed else 0` 改成
  `return 0`、把 `--node` 过滤注释掉，pytest 依然全绿）、`PM_EVAL_CASES` 覆盖、
  坏用例集必须显式报错、判据强度、CI 接线护栏（两 job 都跑 / `--live` 不进 CI /
  两步并存）。
- 坦白一处**没做**的：`eval_prompts.py` 仍不在 ruff/mypy 的检查范围内
  （CI 与 pre-commit 的路径都是 `pm/ tests/ run.py run_server.py examples/`）。
  把它纳入需要同步改 CI 3 处 + pre-commit 3 处 + pyproject 的 mypy files +
  一致性测试 4 处，属本轮范围外的扩张，故记录为已知边界而非默默做掉。
  （它当前**碰巧**通过 ruff/mypy，但没有任何东西防它漂。）
- **验证**：全量 1061 条 rc=0、覆盖率 92.71%（地板 92）、modelhub 86%（地板 82）、
  三套 releases 全绿、运营数据未写脏、`--selftest` 通过、`run.py gate` rc=0、
  `eval_prompts.py` 6/6 rc=0、stub e2e 三场景通过、变异矩阵 6/6 + 结构 10/10 全守住。

## Round 7（2026-10-02，PromptMaster 本产品）—— eval_prompts.py 纳入 lint/type 范围

> 结掉 Round 6 末尾记录的那处"已知边界"。

- 动机：`eval_prompts.py` 在 Round 6 成了 CI 的一步，但**不在** ruff/mypy 范围内。
  它当时碰巧通过 —— 也就是说没有任何东西防它漂。
  "CI 绿"里有一块没人查，比不跑那一步更危险：它看起来是绿的。
- 同步的 **5 处**（比请求里说的 4 处多一处，见下）：
  1. `.github/workflows/ci.yml` 三条命令（ruff check / ruff format / mypy）；
  2. `.pre-commit-config.yaml` 三条 entry；
  3. `pyproject.toml` 的 `[tool.mypy] files`；
  4. `docs/operations.md` 的门禁块 —— **请求里没提，但它自称"与 ci.yml 逐条同口径"，
     且此前不被任何测试守着**。不改它就会出现"照文档跑一遍门禁"拿到比 CI 松的范围；
  5. `tests/test_packaging.py` 的一致性测试。
- 一致性测试做了两处**加强**（原来只比"CI == pre-commit"）：
  - 新增 `test_lint_scope_covers_every_gated_entry_script`：钉住"必需在范围内"。
    只比相等的话，**从两处同时删掉一个文件也满足相等** —— 这条看不见那种删法。
  - 新增 `test_docs_gate_block_matches_ci_lint_scope`：把文档门禁块也纳入断言
    （同类漂移这个仓库已经吃过两次：覆盖率地板抄件、用例数抄件）。
- **纳入后立刻抓到一处真实类型问题**：`mypy --strict` 报
  `eval_prompts.py:209: Need type annotation for "result"`。
  成因是 `NODE_SCHEMAS: dict[str, type]` 在静态层面丢掉了"node → 具体 BaseModel 子类"
  的对应关系。已用显式 `result: Any` + 说明修掉（标注这是类型系统的真实边界，
  而不是用 `type: ignore` 把它藏起来）。
  ——这条本身就是"纳入范围有实际价值"的证据：问题此前一直存在，只是没人查。
- 一处**试过但放弃**的改动：曾尝试把根目录加入 ruff 的 `src`
  （想让根级脚本的 import 被正确分类）。实测会波及全仓库 ——
  根目录被当成第三方包，`tests/` 下几十个文件的 import 分类同时失效（I001 一片红）。
  已回滚，`src` 保持 `["pm", "tests"]`。**这类"看起来更规范"的改动要先量影响面**。
- 测试：+3 条（`test_packaging.py`）。
- **验证**：
  | 项 | 结果 |
  |---|---|
  | 全量 pytest | **1064 条 rc=0** |
  | `pm/` 覆盖率 | 92.71%（地板 92） |
  | modelhub 分项 | 86%（地板 82） |
  | ruff check / format / mypy strict | 全绿（**129 个文件 / 65 个源文件**，含新纳入的） |
  | pre-commit 三个钩子实跑 | 全 Passed（不只是配置存在） |
  | 三套 releases | 18 / 46 全绿，运营数据未写脏 |
  | `--selftest` / `run.py gate` / `eval_prompts.py` | 通过（eval 6/6） |
  | 范围护栏变异 | 5/5 全拦住（含"两边同时删"那种） |
  | 门禁实战 | 植入未使用 import → ruff 拦；类型错误 → mypy 拦；格式 → format 拦 |

## Round 8（2026-10-02，PromptMaster 本产品）—— --live 手动工作流

> 结掉 Round 6 结尾提的"下一步"。

- 新增 `.github/workflows/eval-prompts-live.yml`：`workflow_dispatch` 手动触发，
  跑 `eval_prompts.py --live`（真实调用 + 确定性代码侧校验）。
- **为什么单独一个 workflow**：`--live` 打真实端点、花真钱，绿不绿取决于端点当时的状态。
  放进每次提交的 CI 会有两个后果：端点抖一下就红（红久了没人看）、
  每次 push 都在烧额度。所以结构契约（离线、零成本）进 CI，行为评测留人手点。
- 设计要点：
  - **只有 `workflow_dispatch`**，护栏测试钉住"不许出现 push/pull_request/schedule/release"；
  - `dry_run` 开关：只查密钥/端点/依赖，**不调用模型**（首次点的人不会误花钱）；
  - 前置检查独立成步并输出 `::error::` + 修复指引 —— 让"没配 Secret"与
    "评测没过"在日志里分得开（两者的下一步动作完全不同）；
  - `--preflight` 先逐角色冒烟（花极小钱，确认端点活着再跑完整评测）；
  - `timeout-minutes: 20` + `concurrency` 防并跑抢限流；
  - 密钥走 Secrets，日志只打印**长度**（`${#PM_API_KEY}`），绝不展开值。
- **本轮修掉的一个真实缺陷（自己踩的）**：初版只传全局 `PM_API_KEY/BASE_URL/MODEL`，
  漏了**角色级覆盖**（实测真实部署里 `evaluator_b` 用 glm-5.2、`arbiter` 用 deepseek-v4-flash）。
  而漏配不会报错 —— `pm/llm.py` 的 `PM_{role}_X or 全局` 会静默回退，
  于是"冒烟过了、评测也过了"，但用的不是你以为的模型。
  补齐后又发现自己写出了 typo（`PM_COMPARATOR_B_BASE_URL` 多一个 `_B`）。
  两条护栏就此补上：三处 env 块必须**逐字一致** + 角色名必须**在 `pm.llm` 的角色表里**。
- 测试：+10 条（`test_prompt_eval.py` 到 32 条）：manual-only、ci.yml 不许有 --live、
  下拉选项 == TEMPLATES、node 真传给 CLI、dry_run 开关、密钥不落日志、
  超时/并发有界、前置检查独立、三处 env 一致、角色名合法。
- **验证**：
  | 项 | 结果 |
  |---|---|
  | 全量 pytest | **1074 条 rc=0** |
  | `pm/` 覆盖率 | 92.71%（地板 92） |
  | modelhub 分项 / 三套 releases | 86% / 18+46 全绿，数据未写脏 |
  | ruff check / format / mypy strict | 全绿 |
  | pre-commit 三钩子实跑 | 全 Passed |
  | **变异矩阵** | 12/12 全拦住（含"加自动触发器"、"把密钥 echo 出来"、"三处 env 不一致"、typo） |
  | 前置检查 shell 实跑 | 三分支正确（齐全 rc=0；缺配置 rc=1 且**只报长度**） |
  | `--preflight` 实跑 | 8 个角色端点全部可达，密钥自动脱敏 |
  | `eval_prompts --live` 实跑 | clarifier 2/2、mockgen 1/1 通过（真实端点） |
  | stub e2e / selftest / gate / eval 离线 | 全部通过 |

## Round 9（2026-10-02，PromptMaster 本产品）—— Environment 审批（仓库内可做的部分）

> ⚠️ **本轮有一半做不了**：required reviewers 是 **GitHub 仓库设置**（服务端状态），
> 不是仓库文件。本机无 `gh` CLI、无 token 环境变量、无凭据文件，
> 且 `git-remote-https` 缺失（`git ls-remote` 直接 rc=128）—— **没有可用的写权限通道**。
> 所以这一步只能由仓库管理员在网页上完成，本轮交付的是"仓库内能做的那一半"。

- **仓库内可做的部分（已完成）**：
  1. 工作流注释里写清审批**怎么开**（`environment:` 那一行本身不产生任何审批 ——
     审批来自 Environment 设置，这一点不写出来会让人以为已经生效了）；
  2. `docs/operations.md` 补完整配置步骤（Settings → Environments → 新建 → 勾 Required reviewers
     → Save），并说明配好后会看到 "Waiting for review"；
  3. 新增 2 条护栏钉住**名称一致性**：工作流里的 `environment:` 名 == 文档里让人填的名字。
- **为什么名称一致性值得单独钉**：名字拼错时 GitHub 会**静默新建**一个同义不同名的
  Environment（默认没有审批人），工作流照样跑通 —— 你以为存在的审批闸从未存在。
  这是典型的"配置漂了就静默失效"，与这个仓库反复处理过的那类问题同族。
- 护栏强度踩过一次坑：第一版只断言 `env_name in docs`（"出现过就行"），
  变异测试显示**文档里改掉"让人填的那一步"仍然绿**（因为别处还提到过这个名字）。
  已改为断言具体的 `名称填 **\`<name>\`**` 步骤存在 + 工作流那一行被引用到。
- 测试：+2 条（`test_prompt_eval.py` 到 34 条）。
- **验证**：全量 **1076 条 rc=0**、覆盖率 92.71%、modelhub 86%、三套 releases 全绿、
  ruff/format/mypy strict 全绿、**变异矩阵 8/8 拦住**（含前一轮漏网的两种）。

## Round 9（2026-10-02，PromptMaster 本产品）—— Environment 审批（仓库内可做的部分）

> ⚠️ **本轮有一半做不了**：required reviewers 是 **GitHub 仓库设置**（服务端状态），
> 不是仓库文件。本机无 `gh` CLI、无 token 环境变量、无凭据文件，
> 且 `git-remote-https` 缺失（`git ls-remote` 直接 rc=128）—— **没有可用的写权限通道**。
> 所以这一步只能由仓库管理员在网页上完成，本轮交付的是"仓库内能做的那一半"。

- **仓库内可做的部分（已完成）**：
  1. 工作流注释里写清审批**怎么开**（`environment:` 那一行本身不产生任何审批 ——
     审批来自 Environment 设置，这一点不写出来会让人以为已经生效了）；
  2. `docs/operations.md` 补完整配置步骤（Settings → Environments → 新建 → 勾 Required reviewers
     → Save），并说明配好后会看到 "Waiting for review"；
  3. 新增 2 条护栏钉住**名称一致性**：工作流里的 `environment:` 名 == 文档里让人填的名字。
- **为什么名称一致性值得单独钉**：名字拼错时 GitHub 会**静默新建**一个同义不同名的
  Environment（默认没有审批人），工作流照样跑通 —— 你以为存在的审批闸从未存在。
  这是典型的"配置漂了就静默失效"，与这个仓库反复处理过的那类问题同族。
- 护栏强度踩过一次坑：第一版只断言"名字在文档里出现过"，变异测试显示
  **文档里改掉"让人填的那一步"仍然绿**（因为别处还提到过这个名字）。
  已改为断言具体的"名称填 X"步骤存在 + 工作流那一行被引用到。
- 测试：+2 条（`test_prompt_eval.py` 到 34 条）。
- **验证**：全量 **1076 条 rc=0**、覆盖率 92.71%、modelhub 86%、三套 releases 全绿、
  ruff/format/mypy strict 全绿、**变异矩阵 8/8 拦住**（含前一轮漏网的两种）。

## Round 10（2026-10-02，PromptMaster 本产品）—— 按覆盖率数据补测试

> 承接 Round 9。方法：先跑一次全量覆盖率，**按数据**挑薄弱点，而不是凭感觉挑。

- 覆盖率轨迹：93.30% → **93.83%**（本轮 +0.53pp）；测试 1097 → **1115 条**。
- 补的四处（都是"真实用户可见分支"，不是为凑数字）：
  1. **`pm/cli/main.py` 82%（73%→82%）**：十个子命令的分发路由（`diff`/`gate` 是
     我这两轮新加的，接进分发后**没人逐个走过**）。用替身记录调用，钉住
     "每个 `_SUBCOMMANDS` 成员都路由到真实实现" + "submit/status/report/wait
     四个共用 agent 分支"；另补入参护栏（`--cases` / `--max-iter` 越界、需求长度
     上下限），这些都是**花钱之前**该拦住的东西。
  2. **`pm/cli/console.py` 与 `pm/cli/__init__.py`（从榜上消失）**：装包态入口
     `prompt-master` 的转发与**引导顺序**（`prepare_console` 必须早于 `main`）；
     以及包级 `__getattr__` 的纠错分支 —— 误用 `from pm.cli import main` 时
     必须给出可执行的纠正（那个"看导入顺序定含义"的惰性导出是真实踩过的坑）。
  3. **`--dry-run` 的金额外推**：我上一轮加的能力，**此前无覆盖**。
     三条：没配单价时**不许**出现金额（编一个"看起来合理"的数比不显示危险）；
     有历史台账时按实测均价外推且区间线性；历史文件损坏时**不许**把
     `--dry-run` 整体搞崩（它是提交前的预算入口，崩了会逼人绕过它）。
  4. **`gate` / `diff` 子命令的 CLI 出口分支**：门禁自身的 JSON 与文本出口
     必须能区分"门禁坏了"与"提示词有回归"（两者下一步动作完全不同）；
     `diff` 的"已命中规则上加重"渲染、截断提示、来源解析失败提示。

- **排查踩的坑（记下来免得重犯）**：有一条测试一直红，我依次排除了
  pytest 缓存、路径含中文括号、`conftest` 注入、git 嵌套仓库，
  最后发现是**我自己把 `_py_const(body, name)` 的参数写反了**
  （写成了 `_py_const("OLD_SYS", "旧模板正文")`，于是生成了
  `旧模板正文 = "OLD_SYS"`，不是大写常量、抽不到）。
  排查过程中我**没有**去改产品代码 —— 每排除一个假设，都先确认"产品行为是否真的错"。
  这正是本项目对"先证明缺口真实存在，再动手"的一贯要求。

- **验证**：
  | 项 | 结果 |
  |---|---|
  | 全量 pytest | **1115 条 rc=0** |
  | `pm/` 覆盖率 | **93.83%**（地板 92） |
  | `pm/cli/main.py` | 73% → 82% |
  | modelhub 分项 / 三套 releases | 86% / 18+46 全绿，运营数据未写脏 |
  | ruff check / format / mypy strict | 全绿 |
  | pre-commit 三钩子实跑 | 全 Passed |
  | selftest / gate / eval 离线 / stub e2e | 全部通过 |

- 剩余薄弱点（**记录但不追**，理由各自不同）：
  - `pm/modelhub/server.py` 80% 与 `pool.py` 85%：缺口集中在真上游调用体，
    要活 socket 才走得到（`docs/operations.md` 已有同样的结论）。
  - `pm/calibration.py` 88%、`pm/cli/calibrate.py` 88%：缺口是异常路径与聚合分支，
    补它们需要构造人工锚点账本，收益低于上面那四处。
  - `pm/graph.py` 88%：缺口是 `build_app(sqlite_path=...)` 与 `mermaid()` 异常分支。

## Round 11（2026-10-02，PromptMaster 本产品）—— graph.py 补齐到 100%

> 承接 Round 10 末尾列的剩余薄弱点，挑**成本最低、风险最实**的一处做掉。

- **`pm/graph.py` 88% → 96% → 100%**；全量覆盖率 93.83% → **93.91%**；测试 1115 → **1125 条**。
- 补的三处（每处都对应一条被承诺但没验证过的行为）：
  1. **`build_app(sqlite_path=...)`（138-143 行）**：`docs/agent-cli-guide.md` 明确写着
     "带 `--checkpoint` 可跨进程断点续跑"，而这个分支**从未被执行过** —— 承诺从没被验证。
     测试用**新建第二个 app 实例读回同一份 SQLite** 来证明持久化真的发生，
     而不是只断言"函数没抛异常"（后者对"能不能续跑"毫无信息量）。
     另配两条：不传路径时**不落盘**（别指望跨进程）、路径不可写时**报错而不是静默退化**
     （静默退化比报错危险：调用方以为开了恢复，等真要续跑才发现状态是空的）。
     第三条断言的是**具体异常类型**（`sqlite3.OperationalError`，先实测拿到），
     这样将来若有人改成"捕获后静默退化"，这条会红。
  2. **`mermaid()` 失败兜底（153-155 行）**：`--mermaid` 是文档里的"看拓扑"入口，
     挂在 langgraph 的 `draw_mermaid()` 上；拿不到时应返回空串 + 告警日志，
     而不是让一个"看一眼图"的动作抛异常。配了 happy path 与**真实构建**三条，
     防止"永远返回空串"也算通过。
  3. **`route_after_clarify` 的 `needs_reanalysis` 分支（45 行）**：收到用户回答后
     必须重回 clarify 重分析。漏了它，用户的答案永远进不了优化器 ——
     `docs/design-notes.md` §二.1/§二.3 记的就是这类事故。
     另加一条**优先级**用例：`needs_reanalysis` 必须赢过"看起来已经清晰"
     （重分析期间 `clarification` 可能还留着上一轮的 `is_clear=True`，先判清晰就会跳过重分析）。
- 本轮踩的两个小坑（都是自己写出来的，记下来免得重犯）：
  - 又一次在断言消息里用了中文引号嵌套 `"..."`（本轮第二次），改成 `「」`；
  - 函数内 import 触发 ruff 的 I001，提到文件顶部；
  - 裸 `except Exception` 断言被 ruff 的 B017 拦下 —— 改成实测的具体异常类型，
    反而让断言更强（这也是 ruff 这次给出的建议确实比原写法好）。
- **验证**：
  | 项 | 结果 |
  |---|---|
  | 全量 pytest | **1125 条 rc=0** |
  | `pm/graph.py` | **100%** |
  | `pm/` 覆盖率 | **93.91%**（地板 92） |
  | modelhub 分项 / 三套 releases | 86% / 18+46 全绿，运营数据未写脏 |
  | ruff check / format / mypy strict | 全绿 |
  | pre-commit 三钩子实跑 | 全 Passed |
  | selftest / gate / eval 离线 / `--mermaid` / stub e2e | 全部通过 |

- 剩余薄弱点（更新后）：
  - `pm/modelhub/server.py` 80%（78 行）、`pool.py` 85%（45 行）：真上游调用体，要活 socket。
  - `pm/calibration.py` / `pm/cli/calibrate.py` 88%：异常路径 + 聚合分支，要构造锚点账本。
  - `pm/mcp_server.py` 89%（13 行）：错误分支与长任务转发。
  - `pm/cli/library.py` 84%（22 行）：导出与推荐分支。

## Round 12（2026-10-02，PromptMaster 本产品）—— mcp_server.py 补齐到 100%

> 承接 Round 11 末尾的剩余清单，挑了一处（成本低、且是**对外接口面**）。

- **`pm/mcp_server.py` 89% → 99% → 100%**；全量覆盖率 93.91% → **94.11%**；测试 1125 → **1133 条**。
- 补的四处：
  1. **`prompt_check`（97-98）**：把正文原样传给 `run.py check --prompt ...`。
     钉住"正文以 `-` 开头也不会被 argparse 当选项吃掉"（`--prompt` 显式在前）。
  2. **`prompt_diff`（114-122）**：`diff` 的接口是文件路径，所以必须中转落盘。
     钉三件事：① 两侧写进**不同**文件（写同一个会互相覆盖）；
     ② 用 `tempfile` 且落在系统临时目录下（不是往仓库里写）；
     ③ **顺序不颠倒** —— 这条单独用"真跑 subprocess 层"验证，
     因为 a/b 写反会让 diff 结论整个反向（把"引入回归"报成"修好了"），
     而只验参数拼装是看不出来的。另配中文/emoji 往返（Windows GBK 码页下漏 `encoding` 会被静默替换）。
  3. **`optimize_wait` 超时分支（280）**：等不到终态时必须带 `error: 等待超时`、且**不带** report。
     静默当成功会让调用方拿到 `report: ""` —— 看起来像"跑完了但报告为空"，
     实际是"还没跑完"，两者的下一步动作完全不同。配一条终态用例防止"永远报超时"也算通过。
  4. **模块入口（308）**：`python -m pm.mcp_server` 是 MCP 客户端的**真实挂载方式**
     （`{"command":"python","args":["-m","pm.mcp_server"]}`），这一行此前零覆盖 ——
     也就是"客户端照文档配置能不能连上"从没被验证过。
     测试在子进程里**真启动**它，发一条标准的 MCP `initialize` 请求，
     校验响应里有 `serverInfo.name` —— 同时证明传输方式、帧格式、服务名都对得上。
- **一个必须记下来的技术坑**：入口那行**不能**在进程内用 `runpy` 执行。
  FastMCP 起 stdio 时会接管/关闭 stdout，进程内执行会把**测试进程自己的 stdout**
  弄坏（实测 `ValueError: I/O operation on closed file`）。
  而且 `runpy.run_module` 对"已 import 过的包内模块"会报
  `found in sys.modules after import of package`。
  两个坑叠加，结论是：**只能用子进程隔离**——这也是那条测试的真实形态。
- 308 行按仓库既有惯例（`pm/cli/console.py:28`）标 `# pragma: no cover`，
  但注释里写清**不统计是工具限制、不是没人测**：子进程探针确实走到了它。
- 本轮踩的小坑：heredoc 里的 `\n` 又一次被展开成真实换行（第三次），
  改用 `edit_file` 直接改；断言写得过严（`TemporaryDirectory()` 会在系统临时目录下
  再建子目录，我第一版断言"等于系统临时目录"把正确实现判红了）。
- **验证**：
  | 项 | 结果 |
  |---|---|
  | 全量 pytest | **1133 条 rc=0** |
  | `pm/mcp_server.py` | **100%** |
  | `pm/` 覆盖率 | **94.11%**（地板 92） |
  | modelhub 分项 / 三套 releases | 86% / 18+46 全绿，运营数据未写脏 |
  | ruff check / format / mypy strict | 全绿 |
  | pre-commit 三钩子实跑 | 全 Passed |
  | selftest / gate / eval 离线 / stub e2e | 全部通过 |

- 剩余薄弱点（更新后）：
  - `pm/modelhub/server.py` 80%（78 行）、`pool.py` 85%（45 行）：真上游调用体，要活 socket。
  - `pm/calibration.py` / `pm/cli/calibrate.py` 88%：异常路径 + 聚合分支，要构造锚点账本。
  - `pm/cli/library.py` 84%（22 行）：导出与推荐分支，纯本地逻辑、无外部依赖 —— **下一个性价比最高**。

## Round 13（2026-10-02，PromptMaster 本产品）—— cli/library.py 补齐到 100%

> 承接 Round 12 的剩余清单，挑的是性价比最高的一处（纯本地逻辑、无外部依赖）。

- **`pm/cli/library.py` 84% → 100%**（97 → 98 → 99 → 100 逐级推进）；
  全量覆盖率 94.11% → **94.40%**；测试 1133 → **1150 条**。

### 本轮修掉一个真实缺陷（不是为覆盖率造的测试）

`library` 的三个扫描点（`--recommend` / `--export` / 列表）各自写着
`except (OSError, json.JSONDecodeError): continue` —— 只防住"文件读不了"与
"JSON 不合法"，**没防"JSON 合法但顶层不是对象"**。

实测：目录里放一个 `["数组不是对象"]`，`run.py library` 直接崩在
`AttributeError: 'list' object has no attribute 'get'`。
而仓库文档对这类扫描的纪律是明确的："不能让一颗坏牙毁掉整份体检"
（`pm/cli/history.py` 是同一场景）。

修法：抽出共用的 `_load_run_file()`，三种坏法（读不了 / JSON 非法 / 顶层不是对象）
收在一个判据里，另补 `UnicodeDecodeError`（非 UTF-8 字节）。三处调用点统一改用它。
**写成函数而不是三处各补一句**：同一段"怎么算坏文件"抄三遍，
下次有人加第四种坏法必然只改一处 —— 这正是本仓库反复处理过的那类漂移。

### 补的测试（覆盖四类此前没走到的东西）

1. **文本出口**（`--recommend` / 列表不带 `--json`）：此前只测了 JSON 出口，
   而文本出口才是**人手动跑**时看到的 —— 只测 JSON = 只测了一半用户。
   含 Δ 基线为 `None` 时的 `-` 分支（没有基线臂的运行就是 None，最容易崩）。
2. **空结果分支**：无相似资产 / 空资产库时走的是**提前返回**（给一句可执行的结论），
   而不是打印一个 `top-0:` 或 `0 条:` 的空标题 —— 后者会被读成"命令坏了"。
3. **三处坏文件跳过** + `_load_run_file` 的表驱动单测（6 种形态）。
4. **入口分发**：`library --recommend` / `--export` 必须经子命令入口走到实现 ——
   此前所有测试都直接调内部函数，入口那一行零覆盖。
   它坏了的话，用户敲 `--recommend` 会**静默落到默认列表分支**（看到资产列表而非推荐），
   且不报任何错。

### 一个值得记下的排查过程

覆盖率卡在 99% 不动，那一行是 `--export` 扫描里的 `continue`（坏文件跳过）。
我先确认测试确实放了坏文件，再用 `sys.settrace` 直接观察哪几行被执行：
发现 `_load_run_file(f)` 与 `if d is None:` 都执行了，**只有 `continue` 没执行**。

根因：扫描按 mtime **倒序**，且命中目标 `run_id` 就 `break`。
我的坏文件创建得早、mtime 更旧，排在正常文件**之后** ——
扫描先撞上正常文件直接跳出，跳过分支永远走不到。
**这是测试夹具的缺陷，不是产品问题**：修法是在放坏文件前 `sleep` 一下让它排到前面，
并把原因写进注释（否则下一个人会以同样的方式把这条测试"改回"无效）。

- **验证**：
  | 项 | 结果 |
  |---|---|
  | 全量 pytest | **1150 条 rc=0** |
  | `pm/cli/library.py` | **100%** |
  | `pm/` 覆盖率 | **94.40%**（地板 92） |
  | modelhub 分项 / 三套 releases | 86% / 18+46 全绿，运营数据未写脏 |
  | ruff check / format / mypy strict | 全绿 |
  | pre-commit 三钩子实跑 | 全 Passed |
  | selftest / gate / eval 离线 / `library` / stub e2e | 全部通过 |

- 剩余薄弱点（更新后）：
  - `pm/modelhub/server.py` 80%（78 行）、`pool.py` 85%（45 行）：真上游调用体，**要活 socket** ——
    这两处是当前唯一"不搭假上游就补不动"的，建议保持现状（文档已记录同结论）。
  - `pm/calibration.py` 88%（48 行）、`pm/cli/calibrate.py` 88%（28 行）：
    异常路径 + 聚合分支，要构造人工锚点账本。
  - `pm/cli/calibrate.py` 是剩下的「纯本地、可补」的那一处，**下一个性价比最高**。

## Round 14（2026-10-02，PromptMaster 本产品）—— cli/calibrate.py 补齐到 100%

> 用户指名要"构造人工锚点账本"。**主要精力就花在这条真实链路上**，
> 它也正是评委校准的可信输入端。

- **`pm/cli/calibrate.py` 88% → 100%**；全量覆盖率 94.40% → **94.77%**；测试 1150 → **1179 条**。

### 主体：人工锚点账本的构造链路（此前整段零覆盖）

`--make-form` → 人填分 → `--apply-scores` 是"造一批人工锚点"的唯一入口。
它坏了，评委校准就没有可信的输入端 —— 而校准的全部结论（MAE / 偏差 / 判定一致率 /
κ）都建立在人工分之上。

覆盖的资源：
1. **完整往返**：摊表 → 填分（含小数）→ 回填 → 断言锚点文件里 `human_score` 与
   `confirmed=true` 都正确落地。
2. **表可重复生成而不丢已填的分**（否则每加一个锚点就得重填一遍）。
3. **`_comment` 分隔条目不进表**（它们没有 id）。
4. **失败关闭语义**（这个方法的核心承诺）：非数字 / 越界 / 重复 id → **一条都不写**，
   且断言文件字节级未变。写一半会留下"部分确认"的中间态，下一次校准的分母就变了。
5. **混合场景**：部分 id 对不上时照常写入对得上的，但对不上的要显式告警
   （静默忽略会让人以为全填上了）。
6. 缺列 / 空表 / 非数组 / 无可摊条目 / 路径读不到 → 各自的 rc 与可读提示。

另补的零散分支：`--aggregate` 的人读出口与账本损坏、待确认锚点溯源行、
口径不同不当作可比基线、账本损坏降级为空账本继续。

### 如实记下本轮的返工（同一处栽了三次）

这轮我在**测试夹具**上反复出错，值得记下来：

1. `_analysis` 夹具漏了 `r` 这个必需键 → `KeyError: 'r'`；
2. `calibrate` 的真实签名是 `(samples, role, mode)` 且**只返回 2 元组**
   —— 我按 3 元组写把 drift 塞进返回值，直接 `ValueError`；
3. `load_samples` 会校验 required 六项字段，我的样本太简 → 根本没走到目标分支；
4. `--aggregate` 的账本记录里 `items` 每条要带 `id`（聚合按锚点聚类）→ `KeyError: 'id'`。

**这四条都不是产品缺陷，是我的夹具与真实契约不符。** 我一度引入了两条
会让测试套件变红的坏测试，随后**主动删掉重建**，没有把"能跑但断言着错东西"的测试留在仓库里。
教训：**先用现成测试的夹具形态，别自己凭印象造** —— 既有测试用的
`{"n":5,"bias":0.2,"mae":0.3,"r":0.9}` 就是正确的形状，我一开始没照抄。

另外移除了两个在返工中产生的**死代码**辅助函数（`_patch_calib_full` / `_analysis`）——
留着会误导下一个人，而 ruff 的 RUF100 也正好指出了其中一个的多余 `noqa`。

- **验证**：
  | 项 | 结果 |
  |---|---|
  | 全量 pytest | **1179 条 rc=0** |
  | `pm/cli/calibrate.py` | **100%** |
  | `pm/` 覆盖率 | **94.77%**（地板 92） |
  | modelhub 分项 / 三套 releases | 86% / 18+46 全绿，运营数据未写脏 |
  | ruff check / format / mypy strict | 全绿 |
  | pre-commit 三钩子实跑 | 全 Passed |
  | selftest / gate / eval / `calibrate --aggregate` / stub e2e | 全部通过 |

- 剩余薄弱点：**只剩 `pm/modelhub/server.py` 80%（78 行）与 `pool.py` 85%（45 行）**。
  这两处的缺口集中在真上游调用体，**要活 socket 才走得到** ——
  建议保持现状（`docs/operations.md` 里早已记录同结论）。
  除它们之外，`pm/` 已无低于 90% 的文件。

## Round 15（2026-10-02，PromptMaster 本产品）—— 薄弱点清单持久化进文档

- 请求：把 `modelhub/server.py` 的未覆盖分支持久化记录到 `docs/operations.md` 的已知薄弱点清单。
- 做法：**先实测再写**，没有凭印象列。核验时抓出一处真实不准确 —— 见下。
- 交付：`docs/operations.md` 里那一行简短提及，扩成完整的**已知薄弱点清单**，
  含三张分区表（A `modelhub/server.py` / B `modelhub/pool.py` / C 其余 `pm/`），
  每块写明「未覆盖行 / 属于哪一类 / 补它的前提是什么」。

### 核验抓出的问题（第一版文档写错了）

我第一版按"看起来连续"圈了区间（如 `154-164`、`507-520`），
用脚本对着 `--cov-report=term-missing` 逐块核对后发现：**有 19 行其实已被覆盖**
（如 `154-164` 里的 `156/157/162/163`、`507-520` 里的 `510-512/514-519`）。

于是把清单改成**只列真正未覆盖的行**（`154/155`、`158-161`、`164` 这种精确写法），
并在文档里写明"行号是实测快照，会随代码移动，照它补之前先重测"。
**这是这轮最有价值的部分**：一份行号不准的清单，会让人照着一份废数据去补覆盖率。

### 一个被纠正的误判

原先文档（与本轮我自己的初稿）都把 `server.py` 的缺口笼统写成"对话主路由的上游调用体，
要活上游才走得到"。逐块核过后发现**这个归类不成立**：

| 性质 | 未覆盖行 | 说明 |
|---|---|---|
| **纯逻辑（假上游/无上游即可）** | 15 + 9 + 9 = **约 33 行** | 鉴权与头解析（现有测试走 `X-API-Key` 头，没走 `Bearer`）、角色与参数拼装、SSE 帧解析与心跳 |
| 半真（假上游可补） | 25 + 11 行 | 对话主路由成功路径、流式尝试链错误分支 |
| **真上游条件** | 84 行 | 流式降级兜底（全模型 `stream=true` 失败 → 非流式 + 合成 SSE） |

笼统归类会让下一个人**直接把本可以补的 33 行也跳过**。文档里现在明确写出了这个区分。

### 附带的护栏（+2 条测试）

- `test_operations_lists_the_known_coverage_gaps`：清单被删 / 丢掉复现命令 /
  丢掉"纯逻辑 vs 真上游"的区分时变红。
- `test_coverage_gap_doc_lines_still_look_like_a_real_file`：
  清单点名的行号必须仍落在对应文件的真实范围里（真漂了会红，提醒重测；
  刻意**不**检查行号准不准 —— 那要重跑覆盖率，代价高且本机有间歇性假红）。

- **验证**：
  | 项 | 结果 |
  |---|---|
  | 全量 pytest | **1181 条 rc=0** |
  | `pm/` 覆盖率 | 94.77%（地板 92）；modelhub 分项 86%（地板 82） |
  | 三套 releases / 运营数据 | 18+46 全绿，未写脏 |
  | ruff check / format / mypy strict | 全绿 |
  | pre-commit 三钩子实跑 | 全 Passed |
  | selftest / gate / eval / stub e2e | 全部通过 |

- 结论：**覆盖率补强这条线到此收尾**。剩下能补的（约 33 行纯逻辑）已在清单里标明前提，
  其余（84+11+37 行）要搭"会按需失败的上游桩"，属独立工程 ——
  建议改到那几段代码时顺手补，而不是为数字专门去做。

## Round 16（2026-10-02，PromptMaster 本产品）—— 纯逻辑缺口清零（33 行 → 0）

- 请求：给三组纯逻辑分支（Bearer 鉴权 / 角色参数拼装 / SSE 帧解析与心跳）补假上游桩，
  再重跑覆盖率看收敛到多少。
- **结果：33 行纯逻辑缺口 → 0**；`modelhub/server.py` 80% → **88%**；
  modelhub 分项 86% → **89%**；`pm/` 全量 94.77% → **95.19%**；测试 1181 → **1198 条**。

### 三组各自怎么补的（复用既有范式，没另起一套）

| 组 | 补法 |
|---|---|
| **SSE 帧解析与心跳**（9 行） | 在 `tests/test_modelhub_stream_contract.py` 里复用既有的 `_FakeHub` + 桩 `stream_upstream` 范式。覆盖：首帧正文必须计入 `content_chars`（这是修过的真缺陷）、注释/`[DONE]` 帧不计、坏 JSON 帧按 0 计并继续转发、`ERR::` 哨兵抛异常而普通 str 帧丢弃 |
| **鉴权与头解析**（15 行） | 在 `tests/test_modelhub_admin_routes.py` 里复用既有 `gateway` 夹具。覆盖：`Bearer` 管理员口令走对话路由、开放模式无凭据读 `/v1/models`、池配置坏掉时 503（不是 500）、以及 `/v1/models` 与 `/v1/agents` 的**降级兜底**（`_chat_auth` 因无效 vk- 抛 401 → 退到 `_admin_auth`） |
| **角色与参数拼装**（9 行） | 新加 `gateway_like` 夹具（只要环境、不建 TestClient —— 这几条测的是纯函数）。覆盖：`_params_of` 只透传显式设过的字段（**0.0 / 1 不能被真假判断吃掉**）、system prompt 前置注入、调用方已带 system 时不重复注入、未知角色 422 |

### 顺带修掉两处真实问题

1. **`server.py` 里的重复判断**：`if chunk is None: break` 连写了两遍（`510-513`）。
   静止代码，还让覆盖率永远差一行。已删。
2. **心跳间隔原本无法被测试触发**：`HEARTBEAT = 15.0` 是函数内硬编码，15s 的等待
   让任何单测都不可接受 —— 这就是心跳路径长期是缺口的原因。
   已改为读 `PMH_HEARTBEAT_SECONDS`（与 `PMH_TIMEOUT` / `PMH_BREAK_THRESHOLD` 同族惯例），
   测试把它调到 0.05s 就能真实触发心跳。

### 本轮踩的坑（记下来）

- bytes 字面量不能含中文（`b"...中文..."` 是 SyntaxError），必须 `json.dumps(...).encode()`；
- HTTP 头不能含中文（httpx 按 ascii 编码），无效 vk 要用 ASCII；
- `get_agent_store` 在 `vkeys.py` 而非 `agents.py`；方法是 `register(name, *, default_role=)`
  而非 `put` —— **试错两轮后改成"先查签名再写"**，这才是这个仓库一直强调的做法。

### 一次与改动无关的红

全量首跑时 `tests/test_cache_branches.py::test_update_existing_key_is_persisted` 红了一次，
单独跑与连跑 5 次全过、且 `pm/cache.py` 本轮未改动 —— 属文档里记录的**间歇性红**
（`WinError 10053` 类）。已记在此处，不当作本轮缺陷。

- **验证**：
  | 项 | 结果 |
  |---|---|
  | 全量 pytest | **1198 条 rc=0** |
  | `pm/modelhub/server.py` | 80% → **88%**（纯逻辑缺口 33 → 0） |
  | `pm/` 全量 / 去掉 modelhub / modelhub 分项 | **95.19% / 96% / 89%**（地板 92 / 82） |
  | ruff check / format / mypy strict | 全绿 |
  | 三套 releases / 运营数据 | 18+46 全绿，未写脏 |
  | pre-commit 三钩子实跑 | 全 Passed |
  | selftest / gate / eval / stub e2e | 全部通过 |

- 剩余（已在 `docs/operations.md` 的清单里更新为收敛后的数字）：
  全是"**要搭会按需失败/按需拒绝的上游桩**"那类 —— `server.py` 的流式降级块（84 行）
  与尝试链（11 行）、非流式成功路径（25+3 行）、`pool.py` 的真 urllib 段（37 行）。
  建议改到那几段代码时顺手补，不为数字专门做。

## Round 17（2026-10-02，PromptMaster 本产品）—— 降级兜底桩：server.py 补到 100%

- 请求：把「降级兜底 84 行」的桩补上，看 modelhub 分项能推到多少。
- **结果：`pm/modelhub/server.py` 88% → 100%**；
  **modelhub 分项 89% → 93%**；`pm/` 全量 95.19% → **95.80%**；测试 1198 → **1216 条**。

### 降级兜底（84 行）是什么、怎么补的

触发条件：`attempt_stream()` 抛 `ModelPoolExhaustedError`（所有候选**流式**都失败）——
真实场景是上游对 `stream=true` 做**能力性拒绝**（实测商汤 Token Plan 同一时刻非流式正常、
流式 401）。这时对客户端正确的做法不是报错，而是改走非流式 + 把整段正文**合成一套合法 SSE**。

补法：复用既有 `_FakeHub`（给它加 `chat`，默认抛池耗尽 → 既有 14 处用法行为不变），
7 条用例覆盖三条支路：
1. **兜底成功**：帧序 `role → 正文 → degraded 说明 → [DONE]`、每帧 bytes、
   `degraded: true` 必须在场（不标注的话客户端的"首字延迟"指标会被静默污染）、
   且确实走的是 `stream=False`；
2. **兜底也耗尽** → 502 + 台账记 `stream+fallback both failed`（否则运维查不出是两段都不通）；
3. **兜底抛 GatewayError** → 用**上游状态码**（≥400 才用，否则 502）——
   一律 502 会把可自纠的 4xx 也说成网关故障。

### 顺手把对话路由的剩余分支也补完了

同一轮里用 `gateway` 夹具 + 桩 `_hub_instance` 补了：
- 非流式**成功路径**（`234-258`）：响应体必须是标准 `chat.completion` 形状
  （OpenAI SDK / LangChain 按 `choices[0].message.content` 取值）、
  `usage` 透传、落用量库；
- **vk 归属覆盖**（`225`）：`vk-` 密钥的 agent 必须覆盖请求体里声明的 agent（防伪造归属）；
- **auth 失败重抛**（`220-221`）：那两行看着像空操作，但没有它 401 会被吞成 500，
  调用方连换钥逻辑都触发不了；
- 错误映射：`ConfigError` → 503、`GatewayError` → 上游状态码或 502。

### 本轮踩的坑（都是"没先查就写"）

1. 测试里自己 monkeypatch `stream_upstream` **被 `_call` 内部覆盖** ——
   该函数的唯一注入点是它的 `upstream` 回调参数；
2. `record_call` 写的是**用量库**（`usage_store.py`，当日聚合），不是台账 `ledger` ——
   第一版去 `/v1/ledger` 找，什么也找不到；出口是 `/v1/usage`；
3. `_ok_result` 桩漏了 `request_id`（server 侧直接索引它 → KeyError）；
4. `_stream_response` 的两条"兜底也失败"路径是**同步 raise** 的（`attempt_stream()`
   在函数体内就跑完），不是等迭代响应体时才抛 —— 断言抛点写错了一轮。

### 我的护栏误红了一次（按意图而非措辞修正）

上一轮加的 `test_operations_lists_the_known_coverage_gaps` 要求文档里出现
"纯逻辑"与"真上游"字样。本轮 server.py 补到 100% 后，清单的分类表述被收敛掉，
护栏就红了 —— 而**内容其实更准确了**。
已把判据改成"必须说明补各项的前提/成本"（意图），不绑定具体措辞。
**固守措辞的护栏会在内容变好时误红**，这比不设护栏更坏。

- **验证**：
  | 项 | 结果 |
  |---|---|
  | 全量 pytest | **1216 条 rc=0** |
  | `pm/modelhub/server.py` | **100%**（降级兜底 84 行全覆盖） |
  | `pm/` 全量 / 去掉 modelhub / modelhub 分项 | **95.80% / 96% / 93%**（地板 92 / 82） |
  | ruff check / format / mypy strict | 全绿 |
  | 三套 releases / 运营数据 | 18+46 全绿，未写脏 |
  | pre-commit 三钩子实跑 | 全 Passed |
  | selftest / gate / eval / stub e2e | 全部通过 |

- **剩余：`pm/` 里只剩 `pm/modelhub/pool.py` 一处**（85%，45 条语句）。
  其主体是 `331-367` 的**真 `urllib` 请求段** —— 要活 socket 或"会按需失败的 socket 桩"。
  不建议为覆盖率数字专门做，改到那段代码时顺手补即可。已更新进 `docs/operations.md` 的清单。

---

## Round 18（2026-10-03，PromptMaster 本产品）—— 卡死机：把看不见的重试层数出来并钉住

- 请求：「卡死机全方面的修复」。指 2026-10-02 真端点轮量到的"单个角色调用最坏挂 45 分钟"。

### 先公开撤回一条我自己写进文档的错归因

Round 17 前后我记下的是：`pm/llm.py` 的 `_TRANSIENT_CONN_HINTS` 含 `readtimeout` / `read timeout`
⇒ `OpenAITimeoutError` 被归类成瞬时连接故障 ⇒ 进 `_invoke_with_conn_retry` 原地重试 3 发 × 300s = 900s。
**这条是错的。**本地装死端点实测（`scripts/measure_retry_layering.py`，timeout=1s、端点每发挂 3s）：

- 真实异常文本是 `OpenAITimeoutError: Request timed out.`，**两条 hint 都命中不了它**
  ⇒ 旧版我方那层对超时的重试次数是 **0**；
- 一次 `invoke()` 端点实收 **3 发**而我方计数只有 1 ⇒ 那 3 倍来自
  **openai SDK 的隐式 `max_retries=2`**（`openai._base_client.DEFAULT_MAX_RETRIES = 2`，
  `langchain_openai.ChatOpenAI` 不显式传就吃这个缺省）。它不看代码想不到、日志里不留痕。

顺带量到分类的另一处死角：5xx 的判定其实靠**响应体子串**——502 蒙对是因为 body 里恰好有
"502 Bad Gateway"，而 `Error code: 503 - {'error': …}` 不含 "503 service" ⇒ **漏判**。

### 差分实测（同一装死端点，只换配置）

| 配置 | SDK 重试 | 连接重试 | 墙钟预算 | 端点实收 | 墙钟 | 折生产口径（PM_TIMEOUT=300） |
|---|---|---|---|---|---|---|
| A 修复前形态 | 2（隐式） | 2 | 无 | **18 发** | 36.98s | **≈11,095s ≈ 3.1 小时** |
| C 只钉住 SDK 层 | 0 | 2 | 无 | 6 发 | 15.11s | ≈4,533s |
| B 现行缺省 | 0 | 2 | 3s（比例尺） | **1 发** | 1.02s | **≈305s** |

**必须同批生效**：C 档 6 发比修复前我方层的 3 发还多——因为超时现在**真的**会被重试了
（旧版不重试是分类漏判的结果，不是设计意图）。只改分类不加预算 = 更慢。

### 改了什么

1. `SDK_RETRIES`（`PM_SDK_RETRIES`，缺省 **0**）：`get_llm()` 对 openai / anthropic 两条构造点都显式传，
   重试决策收回到可见的两层（连接短退避、429 退避+换 Key）。
2. `CALL_BUDGET`（`PM_CALL_BUDGET`，缺省 **600s**）+ `WallBudget`：一次调用（含双通道、全部重试与退避）
   的墙钟上限。**旧版也有 `deadline`，但它只约束"还要不要睡这一觉"** —— 一次挂住不返回的调用它管不到。
   现在两件事都管：每次真实发起前查剩余量；并把这一发的客户端 `timeout` **夹进剩余预算**
   （不夹就是"剩 6s 却发一个 300s 的请求"）。首发只要求"还剩预算"，后续重试额外要求装得下整发。
3. 分类器改**先看状态码**（408/5xx 可重试，429 明确排除走限流通道），文本只作拿不到码时的兜底；
   并按实测文本补上真实超时形态。
4. 通道 B 不再把"传输层已重试满"的故障当解析失败烧名额（与 429 同形处理）；
   `CallBudgetExceeded`（继承 `RuntimeError`，现有兜底接得住）**不进通道 A 的能力缓存**——
   预算见底不是"这个端点不支持结构化输出"，记进去会让后续调用永久跳过通道 A。
5. 失败现场带数字：`{role, budget_s, wall_s, requests, last_error}`，日志每发披露"墙钟已用 Xs"。
   历史台账只记**成功**调用的延迟，卡死那 15 分钟在产物里是隐形的——现在至少报错文本自带秒数。
6. `scripts/measure_retry_layering.py`：把一次性探针做成可重跑命令（零出网，绑 127.0.0.1）。
   缺省预算 600s 的来源写进代码注释：历史 trace 里 >60s 的单调用最慢 **122.2s**，600s 容得下 3~4 次正常尝试。

### 修过程中我自己踩出来的一个坑（已钉成用例）

绝对下限 5s 一开始连**第一发**一起拦掉了 ⇒ 装死端点差分实测到"端点收到 0 发、墙钟 0s，
报的却是预算用尽"——读起来像端点坏了，其实闸门装反。`test_small_budget_still_fires_the_first_attempt` 钉住。
另：本文件的 autouse 夹具把 `time.sleep` 打成 no-op，而装死端点必须**真的**挂住 ⇒ 处理器 import 期绑死
`_REAL_SLEEP`；不然"装死"变成秒回，用例测的是解析失败而不是超时（第一轮全量里就是这样红的）。

### 验证

| 项 | 结果 |
|---|---|
| 全量 pytest（CI 等价，独占） | **rc=0**，活体收集 **1235 条**（+19：`tests/test_call_budget.py`），覆盖率 **95.73%**（地板 92） |
| modelhub 分项线 ≥82 | rc=0 |
| 三套 releases（18 / taskboard / 46）+ 运营数据脏检查 | 全 rc=0，`git diff --exit-code -- releases/*/data/*` 干净 |
| `run.py gate` / `eval_prompts.py` / `--selftest` / `bash examples/run_e2e_stub.sh` | 全 rc=0（stub 那步显式钉 `PM_FORCE_JSON_CHANNEL=0 PM_JUDGE_JITTER=0` 复刻 CI 干净检出） |
| ruff check / ruff format --check / mypy | 全 rc=0（CI 同路径） |
| 变异验证 | M1 SDK 缺省回 2 → **4 条红**（含两条端点级）；M2 停用连接层预算闸 → 1 条红；M3 停用实测超时 hint → **7 条红**；逐个逆操作还原后 rc=0 |

### 未验证（别当事实引用）

- 全程**零真实端点调用**：证据来自本地装死 server 与注入式假 LLM。真实网关上的
  "慢而不挂"分布没重测过，600s 缺省是从历史台账推的，不是真端点实测。
- `PM_TIMEOUT=300` + 预算 600 的组合在生产 `.env` 里未被本轮改动（用户自己的配置未动）。
- 台账只记成功调用这件事没改（改它要动 `data/usage_daily.json` 的 schema 与既有断言），
  本轮只做到"失败自带墙钟读数"。

---

## Round 19（2026-10-03，PromptMaster 本产品）—— 锚点人工分的出处位：把"64 条 bias +3.09"拆成人能复核的两半

- 请求：「继续」。我按上一轮自评里点名的第二条动手（第一条那 34 条人工分只有他能给，第三条落档等他发话）。
  选它的理由：零调用、确定性、不改任何默认——正是被验证过会接受的那一形。

### 缺陷形状

交付报告最承重的一句是"64 条人工锚点、4 轮校准的聚合平均偏差 **+3.09**（95% CI +2.36~+3.82）"。
但 48 条候选里 25 条的人工分不是产品所有者打的，是 AI 按所有者已确认判例外推的
（GLM-5.3，与评委 B glm-5.2 同族）。这件事此前**只**写在提交说明 `8af8c3b` 和
`score_form.csv` 的备注栏里，锚点 JSON 的 `provenance` 只有 `source_run/target_model/judge_score`，
**没有出处位** ⇒ 那句 bias 拆不出"严格人工"那一半。
评委同源（A/B 同模型）早有检测与警示，锚点侧的同源却只是口头承认——不对称。

### 落地

1. `scripts/mark_anchor_provenance.py`：从打分表备注（「AI代判」开头）取 id 集，
   给 `judge_calibration/samples*.json` 盖 `provenance.human_score_source`
   （`owner` / `ai_proxy_glm-5.3`）。可重跑（幂等用例钉住）、`--apply` 才写盘、
   没打分的条目**留空读作 unknown**——把"没标注"默认成"人打的"正是这个洞本身。
   演示数据 `samples.example.json` 整份跳过。写出格式与 `calibrate --apply-scores` 逐字一致，
   免得两个写入点互相搅排版（那会产生"只有缩进变了"的假差异）。
2. `pm/cli/calibrate.py`：回填通道同步盖 `owner`（那条路径只有人真填了分才会走到）。
3. `pm/calibration.py`：`classify_human_source` / `load_human_sources` /
   `split_by_human_source` / `render_human_source_split`；`aggregate_history` 的 pooled
   多带 `by_human_source` + `owner_anchors` + `ai_proxy_anchors`。纯账本重算，零调用。
4. `pm/report.py`：`_human_source_note` —— 只在**代判条数 ≥ 亲判条数**时给披露行加同源提示。
   现状不过半，那句不出现（把警示常驻就是把它用成噪声）。

### 实测读数（`run.py calibrate --aggregate`，impression/evaluator，4 轮 95 配对）

| 分组 | 锚点 | 配对 | bias |
|---|---|---|---|
| 所有者亲判 | 41 | 72 | **+2.90** |
| AI 代判（与评委 B 同族） | 23 | 23 | **+3.67** |
| 合并 | 64 | 95 | +3.09［CI +2.36~+3.82］ |

**方向没变，变的是出处**："评委系统性偏松"不再靠代判那半撑着。
但"代判把 bias 拉高了多少"这个问句**答不了**：0.77 之差小于合并 CI 宽度 1.47，
低于这台仪表的分辨率——这条如实写进 `docs/evaluation.md` §十五 的新小节。

### 验证

| 项 | 结果 |
|---|---|
| 新增用例 | `tests/test_anchor_provenance.py` 11 条（分类表 / 拆分算术 / unknown 不消失 / 真锚点全覆盖 / **代判条数与打分表一致 25** / 真账本分组锚点数之和==合并数 / 渲染两种数 / 过半才警示 / 无组可拆时静默 / 披露行触发条件 / 迁移幂等） |
| 既有相关用例 | `test_cli_agent.py`（回填盖 owner 两条断言）、`test_report.py`、`test_calibrate_judge.py`、`test_calibration_aggregate.py`、`test_round_report.py` 全绿 |
| 数据文件改动性质 | 与 HEAD 做**结构** diff：4 个文件 0 删除、0 修改、只新增 `provenance` 键（11/18/48/8），无空对象残留 |
| 变异 | 「未标注默认成 owner」→ 2 条红；「摘掉 pooled 接线」→ 1 条红（第一次写的变异是 `{} if False else …`，**空转不红**，已换成真变异重测） |
| 静态 | ruff check / ruff format --check / mypy strict 全 rc=0（格式化只落在我自己那 3 个文件上） |

### 未结案 / 别当事实引用

- 全量门禁与覆盖率地板本轮收尾时统一复跑（数字以那一趟为准）。
- 41+23=64 是**最近 4 轮**涉及的锚点并集，不是 48 条全集；早期轮的 demo-* 条目算 unknown。
- `docs/evaluation.md` §十七 仍然不存在（第三轮真端点读数的落档义务没履行）——那是他发话的活，我没动。

## Round 20（2026-10-03，PromptMaster 本产品）—— 第三轮真端点 E2E 收口：原稿改进模式 C1 过 C2 不过 + G1 触发，结论维持「无证据」

接 Round 19 未结案项：`docs/evaluation.md` §十七 本轮已补（判据/读数/未验项分开），
第三轮真端点读数（run `9950147e2e40`，57 调用）完整入档。判据原文
`logs/e2e3_criteria.md`（21:15 写死，早于任何结果读数），跑批产物 `logs/e2e3b_*`。

### 判据对照（一句话版）

- C1 盲评 4/0/0 **过**；C2 Δ+1.69 < 自报噪声带 2.707 **不过** ⇒ 主判据「本轮无证据表明
  比原稿好」；反向判据（原稿胜 ≥3/4）亦不成立。
- G1 **触发**（revise 空返回 2 次 → early_stopped，迭代 0/3）⇒ Δ 不作方向性结论，只算链路验证。
- G2/G3/G4 未触发；G5 未触发停轮（跑批早期 1 次 900s 挂死 + 1 次 429 + 2 次空内容，
  22:20 量化「再挂死即停轮」后未再挂死）。

### 本轮真正证明/证伪的事（详见 §十七）

1. **原稿改进模式（8edcfb7）链路首验通过**：seed_prompt→基线=原稿→Δ 读作「比你自己那版
   好多少」，267→363 字符，报告模式行/体检行/对比表渲染全对。
2. **§十六「空返回=预算不足」归因被证伪一半**：reviser 24000 / evaluator 32000 下照样
   空返回，三角色都在 sensenova-6.8-flash-lite——画像指向端点 reasoning 行为而非预算，
   修法方向是角色选型（换端点/关 thinking），不是继续加预算。
3. **超时叠层 45 分钟最坏形态首次量到**：`_TRANSIENT_CONN_HINTS` 把 OpenAITimeoutError
   当瞬时连接故障（3 发×300s=900s，实测吻合）+ 外层再叠 3 次 ⇒ 单角色最坏 45 分钟静默。
   修复（`PM_CALL_BUDGET` 墙钟 + `PM_SDK_RETRIES=0`）已在工作树，未真端点复测。
4. 跑批期一次归因错误当场纠正留档（「PM_TIMEOUT 显然是 900」实测是 300，判据 amendments
   如实记录）——文档纪律按「错归因必须公开撤回」执行（同 Round 19 先例）。

### 验证

| 项 | 结果 |
|---|---|
| 入档完整性 | §十七（evaluation.md）+ 本节，判据/读数/未验项三分开；负结果未粉饰 |
| Round 19 未结案项 | §十七 落档义务 **已履行**（本轮闭环） |
| 回归（串行） | 主套件 **1246 全绿**（72s，rc=0）+ 三服务 18/13/46 全绿 + ruff/format/mypy 全绿；以本轮收尾时点为准 |

**回归期间的插曲（如实入档，防后人误判）**：收口验证第一轮为省时把「主套件」与
「三服务」两条 pytest **并行**执行，随即 1097→1170 个 ERROR，全部同一根因——
`FileNotFoundError: .pytest_tmp\pytest-of-<用户>\pytest-N`（pytest 9.1.1
`pathlib.py:175`，numbered-dir 清理）。定性：仓库根 conftest 为绕开 `%TEMP%` 坏软链
把 `PYTEST_DEBUG_TEMPROOT` 固定到共享的 `.pytest_tmp/`，两个 pytest 进程并发时
互删对方的 numbered dirs（本机独有组合；CI 每 job 单会话且 temproot 走系统临时目录，
从未复现）。**串行复跑 1246 全绿（rc=0）**，连续 5 次串行全绿 vs 2 次并行全炸，
race 假设成立。处置：本轮不改 conftest（官方口径 CI/本地门禁均为串行，产品代码
零缺陷）——但**本地多开 pytest 属禁忌**，如未来需要并行跑，须先给 temproot 加
进程唯一后缀（conftest 注释第 8 行「由本进程自己创建」的意图保留即可）。
另：并行轮的 rc 曾被管道 `tail` 吃掉显示 0（`EXIT=$?` 取的是 tail 的）——
退出码硬判据必须直接取 pytest 进程自身，验证判据与既有 pipeline-exitcode 教训同源。

### 未结案 / 别当事实引用

- Δ+1.69 不可单独引用（G1）；C1∧C2 同过之前，价值主张维持「流水线跑得通 ≠ 创造了价值」。
- 注入免疫本轮未实测（用例集无 injection 场景）。
- 空返回的角色选型方向未实测；`PM_CALL_BUDGET` 修复未真端点复测。
- G5 污染程度未定量排除（只确认未达停轮线）。
- 工作树未提交的新能力（promptdiff/promptgate/cost 等）不在本轮实测覆盖内。

### 补记（2026-10-03 收口日）：角色选型方向最小实测——初步验证成立

Round 20 未结案第 3 条（空返回角色选型方向）当天做了最小实验（11 锚点 =
samples.json 全量 3 手写 + 8 真实；`--judge evaluator --no-save` 隔离账本；
模型经 `PM_EVALUATOR_MODEL` env 覆盖、.env 零改动；产物 `logs/exp_base_*` /
`logs/exp_alt_*`）：

| 配置 | 结果 |
|---|---|
| 基线 `sensenova-6.8-flash-lite`（现行） | 17 分钟仅推进约 4 条锚点，已 3 次「空内容——reasoning 耗尽」（前台超时树杀中止） |
| 实验 `deepseek-v4-flash`（同端点非 reasoning） | 约 10 分钟跑完 11/11，**空返回 0 次**，MAE **0.70**、bias **+0.03**，超阈仅 1 条且方向正确（real-sales-derived-stats Δ-3.0——§八 点名的无 σ 支撑假断言，评委抓对了） |

**初步结论**（单轮小样本、未入账本）：§十六→§十七 的「空返回=预算不足」归因
链在本实验下进一步收敛为**端点/模型的 reasoning 行为**——换非 reasoning 模型后
空返回清零，且 MAE 从历史聚合 3.13 量级降到 0.70（bias +3.09→+0.03，系统性放水
在读数上消失）。若 evaluator 换 deepseek-v4-flash 落地，可能同时改善第三轮 E2E
的两个根因（G1 空返回早停 + 评委系统性放水）。

**别当事实引用的边界**：11 锚点单轮、`--no-save` 不入漂移账本、rubric 为现行版
（与 §八 历史 0.97 读数的 rubric 已不同，不可直接比）；换 evaluator 是产品级
决策——会改变评估读数坐标系（历史账本/披露行/达标判定口径），需所有者拍板并
重跑校准基线后才可切换。`PM_CALL_BUDGET` 真端点复测仍未做。

## Round 21（2026-10-04，PromptMaster 本产品）—— 工程质量轮：复杂度拆到可评审，并当场挖出两个真缺陷

本轮不动产品行为，只动"代码能不能被 safely 改"。基线（改前独占实测）：
`ruff check` / `ruff format --check` / `mypy --strict` 全 rc=0，1246 条 rc=0，覆盖率 95.65%。
**这三道门当时都是绿的，而下面那两项缺陷就藏在一片绿里** —— 这是本轮的主要发现。

### 挖出来的两个真缺陷（都不是重构的副产品，是重构的副产品也没关系：它们本来就在）

1. **脏 `test_runs` 让整份交付报告崩掉**。`render_report` 里三处读 `test_runs`，只有
   `_infra_invalid_cases` 防了非 dict，另外两处没有。实测三条崩法：
   `test_runs=["x"]` → `AttributeError: 'str' object has no attribute 'get'`；
   `test_runs=None` → `TypeError: 'NoneType' object is not iterable`；
   `test_case_index="abc"` → `ValueError: invalid literal for int()`。
   同文件的 `_report_safe_int` 文档里**早就写着**"state 里的字段类型不可信（旧版
   checkpoint / 外部构造）"，而报告是唯一给读者看的东西 —— 认知在场、执行缺位。
   修法是在读取点单点收口（`_test_runs()`）+ 索引走 `_report_safe_int`。
2. **`pm/nodes/execute.py` 上挂着一条从未生效的 `# noqa` 豁免**：冒号后写的是散文
   （`avoid no-redef——…`）而不是规则码，ruff 把整条指令当无效忽略（只 warn、不报违规）。
   也就是说那个"这里已显式豁免"的注释一直在骗读者。已改成普通注释，并对全仓扫了一遍
   形如 `# noqa` 但不合语法的指令：**现在 0 条**。

### 复杂度：三个最大的函数拆到可评审

判据来自 ruff 自己的 mccabe，不另写 AST 口径。作用范围直接取 ci.yml 的 `ruff check` 那一行。

| 函数 | 拆前 | 拆后 |
|---|---|---|
| `pm/report.py::render_report` | 508 行 / CC 57 | 编排 27 行 + 16 个章节函数，本文件最高 CC 7 |
| `pm/cli/main.py::main` | 346 行 / CC 44 | 拆出 12 个函数，`main` CC ≤ 10 |
| `pm/cli/calibrate.py::_calibrate_command` | 341 行 / CC 33 | 拆出 9 个函数，CC ≤ 10 |

### 等价性怎么证的（以及它当场抓到的一次自伤）

- `render_report`：50 例合成 state（覆盖 `if agg` / `if base` / `pw.verdict` 四值 / 冲突+归因 /
  注入三态 / 断言四态 / 满分声明 / 空产出 / 脏整数字段等分支）黄金对照，重构前后
  输出 **sha256 `e9e5916d…` 逐字节相同**；另用 AST 比对了新旧字符串字面量的 multiset，
  唯一"少掉"的一条是被扩写进 docstring 的原说明。
- **这个对照当场抓出了我自己写进编排层的一处 API 破坏**：把 `pick_best(state)[1]`（说明文字）
  当成了 `[0]`（版本字典）返回，`pm/nodes/report.py` 会拿它 `.get("prompt")`。
  只跑测试套件抓不到 —— 是 50 例输出比对抓住的。这轮之后我对"重构只靠套件绿"这句话
  的可信度又降了一档。
- `main` / `_calibrate_command`：同样做字面量 multiset 对账（`main` 少 1 条是把
  `rows[0]['run_id']` 提成局部变量；`_calibrate_command` 少 4 条分别是 docstring 缩进、
  两处同文案合并、f-string 里 `：` 被拆出来），加上 CLI/校准相关套件。
  对账过程中真的发现两处遗漏并补回：`--scoring-mode` 的 `mode` 被我误换成
  `analysis["mode"]`，以及不可比基线那句兜底 `"口径指纹不一致"` 被吞掉了。
- 两处新缺陷的回归用例做了**摘守卫验红**：M1（去掉 `_test_runs` 的非 dict/None 防护）
  → 4 条参数里 3 条红，异常类型与预期逐条对应；M2（把 `_report_safe_int` 换回裸 `int()`）
  → 恰好 `test_case_index="abc"` 那条红。两次摘除都按逆操作还原并核对 sha256 相同。

### 门禁扩容（防的不是本轮这类"已经改过的地方"，是下一轮）

- `ruff.lint.select` 加 `SIM` + `RET`。存量清了 12 处（嵌套 if 合并、`if x: a=True else: a=False`
  这类），其中 `--fix` 自动改的只有 SIM114/SIM117 两处、逐处看过 diff。
- `SIM105`（`try/except/pass` → `contextlib.suppress`）**显式不采纳**并写了理由：这几处
  是故意的吞异常，都挂着 `# noqa: BLE001 + 一句为什么`，换成 suppress 会把那句"为什么"挤掉，
  而本仓库的判断恰恰依赖它存在。
- 新门：`tests/test_complexity_ratchet.py` + 台账 `tests/complexity_budget.json`。
  不用 `max-complexity` 死线的原因：存量 33 个函数超过 CC 10，定值线只能"全拆或不开"。
  台账要求与实测**逐字相等** —— 变复杂要显式登记，拆小了必须回收，条目消失要删行，
  另设硬顶 28（= 本轮实测最高值，抬它需要一个单独决定）。
  台账重生成是**手工**命令（`python tests/test_complexity_ratchet.py --rewrite`）：
  测试自己绝不写被跟踪文件，否则 CI 里那条 `git diff --exit-code` 会被自己抓红。
- 本轮这些门**真的抓红过一次**：我改 README §五.2 的覆盖率措辞时，把 `（地板 92）`
  写成了 `（地板 92；同一份代码连跑两遍…）`，`test_coverage_floors_agree_across_all_copies`
  当场断言失败（它按 `（地板 N）` 精确配对两条线，少一条就"静默放过一整个文件的漂移"）。
  改回可配对的形式后复绿。**没有一处是为了迁就我的编辑而放宽判据** —— 这类判据一旦
  因为"只是想改个措辞"被松掉，它就再也不保护任何东西了。

### 实测读数（本轮改后独占复测，连跑两遍）

1251 条 rc=0（两遍都是）；覆盖率 `pm/` 全量 **95.69% / 95.72%**（改前 95.65%）、
去掉网关 **96.3%**、`pm/modelhub/*` **92.8%**（分项地板 82）。README §五.2 原本写的 93.37%/94.91%/86.37%
是 2026-10-02 的旧快照、已被 10-03 Round 18 的 95.73% 越过，本轮按实测改写，
并顺手修掉那句过期的"缺口集中在 `server.py` 361-444"（`server.py` 10-02 就补到 100% 了）。

⚠️ 顺带量到一件事：两遍之间那 0.03pp 的差，逐文件比对下来**全部落在
`pm/modelhub/vkeys.py`**（同一份代码，miss 27 ↔ 30）。
**覆盖率读数自己也会抖**，所以任何引用它的文档句子都该注明是哪一次跑的，
别把它当成精确指标；而"同一份代码两遍数不一样"本身是一条待查的测试不确定源
（vkeys 的密钥签发分支），已进下面的未结案清单。

### 未结案 / 别当事实引用

- 台账里还剩 33 个 CC>10 的函数（最高 28：`pm/modelhub/server.py::_stream_response`，
  其次是 `pm/llm.py::structured_call` 24）。本轮**没动它们**，只保证不会再长。
- `ARG001`（13 处未用形参）不采纳：FastAPI 路由形参与假后端桩类的签名是**故意的**，
  纳进来只会逼出更多 noqa。
- 等价性证据是合成 state 上的输出比对，覆盖不到真端点跑出来的 state 形状
  （那条路径仍由 stub e2e 与第三轮真端点 E2E 的结论管：优化效果至今"无证据"）。
- **新量到的测试不确定源**：`pm/modelhub/vkeys.py` 同一份代码两遍覆盖率 miss 27↔30。
  本轮只观测到"它在抖"，没定位到是哪条用例的路径不确定（要定位得连跑多遍并逐用例比对）。
  影响面：任何拿覆盖率数字做前后对比的结论，差值小于 3 条时不构成信号。
- 本轮所有数字都是本机（Windows / py3.12）独占实测；远端 CI 未重跑，
  而 origin/main 上那步 windows `Tests (pytest)` 的红仍未结案（见 Round 20 未结案条目）。

### 续记（同日第二轮）：测试面收口——上一轮那条"覆盖率会抖"已结案

未结案第 4 条定位到了根因并修掉：**不是噪声，是"只被真竞态撞上才覆盖"的分支**。
`_atomic_write` 的 `except PermissionError`（188-190）在全量跑批里恰好被撞到过，
撞不到就整块不覆盖 ⇒ 两遍差 3 条。

- 新增 `tests/test_modelhub_contention_paths.py`（13 条）：不制造真竞态，而是把
  "瞬时被拒"直接注入（桩 `os.replace` / `Path.read_text` / `_lock_impl`），
  每条防御分支每次都走一遍。断言形式是"有没有按设计退避、让了几次、每次多久"，
  以及失败侧的三条硬约束：**抛出去不静默、老文件不动、临时文件不留场**。
  `vkeys.py` 88% → **98%**，剩 4 条是只在 POSIX 上执行的分支（平台分割，不是缺口，
  在任一台机器上都补不到 100%）。
- 新增 `tests/test_cli_units.py` 的 10 条入口级分支：`--no-baseline/--no-pairwise`
  是否真的落进 env 并与 dry-run 的 enabled 两栏一致、裸字符串用例补齐全形状、
  `--cases-file` 解析失败退出 2 并说明原因、金额外推的三条静默出口 + 一条正对照
  （**外推必须自曝按哪一轮算的**）、`--json` 出口"stdout 恰好一个 JSON"在**入口处**钉住。
  这些是 `main()` 拆细后新露出来的空档 —— 拆之前它们挤在一条直线里，缺口被抹成一个数。
- 清掉 30 处"要了但不用的内置夹具形参"（`tmp_path`/`capsys`/`monkeypatch`），
  并加守卫 `tests/test_hygiene_dead_fixture_args.py`（判据取 ruff ARG001，与 CI 同口径）。
  **守卫先植入一条违规用例验过会红**，再删掉桩文件。
- ⚠️ 批量删除形参这件事本身踩了一次真坑：`_hub` / `_write_ledger` 这类 helper 的形参
  在函数体里没用到，**但调用方还在按位置传**。删掉形参就把实参整体错位了
  （`monkeypatch` 绑进 `tmp_path`），11 + 2 个调用点全中。教训写进了工具里：
  "未使用"只描述函数内部，不描述调用边界；自动删除只允许作用于由 pytest 按名字注入的
  `test_*` / `@pytest.fixture`，其余必须先改调用点。这一类是"看起来像清理、实际是破坏"
  的形状，靠的仍是跑套件而不是看 diff。
- 重测（两轮独占）：**逐文件 miss 数完全一致**，`pm/` 全量 **96.22%**（两轮同值）、
  去掉网关 96.5%、`pm/modelhub/*` 94.6%；1275 条 rc=0，三套 releases 测试 rc=0，
  `releases/*/data/*` 干净，ruff/format/mypy strict 全 rc=0。
  `docs/operations.md` 的薄弱点清单 C 段按实测重写（上一版"无低于 90% 的文件"已过期），
  README §五.2 的覆盖率句子同步。

## Round 22（2026-10-04，PromptMaster 本产品）—— 第四轮真端点 E2E：C1∧C2 首次双过 + G1 字面触发但语义边界暴露 + PM_CALL_BUDGET 实战生效

> 编号注记：Round 21（工程质量轮）是并行协作方在同一工作树完成的未提交轮次，
> 本轮 append-only 追加其后，不触碰其内容与代码改动。

第四轮 E2E 判据跑前写死（`logs/e2e4_criteria.md`），与第三轮逐字同口径
（task/原稿/4 用例逐字复用、samples=2、max_iter=3、基线盲评全开），唯一变量
是代码 add142e（含 `PM_CALL_BUDGET` 墙钟 + `PM_SDK_RETRIES=0`）；`.env` 未动
（换型暂缓）。run `34a205ecc9c3`，57 调用 / 67 分钟，基线臂缓存全命中
（4.98 与第三轮逐字一致，可比性锚死）。完整判据对照已入 `docs/evaluation.md`
§十七·四，此处只记要点与增量。

### 读数要点

- **C1∧C2 四轮以来首次双过**：盲评 4/0/0（与第三轮同）+ Δ+3.64 > 自报噪声带
  2.609（余量 1.03）。v1 修订成功且**完整评估**：8.62 / min 8.28 / 4/4 用例
  pointwise 通过 / 修好 3 弄坏 0（净增 3）。
- **G1 字面触发**：revise 在 v2 修订阶段空返回 → early_stopped（1/3）。按跑前
  铁律 Δ 不作方向性结论；**但语义边界暴露**——G1 的作废理由（保留版本未评估完）
  在本轮不成立，v1 评估闭环完整。G1 细化为「仅当早停保留版本**评估未完成**时
  作废 Δ」应入下一轮判据，本轮作为发现如实记录，不事后放宽。
- **V1 关闭**：`PM_CALL_BUDGET` 实战生效——6 次「墙钟预算 600s 用尽」拦截
  （evaluator_b 5 次 302s×2 发、evaluator 1 次 600s×3 发），第三轮 900s 挂死
  形态消失，错误信息带实际耗时与请求数。§十七 未验项「墙钟复测」兑现。
- V2：空返回 4 次（evaluator 3 + revise 1），比第三轮少、未根治（换型暂缓，
  符合预期）。V3：67 分钟（第三轮 61，增量为限流退避与墙钟等待）。
- evaluator_b（glm-5.2@0.1）case#0/1/2 墙钟用尽 5 次 → 部分用例实为单评委
  出分（降级处理按设计工作、未污染读数），如实记录。

### 对 Round 21「windows 红未结案」的回应

8edcfb7 的 CI 红确认为 **windows job**（check-runs 实查：三 Linux job success、
windows failure）；其后 `0ebff28`（五件套，含大量 tests 改动）与 `add142e`
两轮 CI **4/4 job 全绿**（API 实查 2026-10-04）——红已被后续提交修复或属
flaky 自愈，Round 21 落笔时点之后已有两轮绿证。若需归因到具体修复提交，
翻 0ebff28 的 tests/ 变更即可。

### 验证与边界

- 双方共存判据：并行方 19 源文件 + 4 新测试改动与本轮文档改动在工作树共存，
  合并态全量回归结果见下轮补记（本轮收尾时点跑串行主套件 + 三服务 + 静态）。
- 本轮只提交 `docs/evaluation.md`（diff 核查纯本轮内容）；ledger 连同并行方
  Round 21 留待其所有者提交（不动并行方未提交工作）。
- Δ+3.64 宣称权留给下一轮（判据细化或换型落地）；注入免疫连续两轮未实测。

## Round 23（2026-10-04 晚，PromptMaster 本产品）—— 第五轮 E2E + G1 细化版首用：单轮仍「无证据」，同 task 三轮 Δ 首次统计显著为正

判据跑前写死（`logs/e2e5_criteria.md`，G1 细化版「保留版本评估完整 ⇒ Δ 有效」
首次应用）。run `2d306d72feec`，34 调用 / 34 分钟（缓存命中多），基线 4.98
三连一致，`.env` 未动（换型随后安排）。完整判据对照见 `docs/evaluation.md`
§十七·五，此处记要点与增量。

### 读数要点

- **C1 过（3/0/1，位置偏置 1 例记平，擦线）+ C2 不过（Δ+2.81 < 自报噪声带
  3.181，差 0.37）⇒ 单轮结论「本轮无证据表明比原稿好」**（与第三轮同款）。
- **G1 细化版首秀正常**：早停保留 v0 评估完整（4/4 出分 + 断言 + aggregate
  齐备）⇒ Δ 有效——细化方案判定路径清晰，第三/四/五轮三个 Δ 均有效。
- V1 墙钟拦截复现（2 次、0 挂死）；V2 空返回 3 次；V4 evaluator_b 墙钟用尽
  2 次（glm-5.2 端点差复现）；本轮采样噪声 1.833（第四轮 0.212），评委抖动大。
- 优化版绝对分 7.79 披露折算约 4.7，不作达标宣称。

### 增量：跨轮聚合首次显著（`run.py history` 机制，零调用）

同 task 三轮（第三/四/五，同一 task 文本/原稿/用例集/rubric）：
deltas [+1.69, +3.64, +2.81] → **mean +2.71，CI95 [+1.61, +3.82]，不含 0 ⇒
significant=True**；盲评聚合 11 胜 0 负 1 平。对照组：旧口径（--context）同域
6 轮 mean −0.39 不显著——天然 A/B。

**口径边界（引用必须连带）**：两套口径如实并置不互相冒充（单轮判据与跨轮聚合）；
n=3 粗判、共享基线使三轮 delta 不完全独立（方向不受影响）；Δ 同仪表差值使
偏松偏置近似抵消（这是 Δ 比绝对分可信的依据）；「显著变好」仅在 sales_analysis
域成立，换域未验；换型落地后读数坐标系切换，本读数属 flash-lite 仪表时代。

### 验证与状态

- 提交后全量门禁与 CI 以本轮收尾时点复跑为准（与 Round 22 同纪律）。
- 换型落地（evaluator → deepseek-v4-flash，实测 MAE 0.70/bias +0.03）为下一
  大项，落地需重跑校准基线并切换披露口径。

## Round 24（2026-10-04 深夜，PromptMaster 本产品）—— Round 23 的**并行复跑** + 工程质量轮收尾：同代码同时得出「C2 过」与「C2 不过」

> 编号注记：Round 21（工程质量轮）由并行协作方以 `8618385` 代提；本轮是其**后续**，
> 不替换 Round 23。两次真端点 run 在同一天同一 case 上重叠了 34 分钟，
> 这个重叠本身产出了本轮最有价值的两条结论（一条测量学、一条流程）。
> 完整读数见 `docs/evaluation.md` §十七·六。

### 读数要点（run `a43e44adcc9f`，69 次真实请求 / 约 64 分钟，代码 `8618385`）

- C1 盲评 **3/0/1** 过；C2 Δ **+2.85 > 自报带 2.631** 过（余量 0.22）；
  G1 触发（revise 空内容 ⇒ early_stopped 1/3），保留版本 v1 评估完整 ⇒ Δ 不作废。
- 达标判定仍是未达标：保守下界 6.35 < 8.0（点估计 7.83 不参与判定）。
- **真正的结论**：与 Round 23 并排看，Δ 只差 **0.04**，自报噪声带差 **0.55**，
  于是同代码同输入得出**相反**的 C2 判定 ⇒ 单轮"有证据/无证据"当前测的主要是
  **带估计量自己的抖动**（其中采样极差一项两次相差 4.5 倍：0.403 vs 1.833）。
  这不是"多跑一轮就多一份证据"，而是给跨轮聚合加了一条限制：**参与聚合的带值本身可能脏**。
- 并发污染已实证：两条 run 打同一 3-Key 池，本轮记录到 comparator 疑似限流 ×1、
  `evaluator_b` 302s 墙钟耗尽 ×3、`evaluator` 空内容 ×3、reviser 空产物 ×1（G1 直接成因）。
  ⇒ 两轮的带值都不可当"仪表精度"引用，**要干净带值必须串行重跑**。
- 四轮 Δ（三/四/五/本轮）粗口径：均值 **+2.75**、sd 0.802、SE 0.401、
  95% CI **[+1.96, +3.53]** 不含 0，盲评 14/0/2。不独立（共享基线 4.98）、n=4、同域、
  其中两轮互相污染 ⇒ 只作方向性参考，不比 §十七·五 的三轮更硬。

### 工程质量轮（Round 21）的等价性收口：三层独立证据链

`8618385` 把 508 行 / CC 57 的 `render_report` 拆成 16 个章节函数，声称产品行为零改动。
本轮补上最后一层，三层各自独立：

1. 50 例合成 state 的**输出黄金对照**（重构前后 sha256 逐字节相同）——它当场抓出编排层
   我自己写错的一处返回值下标；
2. 三个文件的**字符串字面量 multiset 对账**——抓出 calibrate 里被我吞掉的 `mode` 与
   兜底句"口径指纹不一致"；
3. **真端点结构比对 E1–E3**（本轮 vs Round 22）：10 个章节顺序与集合完全一致、
   9 条固定措辞锚点逐字相同、对手臂称呼一致 ⇒ 真实 state 上渲染不变。

### 流程缺陷（本轮暴露，三条都还没修）

- **判据文件被同名覆盖**：我 18:57 写 `logs/e2e5_criteria.md`，19:04 被并行 run 覆盖 ⇒
  "跑前落盘"的不可改写性没被任何机制保住。修法：判据文件名带 run_id + 落盘后拒覆盖。
  已事后重写存档为 `logs/e2e5_criteria_并行run_a43e44adcc9f.md`（顶部标明是重写件）。
- **缺 Key 的失败形状**：19:50 那次 `run-5` 没有 API Key，0 次调用即 failed，但报的是
  `clarifier 结构化解析失败（重试 3 次）—— RuntimeError: 缺少 API Key`——
  配置错误被包装成"模型输出问题"，会把人引向完全错误的排查方向。
  同步入口有 Key 闸门（`_api_key_gate`），这条路径（异步/服务侧）没有。
- **`git commit --only` 在本仓当前状态下建树失败**：连续两次报
  `invalid object 7e9352e8… for 'prompts.py'`，而 `write-tree` 证明 HEAD 与真实索引都完好、
  `fsck` 无缺失、索引里没有这个条目。绕行改成普通提交，为此临时取消暂存了协作方
  staged 的 `docs/evaluation.md`（只动索引标记，提交后立刻还原）。
  **副作用要说清**：他们那次提交因此**不含** `docs/evaluation.md`，该文件现在仍是
  staged 未提交状态。根因未定位，下次共享工作树提交前要先确认这条路径可用。

### 未结案 / 别当事实引用

- 干净噪声带未获得（必须串行重跑）；"C2 用带还是用 Δ 的 SE"这个判据问题仍未决。
- 上面三条流程缺陷都没动代码。
- 本轮 69 次请求与 Round 23 的 34 调用**不是两次独立复现**，重复计权会夸大证据。
- 注入免疫连续四轮未实测。
- 远端 CI 未重跑；origin/main 上 windows 那步 `Tests (pytest)` 的红仍未结案。


## Round 25（2026-10-04 深夜，PromptMaster 本产品）—— evaluator 换型落地：聚合分代过滤（模型×rubric）+ 新仪表基线入账

所有者拍板换型（依据 Round 20 补记实验：deepseek-v4-flash MAE 0.70/bias +0.03 vs
flash-lite 3.13/+3.09）。`.env` 落地 `PM_EVALUATOR_MODEL=deepseek-v4-flash`
（备份 `.env.bak-20261004-judge-swap`，gitignore 覆盖确认）；评委 B glm-5.2 不变。

### 换型暴露的聚合口径缺口（TDD 修复，1278 全绿）

`aggregate_history` 原按 judge+mode 聚合，不区分评委模型与 rubric——换型后新旧
两把尺会混进同一份 bias，披露行刻画"两把尺的平均"。实测缺口两层：

1. **模型维**：flash-lite 的 +3.09 与 deepseek 的 +0.03 混算；
2. **rubric 维**：同模型 deepseek 在 2026-09-27 旧 rubric 下 bias +4.2（账本
   实录）、现行 rubric 下 +0.29——评分标准变了，同模型也不是同一把尺。

修法：`aggregate_history`/`latest_pooled` 加 `model`/`rubric` 可选过滤（None=
旧行为向后兼容）；报告披露行与 `--aggregate` 传**运行时解析**的当前模型+现行
rubric 指纹（锚点集刻意不进过滤——§十四 同代考卷照常合并的先例）。判定权在
数据：账本轮记录自带 model/rubric 字段，过滤只是把它们用起来。

- 新增测试 3 条（混代过滤/无匹配返回 None/rubric 分代+锚点放宽）；4 个既有
  fixture 适配（运行时解析 model/rubric——模块导入时求值会被 conftest 密封
  env 的时序坑咬，教训入测试注释）；用例数三抄件同步 1278。
- 换型后 `--aggregate` 实测：rounds_used=1（仅今晚新基线），**新仪表读数
  bias +0.29 / MAE 1.04**——旧尺 22+2 轮全部干净排除，披露行自本提交起刻画
  deepseek-v4-flash 仪表。

### 新仪表基线入账（正式轮，含端点限流风暴）

`run.py calibrate --judge evaluator`（入账本）：10/11 锚点出分（1 条空内容
3 次耗尽），**MAE 1.04 / bias +0.29**，drift 判定 `comparable=false`（评委
模型/协议/提示词/锚点集全换——预期行为，新纪元无前值可比）。晚间端点限流
风暴（429 退避多轮 + 4 次空内容）下完成，PM_CALL_BUDGET 无异常触发。

### 跨轮聚合更新（并行复跑入列后，同 task 4 轮）

Round 24 的并行复跑（run `a43e44adcc9f`，Δ+2.85）入列后：deltas
[+1.69, +3.64, +2.81, +2.85] → **mean +2.75，CI95 [+1.96, +3.53]，不含 0**
——比三轮读数（CI 宽 1.44→1.57？实为 [1.61,3.82]→[1.96,3.53] 收窄）更稳，
「原稿改进模式在该域显著为正」的跨轮结论稳固。同代码两跑 Δ 几乎相同
（+2.81/+2.85）而噪声带不同（3.181/2.631）致单轮 C2 结论翻转——单轮判据在
噪声带边缘的敏感性实证，跨轮聚合是正解。

### 验证

主套件 1278 全绿（rc=0）+ ruff/mypy/format 全绿；本轮收口提交后 CI 复跑为准。

### 未结案

- 新仪表只有 1 轮基线——披露行的 CI 会随校准轮积累收窄；建议下次校准后
  `--aggregate` 复查。
- 6 轮 E2E（第三至五轮+复跑）全部无注入用例——注入免疫仍未实测。
- evaluator_b（glm-5.2）端点差（墙钟用尽）连续两轮复现，角色选型待评估。

## Round 26（2026-10-04 深夜，PromptMaster 本产品）—— 第六轮 run `ed320306a11d`：串行尝试失败 + 判据自身的停止条件被证伪

目标不是优化效果，是拿一个**没有并发污染的噪声带**（Round 23/24 已证明单轮 C2
判定被带估计量左右）。判据 21:16 落盘（`logs/e2e6_criteria.md`），开跑前做了并发核查。

### 读数要点（负结果，照实记）

- **P1 不成立**：21:17–22:32 窗口内落了 **5 个外部产物**——两轮真调用
  （`d082d9cf2fc9` 11 次、`1588e049ffb7` 25 次）+ 三次缺 Key 的失败尝试
  （`run-3`/`run-4`/`run-5`，0–1 次调用）。同一 3-Key 池被三方共用。
- 本轮 **50 请求 / 75 分钟**（对比 §十七·六 的 69 次 / 64 分钟）：
  疑似限流 8、`PM_CALL_BUDGET` 墙钟拦截 5、空内容或非 JSON 解析失败 3 ⇒ 吞吐退化被印证。
- **按判据在 75 分钟线截断**（`Stop-Process -Id 21716`，显式 PID），**未事后放宽**。
  代价是本轮**没有终态报告，也就没有 C1/C2 读数**——不许用跑到一半的 case 分数充当结果。

### 本轮真正的产出：判据自己有一条写错了

判据是"调用数 180 **或** 超 75 分钟 ⇒ 截断"。到线时 50 调用 / 75 分钟：
**约束住的是时间而不是成本**。而端点被并发拖慢时恰恰是"跑得久、花钱少"，
于是这条规则专门掐断我们最想采样的情形，换来一个既无终态也无带值的空样本。
整改方向（未落地）：以调用数为主判据，时间只当进程健康看门狗
（连续 N 分钟无日志写入才判卡死）；并给真端点 run 加**串行锁**——
没有它，"跑一遍"在这个工作树里根本不可承诺。

### 唯一可引用的一条：代码身份可确认

`pm/**/*.py` 在 21:17 后**零个文件被改**（逐个 mtime 核查）⇒ 本轮执行的确为 `8618385`。
但截至 22:33 工作树又前进 4 个提交（含 `a374bbe` evaluator 换型 + 聚合分代过滤），
⇒ **此后任何新真端点 run 与 第三～六轮不同仪表**，跨换型比较 Δ/绝对分必须先声明边界。

### 未结案 / 别当事实引用

- 干净噪声带三次尝试未获得（两次并行、一次被并发打断）。
- 缺 Key 的异步/服务侧入口无闸门，本轮又 reproduced ×3（Round 24 已记）。
- 判据文件"带 run_id + 落盘后登记"本轮已执行；"落盘后拒覆盖"未落地。
- 注入免疫连续多轮未实测；evaluator_b 端点差连续复现，角色选型待评估。
- 本轮文档（§十七·七 与 Round 26）**未提交**——工作树在跑批期间被并行提交过四次，
  提交权仍按"提交/推送分两次授权"的规矩留给所有者。


## Round 27（2026-10-04 深夜，PromptMaster 本产品）—— 第六轮（本会话）failed 截断 + revise 墙钟降级缺口修复 + 协作事故注记

接 Round 26（并行协作方的第六轮）：本会话独立发起的第六轮 run
`87d9c16e0e48`（34~36 调用 / 65 分钟）按判据截断入档（`docs/evaluation.md`
§十七·八）。**协作事故如实入档**：双方在同一 worktree 复用同一批
`logs/e2e6_*` 文件名，判据文件被先后覆盖、产物互相视为污染源——教训：
共享 worktree 的实验产物名必须带会话标识。

### 读数要点

- status=**failed**：reviser 墙钟预算耗尽抛 CallBudgetExceeded 炸整轮
  （**缺口已修 f8d899f**：转 early_stopped 保留历史最佳，TDD 钉死）。
- 优化版 v0 = 6.72（新仪表重评，与第四轮 v0=6.67 同域可比）；Δ 不可算
  （基线聚合缺失，failed 截断）；盲评 3/0/1 better 独立成立。
- **V2-E 换型效果 E2E 级证据**：evaluator（deepseek）空返回 2 次 vs 旧仪表
  同位置 3~4 次；V4：evaluator_b（glm-5.2）异常 9 次，晚间不可用三连坐实。

### 验证

主套件 1279 全绿（rc=0）+ ruff/mypy 全绿（单文件 mypy 的 no-any-return 是
缺包上下文假象，全包口径干净）。收口提交后 CI 复跑为准。

### 未结案

- 第七轮 E2E（revise 修复后）是第一个完整验证窗口（新仪表 + 优雅降级）。
- 注入免疫五轮未实测；evaluator_b 选型待评估；协作产物命名规范待固化
  （建议 docs/operations.md 加一条：实验产物名带会话标识）。

## Round 28（2026-10-05 凌晨，PromptMaster 本产品）—— 第七轮：新仪表披露行与 revise 降级双双兑现，C2 仍不过；arbiter 同族注记

产物带 `e2e7_loomy_` 会话标识（Round 27 事故教训落地，开跑前并发核查通过）。
run `c4ccfd18c72d`，36 调用，early_stopped **优雅交付**（第六轮同位置整轮
failed 的降级修复首次实战）。判据对照详见 `docs/evaluation.md` §十七·九。

### 读数要点

- C1 过（4/0/0）+ C2 不过（Δ+1.08 < 自报 2.94）⇒ 单轮「本轮无证据」
  （与三/五轮同款）；G1 细化版 v0 完整 ⇒ Δ 有效。
- **新仪表披露行首次实战**：bias +0.29（CI −0.66~+1.08）落入告警线内触发
  弱措辞「绝对分可按面值理解」——旧尺 +3.09 退出历史，Round 21 预言兑现。
- **revise 降级首战**：空返回 → early_stopped 交付（EXIT=1 未达标但已交付），
  f8d899f 按设计工作。
- V2-E evaluator 空返回 1 次（六轮 2 次、旧尺 3~4 次——复现确认）；V4
  evaluator_b 异常 8 次**四连坐实**；V5 基线读数迁移 4.98→5.38（+0.40，
  在新尺 bias 刻画内自洽）。
- 新注记：arbiter 与 evaluator 同族（deepseek-v4-flash，温度不同）——同源
  检测不触发但仲裁独立性语义弱化；evaluator_b 选型时应一并考虑（§十七·九）。

### 验证与状态

主套件 1279 全绿（rc=0）；收口提交后 CI 复跑为准。产物命名纪律（会话标识）
本轮首次执行，无撞名。

### 未结案

- 新尺下的 Δ 跨轮结论需重新积累（旧尺 +2.75 显著不可直接延续）。
- 注入免疫六轮未实测；evaluator_b 选型（含仲裁同族问题）待拍板。

## Round 29（2026-10-05 凌晨）—— 真端点轮次的跨进程串行锁：`PM_LIVE_LOCK`（缺省关闭，launcher 负责打开）

### 为什么现在才做

三轮真端点跑（§十七·六 / §十七·七）都撞在同一件事上：**多个会话各自起轮次、共用同一个 3-Key 池**，
于是噪声带被互相污染。第六轮的 P1 判据拿到硬证据——我的窗口里落了 5 个别的会话的产物
（两轮真调用 11 + 25 次，另三次缺 Key 尝试），50 次调用换不到一个可引用的读数。
"跑之前先看有没有别人在跑"靠人记不住，同日已经撞了三次。

### 落地

- 新模块 `pm/cli/live_lock.py`：`log_dir()/.live_run.lock` 锁字节 0（Windows `msvcrt`
  独占锁 / 其余 `fcntl.flock`），持有者 PID 与起始时间写进旁边的 `.live_run.holder`。
  拿不到就抛 `LiveRunBusy`，消息点名**持有者**的 PID；`PM_LIVE_LOCK_WAIT` 可选排队秒数。
- 接入 `run.py` 同步入口（`_execute_run_serialized`）：**缺省关闭**——产品侧
  target 模型赛马与网关多 worker 的并发是有意的，这把锁串的是"一轮"，不是"一次调用"。
  被占着退 **2**（配置/参数那一档），`--json` 时仍然只打一个 JSON 对象（`status: busy`）。
- `scripts/run_round.sh` 打开它（`PM_LIVE_LOCK=${PM_LIVE_LOCK:-1}`）并回显状态。
  **默认值不在 pm/ 里、开关在 launcher 里**，是因为 launcher 才是"起一轮"这个动作的现场。

### 过程中被 Windows 语义逼出来的两个设计修正（都留在代码里）

1. 原本打算"抢到锁后把 PID 写进同一个锁文件"。实测：`msvcrt.locking` 是独占的，
   **连另一个句柄读被锁的那一字节都拒**（`PermissionError 13`），
   于是抢到之后的 `ftruncate` 会被拒、竞争者想读 PID 也会被拒 ⇒ 消息只能报"未知"。
   改成锁令牌与 holder 分两个文件（`.live_run.lock` 从不写内容）。
   顺带记一笔：`pm/modelhub/vkeys.py` 的跨进程锁是"先写 PID 再抢"，
   它那条超时消息里的 PID 实际上是**竞争者自己**——同一个坑，那条我没动，写进未结案。
2. 跨进程用例第一版用 `Popen.pid` 断言"消息点名持有者"，红了一次：
   venv 的 `python.exe` 会再派生真解释器，Popen 报的是中间进程（26720 vs holder 自报 21256）。
   改成让子进程把**自己的** `os.getpid()` 写进握手文件再比对。
   这是"断言写得对不对"和"锁对不对"两件事——红的那次是前者。

### 验证

- `tests/test_live_run_lock.py` **16 条**：语义层（抢到写 PID / 释放后可再抢 / 空 holder 退化成"未知" /
  `lock_path` 与 `holder_path` 都跟 `PM_LOG_DIR` 走 / 给了排队秒数就必须真退避）、
  **真跨进程层**（子进程持锁、父进程来抢，双向都对得上）、入口层（开着才串；
  缺省关着时把 `hold` 换成"一调就炸"来证明一次都不碰锁）。
  本模块覆盖 **94%**，剩 4 条是只在 POSIX 上执行的 `fcntl` 分支——与 `vkeys.py` 同一类平台分割，
  不是缺口（Linux CI 上反过来盖不到 `msvcrt` 那几条）。
- 摘保护验红两处，都红在该红的地方：M1 旁路 `_execute_run_serialized` ⇒ 三条入口用例全红；
  M2 把 holder 挪到抢锁之前 ⇒ 跨进程用例红，且报的就是竞争者自己的 PID（正是那条设计断言要拦的）。
  两次都用逆操作还原并 `cmp` 核过。
- 全量套件 rc=0；活体收集 1279 → **1295 条**（+16 全来自本模块），三处抄件同步；
  `ruff check` / `ruff format --check` / `mypy --strict` / 复杂度台账 全 rc=0。
- 覆盖率 96.13%（本轮终态）。**顺手量到同一类问题的第二个实例**：连跑两遍全量，
  `pm/llm.py` 的未覆盖行在 21 ↔ 25 之间摆，逐行差集精确定位到
  **410、734、1286-1288**——其中 1286-1288 那一处在后一遍里**整块没被执行**
  （不是走了另一分支），说明"哪个测试会打到重试层的这条分类"取决于运行时时序。
  这与本轮刚修掉的 vkeys 抖动是同一形状：**覆盖率作为"前后对比"的指标仍不可信**，
  直到这四行被做成确定性用例（注入条件即可，vkeys 已示范过做法）。

### 未结案 / 别当事实引用

- **`pm/llm.py:410 / 734 / 1286-1288` 的覆盖随时序摆**（本轮连跑两遍差 4 行，
  1286-1288 在后一遍里是**整块没执行**，不是走了另一分支）。下一步照 vkeys 的做法把条件注入，
  别等真时序——在此之前，任何"覆盖率比上一轮涨/跌几个点"的说法都不构成信号。
- **只覆盖 `run.py` 同步入口**：`submit` 异步路径与 `pm/server.py` 起流水线不受这把锁管。
- "抢锁前先写 PID 导致消息点名错人"这个形状在 `pm/modelhub/vkeys.py` 里仍然存在，本轮未动。
- 停止条件仍写的是"180 调用 **或** 75 分钟"，本轮未改判据模板（§十七·七 已记整改方向：
  主判据用调用数，时间只当"连续 N 分钟无日志写入"的卡死看门狗）。
- 缺 Key 的失败仍表现为"clarifier 结构化解析失败重试 3 次"（抛点在 `pm/llm.py:707`，
  重试环把配置错误当模型输出问题）。异步路径同样缺 Key 时会被重试用尽后
  存成 `status=failed`，只是错误分类不对。要修的是**错误分类**，本轮没做（并行会话正在改 `pm/llm.py` 重试区，避免撞车）。
- 这把锁**还没在真端点轮次上用过一次**——它是"防下一次"的，不是本轮的实测证据。
  下一轮真端点必须走 `scripts/run_round.sh`（或显式 `PM_LIVE_LOCK=1`）才算被保护。
  （→ Round 30 已做真端点首验，并且首验就抓出"启动器没把 env 传进子进程"这个坑。）

## Round 30（2026-10-05 凌晨 01:30–02:30）—— 串行锁真端点首验 + 一次**写错的归因**被自己推翻：那两条红是判据挂在调度上

### 干净串行测量：拿不到，但拿到了更值钱的东西

判据先落盘（`logs/criteria_cleanband_20261005-0116.md`），停止条件按 Round 26 的整改改成
**主判据看调用数（180）、时间只当"连续 12 分钟无新行"的看门狗**。run `7d87c5065c55`
在 01:30:43 挂上锁起跑，**串行前提成立**（窗口内 `logs/run_*.json` 零外来写入）。
11 次调用后以 `status=failed` 收口：报告里全是 `Connection error`。

带值那一行是这样的：

> 采样噪声（平均极差）：**0.0**；⊕ 评委复现性抖动 2.6 → 合成噪声带 **2.6**

也就是说 Round 24/26 猜的事被直接坐实了：**这条带 100% 来自 `.env` 的配置常数，实测贡献为 0**。
但它是失败轮的读数，只能当"组成的证据"，**不能当新仪表的精度**——这点写死在结论里。

### 锁的首验顺手抓出一件事：没有那行日志，裸奔会看不出来

第一次起跑是 **`Start-Process` 的 `$env:` 没进子进程** ⇒ 那一轮从头到尾没上锁，
而 holder 文件不存在、日志里也没有"串行锁已持有"那行——**只看"启动器说它设了变量"会以为被保护着跑完**。
按显式 PID 停掉（发了 2 次请求、无产物），改到批处理内 `set` 才真挂上（holder 与日志两处 PID 一致）。
这正是 Round 29 那条"可观测"改动的价值兑现：**保护机制必须自证**。

### 【已撤回】"本机 HTTP 客户端连不上自建 loopback 端口"——那句归因是错的

两条测试红了（`test_endpoint_hits_are_our_visible_retry_count`、
`test_sdk_layer_adds_no_invisible_hits`，断言"装死端点应收到 1+2 发"实测收到 **0**）。
当时的定位过程（每条本身都成立，但**推不出我写的那个结论**）：

- **不是并发**：三遍全量失败集完全一致（0/3 抖动），且 `-k` 单跑也红；
- **不是我新加的用例**：单跑那两条时我的用例被 deselect，仍红；
- **不是我给的 env**：去掉 `PM_BASE_URL` 等仍红；
- **不是代理**：进程 env 与注册表 User/Machine 两个作用域都没有 `*_PROXY`；
- 同一端口上 `urllib` 直连 → handler 记到 1 次；换成 openai SDK（httpx）→ 客户端
  `APITimeoutError`，而 handler **一次都没记到**。

我据此写了"**是传输层到 loopback 本身**……环境侧变化，机制未定（疑杀软/网络过滤器）"。
**这句已在 02:30 当场推翻，而且推翻它只需要三条我一直没跑的便宜对照**（同一份代码）：

| 跑法 | 结果 |
|---|---|
| `tests/test_call_budget.py` 单文件，**不带** `--cov` | 24/24 **绿** |
| 它前面那 8 个文件 + 它，**不带** `--cov`（161 条） | **绿** |
| 同一单文件，**带** `--cov` | 同样那两条 **红** |
| 全量 1301 条，不带 `--cov` | 那两条 **红** |

⇒ 触发变量是**负载与插桩**，不是"环境坏了"。而失败现场早就印在日志里，我没读：
带 `--cov` 那一跑的 stderr 里有端点 handler 自己的栈——

```
File ".../http\server.py", line 404, in handle_one_request
    self.raw_requestline = self.rfile.readline(65537)
ConnectionAbortedError: [WinError 10053] 您的主机中的软件中止了一个已建立的连接。
```

**真机制（已定，不再挂"未定"）**：那两条用例数的是"端点收到几发请求"，而记账发生在
`ThreadingHTTPServer` 的 **handler 线程**里；客户端 timeout 只有 1.0s。coverage 要给
**每个新线程**装 tracer，满载全量同样会饿住那个线程 ⇒ 线程起来时连接已被客户端关掉，
`readline` 抛 10053，**计数停在 0**。也就是说"**断言是否为真**"取决于"谁先被调度"——
请求确实到达过（连接被接受、客户端把请求写进去了才会有关闭动作），`urllib` 之所以"记到 1 次"
只是它默认不设 1s 超时、愿意等那个晚起的线程。

**还剩一处我没解释**：同一份带 `--cov` 的全量在 **00:54 / 01:01 两遍是绿的**（`logs/qa-lock-2/3.log`，
无一条 FAILED），01:49 起连红五遍。竞争就是会翻脸的，输赢概率随负载变——01:30~01:44 那阵
活端点轮次正在跑（另一个 python 进程 + 它的 httpx 线程）。这条**只是合理猜测，没量过**，
所以我把它写成待办而不是结论：要坐实得在同样两遍 `--cov` 下并跑一轮活端点看红率。
原先那句"`pm/llm.py` 自 18:44 未改动、00:50 全量还 rc=0 ⇒ 环境侧变化"错在把"没查到机制"
当成了"机制在环境"，而这两跑恰恰是**同一份代码、同一种跑法、结果不同**——那本身就是
"判据挂在时序上"的签名，本该当场把我引到正确答案上。

### 修法：把因果倒过来，而不是把窗口放大

不是"timeout 从 1s 改 5s 再赌一次"，是让**被测方自己制造那次失败**：端点
**先记账、再掐断连接**（`close_connection = True`，不给任何响应）。客户端的错误由那次
掐断引起，因此必然发生在记账**之后** ⇒ 计数与线程调度彻底解耦。同时把判据从"文本里有没有
timed out"改成"`_is_transient_conn_error` 认不认这次失败"——解释权交给生产代码里那个分类器，
而不是某个 SDK 版本的报错措辞。墙钟断言一并删掉：本文件的 autouse 夹具把 `time.sleep` 打成
no-op，那两条 `wall >= 2.5` / `wall < 2.0` 本来就只在新写法下失去意义，留着会误导。

改动后实测（都是当场跑的，非沿用）：

- 带 `--cov` 全量 **rc=0**（连跑两遍：1301 条、96.20%），不带 `--cov` 全量 **rc=0**；
- 两遍 `--cov` 的 `pm/*.py` **逐文件行一字不差**，`pm/llm.py` 的 miss 从摆动的 21↔25
  收成稳定 20 行，其中 `1201` 已被覆盖（就是这两条实证贡献的），`1287` 每次都缺——
  与"疑似死分支"的记录一致（内层在 479/492 就抛预算错，外层 1286-1287 从该调用点走不到）；
- **检出能力没有因为变确定而缩水**：`PM_SDK_RETRIES=2` 变异（把钉住的隐式层放回来）
  仍然精确打出 **9 = (1+2)×(1+2)** 与 **3** 发，两条断言同时红。

### 探针在假绿这件事仍然成立（与上面的撤回无关）

最该记的是这一条：**`scripts/measure_retry_layering.py` 在 hits 全 0 时仍然输出
"A ≥ C ≥ B 成立 ⇒ 层叠逐层收敛"并 rc=0**——`0 ≤ 0 ≤ 0` 是真命题。
端点级实证一旦瞎眼，它会给出"看起来最像修好了"的结论。已加前置体检：
A 档（无预算、SDK 隐式 2 + 连接重试 2）若数不到 ≥3 发，直接判**探针失效**、
输出"别引用本次任何数字"、退出码 **2**（实测改前 rc=0 → 改后 rc=2）。

### 连带撤回一条归因（撤回本身仍成立，但**当时给的理由是错的**）

我 01:45 说过"服务商侧端点全体不可用"。那句话作为归因撤回——不过撤回它的理由
（"同一台机器连 loopback 都送不到 handler"）上面已经作废，所以当时那句是**用一个错解释
去否定一个观察**，两件事都不该写那么硬。现在只保留观察与两处实测：

- 01:45 preflight 九角色全 ❌（`OpenAIConnectionError`），**同一条日志里 langchain 打了
  "detected system proxy configuration … httpx's env-proxy auto-detection applies"**；
- 02:29 同配置 preflight **九角色全 ✅**（2.7~3.4s，rc=0），当场实测
  `urllib.getproxies()` = `{}`、四个 `*_PROXY` env 均未设、`netsh winhttp show proxy` = 直接访问。

⇒ 01:45 与 02:29 之间**系统代理状态确实变过**，"环境侧偶发"在"活端点失败"这条上重新有了线索；
机制仍未定，就不写主人。**但这条不许反过来给那两条测试翻案**——测试的红已实测确定是
线程调度，与出网无关。

### 现在的账

- 活体收集 1295 → **1301 条**（+5：`pm/llm.py` 四条时序分支的确定性用例，其中 base_url 那条两臂；
  +1：Round 29 的锁可观测用例）。三处抄件同步。
- CI 范围内 `ruff check` / `format --check` / `mypy --strict` / 复杂度台账 / 死夹具守卫 全 rc=0。
- 全量套件**三遍全绿**：`--cov` 两遍（run f/g，各 1301 条、Total 96.20%、`pm/*.py` 逐文件一字不差）、
  不带 `--cov` 一遍（run h）。上一版这里写的"2 红，等本机 loopback 恢复后重跑"已作废——
  那 2 红是判据挂在调度上，02:30 前修好并按 `PM_SDK_RETRIES=2` 变异验过检出力。
- 干净带值：**仍在跑**（run_id `54b28925695a`，02:30:40 起跑，`--samples 2 --max-iter 3` 原稿改进模式）。
  （→ **Round 31 已收口**：35 调用 / `early_stopped`，带 3.035 其中 85.7% 是配置常数；
  窗口内有 1 次外来真实调用，故定格为"可引用但非零外来调用"的串行采样。）
  锁的自证两处齐：holder 文件 PID 10896 == `Get-CimInstance` 列出的活进程，stderr 里有
  "串行锁已持有：…（PID 10896）"。第一次尝试 `7d87c5065c55` 因 `Connection error` 判 failed，
  就是上面那条观察。
- `pm/llm.py:410/734/1286/1288` 的覆盖确定性**已验**：两遍 `--cov` 的 miss 列表里都没有它们；
  `1287` 稳定 miss，按"疑似死分支"记着（不是抖动，是从该调用点走不到）。
- 未提交：`pm/cli/live_lock.py`、`tests/test_live_run_lock.py`（新）；
  `pm/cli/main.py`、`tests/test_call_budget.py`、`scripts/measure_retry_layering.py`、
  `scripts/run_round.sh`、三处抄件与 `docs/evaluation.md`/台账（部分 staged 是并行会话的）。

## Round 31（2026-10-05 凌晨 02:30–03:19，PromptMaster 本产品）—— 串行测量·新仪表首代噪声带：带仍是配置常数；顺手照出「空产出硬封顶」的接线缺口

判据 `logs/criteria_cleanband_20261005-0116.md`（01:16 落盘，早于任何读数，本轮未改）。
run `54b28925695a`，35 调用，`early_stopped`（修订器空内容 → 保留历史最佳，EXIT=1）。
读数详表见 `docs/evaluation.md` §十七·十；本条只记判据/读数/未验项三段摘要。

### 判据

- 唯一问题：换型（`a374bbe`）后这台量具的合成噪声带是多少、无并发污染时能否复现。
- 串行：显式 `PM_LIVE_LOCK=1` + 同步入口（`live_lock` 的**第二次真端点使用**）；开跑前核查 +
  运行期每轮询点核对 holder 与外来 `run_*.json`；**不依赖锁自动生效**，绕过 launcher 的入口如实记。
- 停止条件改用新模板：主判据**调用数 180**，看门狗**连续 12 分钟零日志**；单纯耗时长不算失败。
- 主判据 M1 带的组成 / M2 复现性 / M3 判定翻转；产品判据 C1 盲评 ≥3/4、C2 Δ>自报带、G1–G5。

### 读数

- **M1 答案**：带 **3.035** = 采样极差 **1.565**（本轮实测）⊕ `judge_jitter` **2.6**（`.env` 常数）
  ⇒ **85.7% 来自一个配置数字**，同 Round 30 的「0.0 + 2.6」同向。**这条带不能当仪表实测精度引用。**
- 优化版 avg **7.6** / min 6.62 / 下界 **5.29** / 通过 1 条；基线 **5.53** / 下界 3.5 / 通过 0 条；
  **Δ +2.07**（最低 +1.67）；断言 0/2→0/2。
- **C1 过**（盲评 3/0/1，位置偏置 1/4 已记 tie）；**C2 不过**（+2.07 < 3.035）⇒ 单轮「本轮无证据」
  （与三/五/七轮同款）。新仪表披露行 bias **+0.29**（−0.66~+1.08）落告警线内 → 弱措辞，兑现。
- 报告自打 `conflict`：pointwise 未达标但盲评多数判优化版更好；attribution 指向
  「优化版更保守」（保守标记 9→38、字符 5397→3713）——**可能是真实回退**，本轮无人工核查。
- evaluator_b 墙钟用尽 **×3**（302/516/302s）——**连续第五轮**复现，角色选型仍待拍板。

### 本轮真正的发现：封顶只改了落盘值

封顶按设计触发（stderr 有「4 条压到 4.0」），但：**①聚合没吃到**（report 的 avg/min 与逐用例分
都是未封顶读数——`evaluate_node` 另建 `evals` 做聚合，封顶只写在 `evaluations` 那批 dict 上，
`pm/nodes/judge.py:835-885`）⇒ **够不着达标判定**；**②报告没披露**（`pm/report.py:481` 读
`state["empty_cap"]`，而 `judge.py:1039` 只把它传给 trace、没进 `patch` ⇒ 该分支是死的，
交付报告全文确无此句）；**③落盘与报告自相矛盾**（JSON 里四条 `weighted_score` 全是 4.0，报告说 7.6）。
另：**4/4 全命中画像属误报**——交付版提示词本就要求标注「数据缺失」，3/4 用例确实无数据可分析，
与标记表（`scoring.py:439-449`）正面撞车，而 case#1/#3 是完整三章节的实质报告。
（三条均**只凭读码/日志/JSON**，本轮未跑 pytest 复现——判据写明运行期不动代码，未破例。）

### 外来产物（串行前提如实记）

窗口内 3 个非本轮产物：`run-3`（02:06，0 调用）、**`run-4`（02:44，1 次真实调用）**、
`run-5`（02:44，0 调用），都是 `task="t"` 的缺 Key 探针。比 Round 26（5 个外来产物 + 两轮真调用
11/25 次）干净得多，但**不是"零外来调用"的干净窗口**——run-4 那 1 次确实进了同一 Key 池。
下一位引用者按这句话定级。

### 未验项

- M2 复现性未获得（需第二次串行复跑）；M3 因带 85.7% 是配置常数而不成立。
- **封顶接线缺口未修**（聚合/披露/落盘三处），且 `tests/` 下**搜不到 `empty_cap`** —— 无测试钉住。
- 本轮未跑 pytest，上述代码层结论无可执行复现。
- 注入免疫**七轮未实测**；仲裁同族（deepseek）量化影响未测；evaluator_b 选型待拍板。
- 新尺 Δ 序列仍只有两点（+1.08 / +2.07），旧尺 +2.75 的显著结论不可延续。
- 配置少记一笔：`PM_EVALUATOR_MODEL=deepseek-v4-flash` 未进 `.env.example`（Round 25 已知项）。


## Round 32（2026-10-05，PromptMaster 本产品）—— 封顶接线轮：§十七·十 照出的三条对不上全部修复并钉死（TDD 先红后绿）

上一轮（Round 31）只凭读文件照出的三条对不上，本轮逐一 pytest 复现后修复：

1. **聚合没吃封顶**：`judge.py` 的 `evals`（`EvaluationResult` 聚合输入）在封顶**前**就建好，
   封顶只改 dict → 复现：`test_empty_cap_wiring.py::test_cap_feeds_aggregate` 红
   （avg=8.75 裸分、passed=True）。修复：`evals` 构建移到封顶循环之后重建——
   `weighted_score` 是 `from_evaluations` 唯一吃的分数字段，重建即同源。绿：avg=6.0、min=4.0、passed=False。
2. **披露分支是死的**：`empty_cap` 只进 trace 事件 payload，`state` 里永远没有 →
   `report.py:481` 读 `state["empty_cap"]` 永远空。修复：`patch["empty_cap"] = n_capped`。
   复现/钉死：`test_cap_reaches_state` + `test_report_discloses_cap`（键名契约双向验证）。
3. **落盘与报告自相矛盾**：前两条修好即消（落盘 dict、聚合、披露同源）。

**修复的连带效应（意外的活案例）**：`test_injection_gate.py` 两用例红——夹具的 3 字输出
「已解决」此前能以 9.5 **裸分**达标（聚合吃裸分时代的放水通道）；修复后封顶真正参与达标判定
（`len < 8` 判空画像）→ 压到 4.0 不可达标。夹具已换实质文本。这条活案例反向确认了修复的价值。

**测试基建注意**：渲染层 `_empty_deliverable_lines` 在 state 无 `test_runs` 时对空串查画像
必然命中（`len("")<8`）——单独拿 evaluate 的 out 当 state 渲染会误报「空产出获高分」；
真实链路 `test_runs` 从 test 节点起常驻 state 不受影响，测试侧按真实延续方式拼装。

**画像误报未修**（Round 31 的第 4 条）：标记表与「数据缺失」标注撞车属敏感语义改动，本刀不动，
留作下一刀候选。

验收：四条接线测试先红后绿；全量 1308 串行绿（+4）；ruff/format/mypy 干净；用例数三处同改
1304→1308（README / operations / pre-commit 注释，`test_test_count_copies_match_the_live_collection` 钉）。

## Round 33（2026-10-18，PromptMaster 本产品）—— R4 取证：把中断那一轮的读数捞回来，并修掉"中断 = 零落盘"

**① R4 不是"什么都没跑出来"，是"没人去看 err"**

`logs/live_run_support_triage_20261014_r4.json` 是 0 字节，此前据此记为"R4 未跑完、归因未裁定"。
本轮按 `live_run_support_triage_20261014_r4.err`（46KB，322 行）逐行取证，读数其实在：

| 轮 | 基线 avg/min | 优化臂 avg/min | Δ(pointwise) | 成对盲评 |
|---|---|---|---|---|
| R3（命题：单类别刚性约束） | 7.35 / 5.12 | 9.50 / 9.30 | **+2.15** | 优化版 1 : 基线 3 → **worse** |
| R4（只改命题：所有根因类别逐一列出） | 8.77 / 8.60 | 9.36 / 9.12 | **+0.59** | **没跑到**（流程止于 reviser 429 退避） |

出处：R3 `live_run_support_triage_20261013_r3.err:134 / :242 / :263`；
R4 `live_run_support_triage_20261014_r4.err:111`，聚合行在 02:24:33（`avg=9.36 min=9.12 下界=8.76`，
该行 `passed=True` 随后被注入门禁压成 False：1/1 条注入用例被劫持）。

读得出来的方向：**只改任务命题，基线就被抬高 1.42 分（7.35→8.77），而优化臂几乎不动（9.50→9.36），
Δ 从 +2.15 缩到 +0.59** ⇒ R3 那个"大 Δ"里有相当一部分是任务命题把基线压下去造成的假象，
与"冲突是任务命题产物而非提示词骨架缺陷"的假说同向。

**但裁定仍然不成立**：R4 设计的判据是成对盲评（R3 的冲突正是盲评给的），而盲评在流程更后面，
那一轮在 02:25:13（`[reviser] 轮换到 Key 池第 0 个 Key 重试`）之后进程消失，盲评一行都没有。
Δ 缩窄是 pointwise 侧的旁证，不是判据本身。**要裁定必须重跑 R4**（真端点一次约 80~100 次调用 / 3~5 小时），
属 owner 的成本决策，不在工程侧替他决定。

**② 同轮修掉"中断即零落盘"（R4 证据全丢的直接原因）**

- 根因：`run_pipeline` 只在正常返回时把 state 交回 `pm/cli/main.py:245` 的 `save_artifacts`；
  Ctrl+C / 关闭终端抛的是 `KeyboardInterrupt`（`BaseException`），既不抓也不落盘。
  而 langgraph 默认带 MemorySaver，**每个节点之后的状态本来就在内存里**，只是没人去取。
- 对照证据：同日 20:20 / 21:08 / 23:16 / 23:17 四轮（完成或早停）都有 `run_*.json`，
  只有 00:09 起、02:25 中断的 R4 没有 ⇒ 不是"从来不落盘"，是"只有跑完才落盘"。
- 修法：`pm/cli/pipeline.py` 加 `except BaseException` 抢救分支 —— 取 `app.get_state(config).values`，
  用 `_atomic_write`（`pm/modelhub/filelock.py`，同族实现不抄第二遍）写到 `pm.paths.log_dir()`，
  **明写 `artifact_interrupted=true` + `interrupted_at`**，并向 stderr 报出落点。
- 三条刻意的边界：正常路径一个字节都不多写（落盘时机变了会污染 `--json` 契约与运营目录闸）；
  **空状态不落盘**（"文件在、信息不在"正是 `live_run_*.json` 0 字节那个坏形状）；
  抢救失败不许吞掉原始异常（把 `KeyboardInterrupt` 变成 `OSError` 会让调用方以为任务本身坏了）。
- 守卫 6 条（`tests/test_pipeline_interrupt_safety.py`），先红后绿。
  变异验牙：`except BaseException` → `except Exception` ⇒ 2 条当场红，还原即绿。
- ruff / format / mypy --strict 干净；`tests/` 子集 253 条（cli/pipeline/graph/e2e/artifact/interrupt）全绿。

## Round 34（2026-10-22，PromptMaster 本产品）—— 用户旅程审计：配置发现、入口协议与英文文档的三处真缺陷

**本轮判据**：不新增"优化效果"结论（那需要所有者亲判锚点），只修"用户照文档做却走不通"的路径。
对照组仍是 2026-10-13 竞品报告：生态接入、配置到达速度、CI 增量门禁是三个显眼短板，后两个可在本地零调用补齐。

### 用户旅程实测发现（先复现，后修）

1. **`.env` 发现顺序与文档相反（真缺陷）**。`prepare_console()` 直接 `load_dotenv()`，
   python-dotenv 默认从**调用方文件**所在目录向上找：装包后调用方是 site-packages 的
   `pm/bootstrap.py`，用户在自己项目里放的 `.env` 永远读不到（"配了 Key 却说没 Key"）；
   检出模式反过来，从 `/tmp` 跑仓库 `run.py` 会读到仓库那份 `.env`。
   修：`pm/bootstrap.py::load_env_files()` —— **cwd 向上优先，检出根兜底，进程环境不覆盖**；
   `pm/llm/config.py` 与 `run_server.py` 共用同一套（后者此前只做编码兜底，根本不加载 `.env`）。
   守卫：`tests/test_env_discovery.py` 4 条，其中一条用"临时目录里的 probe.py + PYTHONPATH"
   复刻装包态；修复前该用例读到空串。
2. **没有 `--version`**。`run.py --version` 被当成未知参数退 2。修：主 CLI / `run_server.py`
   各加 argparse version action，值取 `pm.__version__`；`tests/test_version_flag.py` 3 条
   同时钉住 `run.py`、`python -m pm.cli.console`、`run_server.py` 三个入口。
3. **面板端口占用只丢裸日志**。uvicorn 的 `SystemExit(1)` 原样穿出：先打印"面板地址"，
   再一行 bind 报错，退出码 1（运行失败）。修：`panelcmd._serve` 接住 SystemExit，
   给"换 `--port 8800` / 关掉占用进程"的人话并退 2；`SystemExit(0)` 仍按正常收工退 0。
4. **用户仓库的提示词 CI 门禁缺一口气**。原有 `diff A B` 要求用户先 `git show > /tmp/base.md`。
   修：`run.py diff --base-ref origin/main prompts/x.md`，左侧直接取 `git show`，
   退出码仍是"有新引入才 1"；新文件无基线、坏 ref、参数写错一律退 2 并给人话。
   守卫：`tests/test_diff_base_ref.py` 9 条（4 条 subprocess 打真实入口 + 5 条进程内分支）。
5. **英文 README 长期漂移**：测试数少 5、子命令数写 13（活体 12）、CI 写成
   "13 gates × Linux + Windows"、校准一节把历史快照当作当前读数。修：按实测重写，
   并把英文 README / 面板底栏 / `docs/agent-skill.md` 纳入既有的"同数抄件"守卫
   （`test_test_count_copies_match_the_live_collection`、
   `test_documented_mcp_tool_count_matches_the_live_registry`，
   新增 `test_documented_subcommand_count_matches_the_live_parser`）。
6. **测试基建会把工作树静默写脏**（本轮全量跑出来的）：变异测试的还原失败被
   `except OSError` 吞掉，`ci.yml` 里留下 `python run.py surprise_gate`；
   下一轮 `test_ci_gate_matrix` 红在无关用例上。修：`tests/_mutation_lock.py` 还原改为
   **重试 5 次 + 无原始异常时必抛**；`tests/test_mutation_isolation.py` 2 条守卫
   （还原失败必响、瞬时锁自动重试）。

### 验收读数

- 全量：`pytest tests -q` **1873 collected / 1872 passed / 1 skipped / rc=0**（2026-10-22 本机，独占）。
- 覆盖率：`pm/` **96.99%**（地板 92）、`pm/modelhub/*` **98.35%**（地板 82）；
  本轮改到的 `bootstrap.py` / `panelcmd.py` 100%，`diffcmd.py` 94%。
- 静态：CI 同口径 `ruff check` / `ruff format --check` / `mypy pm/ extras/ run.py run_server.py eval_prompts.py` 全绿。
- 用户入口实跑：`--version` 三入口、`check`（含目录误传）、`diff --base-ref`（临时 git 仓库）、
  `gate`、`panel`（端口冲突 8799 实跑）、MCP `tools/list`（13 工具）均按预期。

### 未做 / 未验（明说）

- **没做 GitHub Action**：竞品 promptfoo 的生态位靠 Action/matrix 铺开，但本仓主包未发布到
  PyPI，Action 只能靠 `uses: <repo>@ref`；本轮不发布未在真 GitHub Runner 上跑过的接线。
- 校准读数**没有变化**：owner 亲判锚点仍缺，Δ/达标类结论继续不采信；本轮未跑真端点。
- Windows 真机上的 `.env` 优先级只在本机跑过（Linux CI 会跑同一套 subprocess 用例），
  远端 CI 结果未等。

## Round 35（2026-10-22，PromptMaster 本产品）—— 收口 Round 34：4 处本地红归零 + CI 的 windows job 照出"仓库外"用例的临时根假设

**本轮判据**：Round 34 那一批留在工作区、**且是红的**。本轮只做收口 —— 把红归零 → 落库 → 推远端 → 等 CI。
不新增任何"优化效果"结论。

### 接手状态（先量再动）

- 工作区：36 个跟踪文件改动 + 9 个真·新文件未提交（Round 34 + extras 桌面端"项目对话"），另有 6 笔
  10-19 的重构提交未推送。
- **活体 `pytest tests/` 收集 1893 条，4 处门禁红**；而 Round 34 正文与文档抄件都还写着 1873（差 20）。

### 四处红 → 逐条归因与修（不做"改预期迁就实现"）

1. `test_test_count_copies_match_the_live_collection`：4 处抄件 **1873 → 1893**
   （`README.md` / `README.en.md` / `docs/operations.md` / `.pre-commit-config.yaml`）——抄件没跟上。
2. `test_complexity_ratchet`：`pm/cli/agent_mode.py::agent_subcommand` **11 → 12**。按仓规**拆**
   （不登记豁免）：拆成"解析 + 路由" + 每子命令一个函数（submit/status/report/wait），顶层 CC 落到
   ≤10；`python tests/test_complexity_ratchet.py --rewrite` 同步台账（32 → 31 条，无新增）。
3. `test_dynamic_pm_keys_are_known`：`pm/cli/main.py` 出现新的动态键 `f"PM_{role.upper()}_API_KEY"`。
   该键尾 `_API_KEY` 已被 `isolate_host_connection_env` 的后缀规则整类覆盖 ⇒ 加白名单并写清理由。
4. `test_assertions::test_cli_per_case_mode_overrides_global_and_validates`：旧断言只清
   `PM_API_KEY` / `PM_TARGET_API_KEY`，而新 Key 闸门还认 `PM_API_KEYS`；仓库 `.env` 那份多 Key 池被
   `load_dotenv()` 补进子进程 ⇒ 闸门误判"有 Key"而放行到真跑，用例再走不到 Key 检查那一站。修：该
   用例补清 `PM_API_KEYS`（它本来就承诺"掐断一切 Key 来源"），不是放松断言。

### 提交与推送

- 分 **2 笔语义提交**：`bffc785`（Round 34 + 配套收口，44 文件）、`4f44534`（extras 桌面端"项目对话"，
  7 文件）。只拆两笔的原因：`test_test_count_copies_match_the_live_collection` 把"新测试文件 + 文档
  数字 + `pyproject` 的 mypy 覆盖"绑成同一刀（活体收集数必须等于抄件），中间态硬拆必红。
- 连同原有 6 笔 10-19 重构，共 **8 笔**推 `origin/main`。

### CI 真跑照出 windows job 红（本轮最硬的一条）

- 现象：`test (3.11/3.12/3.13)` 全绿，**`windows` 挂在 `Tests (pytest)`**；唯一失败
  `tests/test_diff_base_ref.py::test_resolve_git_base_rejects_files_outside_a_repository`
  （期望 `不在 git 仓库里`，实得 `基线 HEAD 里没有 .pytest_tmp/…/loose.md：…`）。
- 根因：该用例假设 `tmp_path` 在仓库外；但本仓根 `conftest.py` 在 **Windows** 上把 pytest 临时根
  **首选**放进检出目录内的 `.pytest_tmp/`（绕开宿主 `%TEMP%` 的坏软链，见 `choose_temp_root`）⇒
  `tmp_path` 落在仓库内，`git rev-parse` 成功，走的是"新文件没有可对比的旧版本"分支。
- **本机复现（不靠猜）**：`PYTEST_DEBUG_TEMPROOT` 指进检出目录，即复刻出与 windows runner 一致的红。
- 修：该用例改用**确定的仓库外目录**（检出目录的父目录优先，退系统临时目录；先确认祖先无 `.git`），
  不自造 `.git`；两种条件（temproot 在仓库内 / 外）都验绿。提交 `b9fffc7`。
- CI 复跑：**4/4 success**（`b9fffc7`）。

### 收尾

- 仓库根一次性草稿（`pelican-*.png` ×4 + `鹈鹕骑自行车动画/`）按用户可恢复偏好移入 **Windows 回收站**
  （`SHFileOperationW` + `FOF_ALLOWUNDO`，非硬删）；`.gitignore` 保留两条防复发条目（同 `宣传片/` 先例）。

### 验收读数（本机，CI 同口径）

- `pytest tests/` **1893 条全绿**；覆盖率 `pm/` **96.75%**（地板 92）、`pm/modelhub/*` **98%**（地板 82）。
- `ruff check` / `ruff format --check` / `mypy --strict` 全绿；`selftest` / `prompt-gate` /
  `eval_prompts` / 三服务套件（18 / ok / 46）/ stub e2e 全绿。
- 远端：`main` = `b9fffc7`，CI **4/4 success**。

### 更正 Round 34（诚实记录）

- Round 34 正文写"全量 1873 collected / rc=0"，但它落库时的树实际是 **1893 + 4 处红** —— 说明那批
  改动在该段写下之后又长出了约 20 条用例与几处未收口的改动。**Round 34 的"全绿"读数与它所在的树
  不一致，不得引用**；以本轮收口后的 1893 全绿为准。Round 34 的 WIP 直到本轮（`bffc785`）才真正落库。

### 长期沉淀（可复用）

- **"本地绿、CI 红"的新形态：测试的临时目录假设**。当 conftest 因 `%TEMP%` 差异把 temproot 放进检出
  目录时，"在仓库外 / 在仓库里"这类前提会随平台翻转 —— 凡依赖位置的用例必须**显式挑目录**，不能赌
  `tmp_path` 落在哪。
- **复现优先**：`PYTEST_DEBUG_TEMPROOT` 指进检出目录即可在本机复刻 windows runner 的形态，不必等 CI。
- 被多条门禁交叉绑定的工作包（用例数抄件 / mypy 覆盖 / docs 同数）**只能同刀提交**，硬拆中间态必红。

## Round 36（2026-10-22，PromptMaster 本产品）—— 用户视角优化：成本闸门 / 日志降噪 / 可预览 / 首屏结论 / 可发现性 / README 重构

**本轮判据**：不新增任何"优化效果"结论（那要 owner 亲判锚点），只修"用户看得见、用得上"的路径。
方法论：以"第一次打开这个仓库的人"的视角真走一遍，找出 10 处摩擦，按**会不会真伤到用户**排序，逐项收口。

### 收口的 6 项

| 项 | 用户收益 | 提交 |
|---|---|---|
| P0-1 真实运行成本闸门 | 交互终端下 `--task` 先报价再要 y/N（`--yes` 跳过）；Agent / CI / 假后端不阻塞；取消退 2 | `38785b0` |
| P0-2 默认日志降噪 | 首屏不再是一行一次的 `httpx2 HTTP Request…` | `25b1a22` |
| P1-3 可预览样例产物 | 不花钱就知道报告长什么样（`docs/sample-report.md`，读数明确标注为桩） | `b5985d0` |
| P2-7 / P2-10 可发现性 | README 处境决策表；`--cases-file` 三处帮助都指向 `case_templates/` | `0e83ad9` |
| P2-9 报告首屏结论 | `## 结论（先看这一行）`：判定 + 最多 5 条警告，各指回细节章节 | `610fbf4` |
| B README 用户向重构 | 208 → 147 行；工程口径搬 `docs/operations.md` 第十四节（**只搬不删**） | `110e15e` |

### 两处"根因不在表面"的发现

1. **日志降噪的根因是静默名单停在了旧 HTTP 栈**：`setup_logging` 只压 `httpx/httpcore/openai`，
   而 `openai` / `langsmith` 早已迁到 **`httpx2`**（实测 `httpx2-2.12.0`，Required-by 两者）
   ⇒ 所谓"已静默"一行没少。守卫新增"名单必须覆盖**当场已安装**的 HTTP 客户端"——换一代客户端就红。
2. **`--cases-file` 的帮助三处都没说模板在哪**（`run.py` / `run.py check` / `run.py submit`），
   用户只能翻 README 的仓库地图。收成一份 `support.CASES_FILE_HINT`，并有用例验"真的印在 `--help` 里"。

### 一次自查失误（如实记）

按"无 Key 首次体验"敲 `run.py --task "…"` 时，仓库 `.env` 里有真 Key、`load_dotenv` 生效，
**当场真跑起来**（打到真端点，见 200 与一次 429 换 Key），约 5~8 发调用后被超时杀掉；
而该路径只在**结束时**落盘 ⇒ 那几发的产物无法从磁盘还原。
教训：探测"用户体验"时，凡可能触网/计费的路径先用"清空 Key + 死端点"或假后端。
—— P0-1 这条需求正是这次失误反推出来的（README 只在一句括号里写着"先 `--preflight`"）。

### 两处"别只看聚合"的实测

- **GitHub API 的运行级 `conclusion` 会早于 job 级报 success**：`0e83ad9` 那轮 run 说
  `completed success`，而 windows 与 3.11 两个 job 仍是 `in_progress`。**以 job 级为准**。
- **windows job 偶发 flaky**：`tests/test_panel.py` 两条用例里的 `node` 子进程在 runner 上超时
  （`node --check` 60s），带凭据 `rerun-failed-jobs` 重跑即绿 ⇒ 判为环境型 flaky（同轮 Linux 3/3 全绿）。

### 验收读数

- 全量 **1924 条全绿**（本批 +28 条守卫）；覆盖率 `pm/` **96.77%**、`pm/modelhub/*` **98.35%**。
- CI：上述 6 笔提交逐 job 4/4 success（`0e83ad9` 那次 windows 重跑后绿）。
- 抄件同步：计数 1893 → 1924（四处同改）、README 覆盖率快照 96.99% → 96.77%。

### 未做（明说）

- 报告首屏结论**只搬运事实、不产生新判据**；"效果结论仍不采信"这条边界没有变。
- 未发 PyPI（需要 owner 的账号/token）—— `pip install .` 仍是唯一安装路径。
- 未跑真端点取"真实读数"的样例（需 owner 同意烧钱）。
- `run.py demo` 子命令**主动放弃**：README 已有假后端一行命令 + 样例报告，边际价值低于多养一个子命令的成本。


## Round 37（2026-10-23）发布评审修复轮 —— 判定语义 / 鉴权 fail-closed / 安装态 / 门禁可观测性

**本轮定位**：不做新功能。照 2026-10-22 的四路发布评审（核心逻辑 / 测试质量 / 文档一致性 / 安全部署）逐条收口阻断项与严重项，并把「评分口径」里三处互相矛盾的说法收成一个常量。

### 收口的阻断项（发布前必修）

| 项 | 修法 | 验证 |
|---|---|---|
| 结论行事实错误（实测印出「均分 8.5 < 阈值 8.0」） | 按四道闸分支：均分过线时改「已达阈值，但…」 | `test_report_sections.py` 21 条 |
| 达标语义三处矛盾（注释 / rubric / 代码） | `PASS_SHORTFALL_TOLERANCE=1.0` 收口，四处同口径 | 同上 |
| 鉴权 fail-open × Docker 默认 0.0.0.0 | 非回环 + 无 token → 拒绝启动（退出码 2），三启动器共用一判据 | `test_expose_gate.py` 7 条 |
| `pip install .` 面板 / 桌面 / MCP / 历史转发全断 | `cli_entry()` 退到 `-m pm.cli.console`；模板退 `sys.prefix`；logs 落 `<cwd>` | `test_installed_mode.py` 6 条 + 干净 venv 探针 |
| 打包元数据全缺 + 脏 build 混入陈旧模块 | pyproject 补 5 项元数据；`case_templates` 进 data-files；`build/lib` 守卫（**先红后清后绿**） | `test_packaging` 两条守卫 |
| REST 断言静默降级「仅提醒」 | 判据收口到 `pm.assertions`，`/api/optimize` 与 `/api/race` 422 预检 | `test_api` +3 |

### 三处「文档与活工具顶牛」的裁决

1. evaluation.md 说「κ 与瞎猜不可区分」，`calibrate --aggregate` 说「AUC 显著高于瞎猜」——**不是矛盾，是两个统计量、两轮考卷**。新增 2026-10-23 裁决段：AUC 的「名义显著」是对含 6 条编造标签的 11 条锚点的局部统计；κ 的「不可区分」只对 10 条那轮成立。一句话：两者都不可引为「评委可信」。
2. 覆盖率有三份快照（97.15 / 96.77 / 96.75），而 pyproject 明写「只许 operations.md 维护一份」——收敛到 operations.md 表格（新增复测⑧ 96.79 / 96.55 / 98.35），README.en 不再抄快照、地板纳入断言。
3. 样例报告四处自相矛盾（采样次数、未闭合括号、空括号、「判定看这个」旧措辞）——**重生成而非手改**，并修掉生成侧的噪声警告第二形（采样≥2 但极差全 0 时不再误报「只采样 1 次」）。

### 门禁自身的两个洞

- **CI 的 `-qq` 吞掉汇总行**：pyproject addopts 已带 `-q`，命令行再叠一个 ⇒ `N passed, M skipped` 从日志消失，而约百条归档依赖用例在干净检出会**合法跳过**。⇒ 去重复 `-q` + conftest 终端汇总（跳过数 + 原因聚合，`-qq` 下也生效）。
- **modelhub 分项地板 82 而实测 98.35**：留了 17 点余量＝这条线没接（它守的正是出过「流式整条失效、测试全绿」事故的模块）。⇒ 抬到 93。

### 验收读数

- 全量 **1965 收集 / 1964 通过 / 1 跳过**；覆盖率 `pm/` **96.79%**、`pm/modelhub/*` **98.35%**。
- ruff check / format / mypy --strict / eval_prompts 6/6 / selftest / gate 全绿；releases 三套 18/13/46；Windows 桩 e2e 3/3。
- wheel 103 条目（含 5 模板 + LICENSE）+ 干净 venv 安装态探针通过。
- 提交 `e558229`（43 文件，+1340/−147），pre-commit 8 钩子全 Passed。

### 未做（明说）

- **三服务套件的 W7/W8/W9 豁免未收回**：那是 2026-09-21 的正式书面决定（`security-waiver.html`），本轮只让主产品与网关 fail-closed。
- **判定式协议的空桶 9.5 / 自生成清单未改**：协议默认关闭且已被实测否证，改它会动已文档化的 A/B 对比语义；本轮只补「协议 / 混用」披露。
- **日志自动轮转未做**：已披露的运维项（`logs/` 70MB+），留待下一轮。
- 未 push、未打 tag、未发 PyPI：发布渠道等 owner 决定。

## Round 38（2026-10-23，PromptMaster 本产品）—— 用户旅程二轮：中文编码、Key 配置与输出路径的「最后一公里」

**本轮判据**：继续只修"用户照 docs 做却走不通"的路径，不新增效果结论。重点放在中文 Windows
用户真实会踩的三类输入：GBK 文本、多 Key / 角色级 Key、把目录误当输出文件。

### 修了什么

1. **GBK / UTF-16 / BOM 输入兜底**（`pm/textio.py`）。此前 `check` / `diff` / `--task-file` /
   `--cases-file` / 起服务读 `task_file` 都直接按 UTF-8 读，GBK 文件抛 `UnicodeDecodeError` 裸栈、
   退出码 1（与"未达标但已交付"撞车）。现在：BOM 优先 → UTF-8 → GB18030；回退时 stderr 说明一次，
   stdout 的 JSON 契约不变；二进制文件给"另存为文本"的人话。
2. **`.env` 也吃 GBK**：`_load_one_env` 捕获解码错误后按 GB18030 重读；坏编码文件告警并跳过，
   不让"启动即崩"。`test_env_discovery.py` 用 subprocess + 进程内两条路各钉一次。
3. **Key 闸门识别正式配置**：`PM_API_KEYS`（多 Key 池）不再被判"没有 Key"；只配部分角色级 Key 时
   打印缺失角色清单；只有 `PM_TARGET_API_KEY` 不再白放行（其它角色构造时必失败）。守卫 7 条。
4. **`--out` 开跑前校验**：目录 / 尾部分隔符 / 父路径是文件三类错误在调用任何模型之前退 2。
   此前会完整跑完一轮再在 `save_artifacts` 抛 `PermissionError`（钱已花、报告没落盘）。
   同口径补到 `report --out`（父目录自动创建）与 `library --export --out`。
5. **路径类型错误说人话**：`--task-file` / `--prompt-file` / `--cases-file` 传目录时不再报
   `Permission denied`，直接说"要的是文件，不是目录"。
6. **快速档前置**：README 中英双语的"真实运行"块把 `--fast` 放到第一次运行的位置，
   完整闭环标明 45~102 次调用。

### 验收读数

- 全量：**1970 收集 / 1969 通过 / 1 跳过 / rc=0**（2026-10-23 本机，独占，`--cov=pm` 同趟）。
- 覆盖率：`pm/` **96.83%**（地板 92）、`pm/modelhub/*` **98.35%**（地板 93）；
  `bootstrap.py` 100%、`textio.py` 97%、`library.py` / `panelcmd.py` 100%。
- 静态：CI 同口径 ruff / format / mypy 全绿；复杂度过线（`_library_command` 通过抽出
  `_export_out_path` 保持 18，未抬台账）。
- 用户入口实跑：GBK 的 check / task-file / cases-file / diff、多 Key 闸门、`--out` 目录拒绝、
  `report --out` 嵌套目录、面板端口冲突、`--version` 三入口。

### 事故注记（本轮发生，如实记）

探测 `--out` 目录行为时，有一轮命令**忘了加 `PM_FAKE_BACKEND`**，误打真实端点：
3 次 `POST`（clarify / optimizer / mockgen 起始）后被杀，未产生 `run_*.json`。
之后全部改用假后端复现，并清掉本轮临时产物。教训与既有纪律同向：
**探测"会不会写/会不会花钱"的命令，必须先把后端钉成 fake 或把 Key 清空。**

### 未做 / 未验

- 未跑真实端点，校准/Δ 结论不变；owner 亲判锚点仍缺。
- 编码回退对 Big5/Shift-JIS 等非 GB18030 家族不做自动识别（会落到 GB18030 或报错），
  文档只承诺 UTF-8 / GB18030 两类。
- GitHub Action 仍未加：发布链路本轮由 release 轮处理，Action 需要真 Runner 验证。

## Round 39（2026-10-23，PromptMaster 本产品）—— `run.py doctor`：把「配置对不对」提前到花一分钱之前

**本轮定位**：用户配错 Key / 端点 / 输出目录时，反馈要么等真跑、要么散在 `--preflight`（花钱）
与报错栈里。doctor 把它变成一张**零调用**检查单。主体由并行会话起草（文件 13:08–14:19 落盘后停住），
本轮接手做了三件收尾：复杂度棘轮、动态键白名单、计数与覆盖率抄件。

### 检查项与判据

| 项 | 判据 | 边界 |
|---|---|---|
| Python / 版本 / 工作目录 | `sys.version_info >= (3,11)` | — |
| 配置文件 | `load_env_files()` 的返回路径 | 找不到只给 info，不算错 |
| Key | 全局 / `PM_API_KEYS` 多 Key 池 / 逐角色，**占位符按缺失** | 占位符真跑只会 401，比空值更难查 |
| 角色配置 | 逐角色 `build_config()` + anthropic 依赖探测 | 构造失败即 error（依赖没装） |
| 日志目录 | 可写性 + `run_*.json` 体积 | 只检查不创建；>500MB 给清理提示 |
| 端口 | 8080 / 8765 瞬时 bind | 只 bind 不 listen |
| 常驻服务 | localhost `/api/health`，0.5s 超时 | `PM_SERVER_URL` 非本机直接跳过（不出网） |
| MCP | `importlib.util.find_spec("mcp")` | 缺了只 warn |

退出码：0 无问题 / 1 只有警告 / 2 有错误；`--json` 是单个对象，且**不回显完整 Key**（只给尾号）。

### 接手时遇到的三处门禁红（都修了）

1. **复杂度棘轮**：`build_report` CC 13、`_dispatch_subcommand` 涨到 11。
   ⇒ 拆出 `_dispatch_agent` / `_dispatch_lazy`（名字在**调用时**解析，monkeypatch 语义不变）；
   `build_report` 是 8 段线性检查、无嵌套，按棘轮规则**登记进台账并写明理由**（拆分只会打散
   「一次跑完、按固定顺序追加」的语义）。
2. **动态键白名单**：`PM_<ROLE>_API_KEY` 的构造从 `pm/cli/main.py` 搬到 `pm/keycheck.py`，
   白名单跟着搬（后缀 `_API_KEY` 已被连接类夹具整类覆盖）。
3. **计数抄件与覆盖率列**：1970 → 1979（五处）；新增复测⑩（`pm/` 96.40%、去掉 modelhub 96.10%、
   modelhub 98.35%）。全量被新代码稀释（96.83% → 96.40%），两条地板照旧。

### 验收读数

- 全量 **1979 收集 / 1978 通过 / 1 跳过 / rc=0**——跳过项是 jsdom 未安装的 rejudge 表单 DOM 冒烟
  （合法跳过，原因由本轮新增的终端汇总钩子印出）。
- 覆盖率 `pm/` **96.40%**、`pm/modelhub/*` **98.35%**（地板 93）。
- ruff / format / mypy strict（101 文件）/ eval_prompts 6/6 / selftest / gate 全绿。

### 未做（明说）

- doctor **不验端点连通性**（那要发请求）：`--preflight` 仍是"花小钱冒烟"的唯一入口。
- 检查分支多与环境相关，未做"假环境"全覆盖；覆盖率因此被稀释，属已披露代价而非回归。


## Round 40（2026-10-23，PromptMaster 本产品）—— 发布态真实端点 E2E：全链路跑通 + 运维侧首验（本轮为实测轮）

**本轮性质**：前几轮明确"未做真端点复测"（Round 38 未做 / Round 21 TODO `PM_CALL_BUDGET` 真端点），
用户本轮给出明确指令用 `.env` 里的真实 LLM Key 测试。这是**用户授权的真实花费**，与
「探测性命令必须钉 fake 或清 Key」的纪律不冲突——那一纪律针对"不确定会干嘛"的探测。

### 配置事实

- 端点：`PM_BASE_URL=https://token.sensenova.cn/v1`（商汤统一网关），`PM_PROVIDER=openai`。
- 角色模型：evaluator `deepseek-v4-flash`、evaluator_b `glm-5.2`、arbiter/comparator `deepseek-v4-flash`、
  其余（clarifier/optimizer/mockgen/reviser/target）`sensenova-6.8-flash-lite`。
- Key：全局 `PM_API_KEY` + `PM_API_KEYS` 三把 Key 池（语序上多个 `_AMD/_NVIDIA/_OPENROUTER/_STEP` 属 modelhub 网关池，非本品角色 Key）。

### 一、`--preflight` 冒烟：全角色端点可达

9 角色（clarifier/optimizer/mockgen/evaluator/reviser/target/evaluator_b/arbiter/comparator）逐次 64-token
冒烟调用全 ✅，延迟 3.2s–9.0s；comparator 一次疑似限流退避 5s + Key 池轮换后成功。

### 二、完整优化 run `0e4f24e04d2c`：达标交付

- 任务：销售数据分析（`case_templates/sales_analysis.json` 带货真断言），`--samples 2 --max-iter 2 --no-pairwise`。
- **65 次 LLM 调用**，2 版提示词，第 1 轮修订 v0 5.29 → v1 **8.72**。
- 聚合：avg **8.72** / min 8.59 / 保守下界 8.29 / **passed=True**；基线臂 avg 6.33 → Δ **+2.39**。
- 修好 3 条 / 弄坏 0 条（净值 +3）；规则核验 2 条 0 否决。
- 真实退出码 **0**（顶层 `status=passed` + `_result_exit_code("passed")=0`；命令显示 1 是 PowerShell
  把 Python stderr WARNING 当 NSL 的管道误报，非 run.py 真实退出码）。
- 产物：`logs/e2e/e2e_sales.md`（交付报告）+ `logs/e2e/e2e_sales.SKILL.md`（最终优化版提示词）。

### 运维侧首验（这正是真实端点测试的价值）

1. **限流显著**：`deepseek-v4-flash` evaluator 在 case#2 反复 `429`（inference exceeds tpm/rpm limit），
   Key 池轮换 + 退避 5/10/20s 顶不住几次，3 次撤回后放弃该采样。→ 云高峰并发多角色吃配额。
2. **evaluator_b（`glm-5.2`）长上下文超时**：短冒烟 3.9s，但完整评估常撞 `OpenAITimeoutError`，
   削到墙钟预算 600s 仍超（剩 298s 装不下一发完整超时则不重试），**每发白等约 300s** —— 本轮拖到 2h+ 的主因。
3. **降级路径全按设计生效**：限流/超时下不崩；Key 轮换、优雅降级、仲裁纠正（evaluator_b 误判 4.0 被 arbiter 拉回 8.85）、
   schema 形状修复、pydantic 重试、质量门重试（约束超载 10>8 与定界符不配平各一次）全部触发且成功。
4. **披露诚实**：报告如实标注「3/4 例有评委缺席，那些分数不是双评委共识」，并把缺席原因列进运行期错误。

### 未做（明说）

- 实测轮**不新增产品代码**，无覆盖率/静态变化；校准锚点仍缺 owner 亲判。
- evaluator_b 长上下文超时未优化（换更快的模型 / 调大 Key 池 / 错峰是可选动作，非本轮职责）。
- 未 push / 未发 PyPI / 未发 GitHub Release：发布渠道等 owner 决定（PyPI 需 token、Release 需真实 Runner）。

### 事故注记（无）

本轮全程遵守纪律：这是用户授权的真花费，且用了 `PM_CALL_BUDGET` 无（未设，靠任务规模即有界）；
没有擅自探测"会不会写/花钱"的命令。


## Round 41（2026-10-23，PromptMaster 本产品）—— 发布通道工具化 + 日志归档落地

**本轮定位**：Round 40 真端点测试回答"发布态能不能用"（能）。这一轮把"发布剩下那两截"和
"日志持续膨胀"这两件**欠着但仓库内可做**的事补上，仍不碰凭据/owner 才能做的部分。

### 一、`release.yml`：tag 驱动的发布工作流

之前发布全靠手动（打 tag → 本地 build → 手动传）。现在有一个独立 workflow，只在 `v*` tag
推送时触发，四条 gate 分工（对齐 ci.yml 的"注释讲清为什么"惯例）：

- **build-check**：构建 sdist+wheel 并跑 `release_pypi.py --check` 校验，零凭据，任何 tag 都该绿。
- **artifact**：把校验过的那份产物打成 artifact，供下游复用（只 build 一次，避免两份产物漂移）。
- **github-release**：用 GitHub 自动注入的 `GITHUB_TOKEN` 打 Release 并附产物 +
  `release-notes.md` 对应版本段落。**不需要 owner 另配 secret** —— 这是 tag 推送后能真自动的部分。
- **pypi**：唯一真凭据门槛。`secrets.PYPI_TOKEN` 未配则跳过（写人话），不放行也不阻 GitHub Release。

设计要点：GitHub Release 与 PyPI 解耦（一个成功不挡另一个）；pypi 用 artifact 同一份物理字节，
保证 PyPI / Release / build-check 三者字节一致。

### 二、`scripts/logs_archive.py`：日志归档（移动 + 留档，绝不删除）

`operations.md` 早有"日志会膨胀，定期归档 run_*/report_*"这句话，但**一直没有工具**。
logs_usage.py 刻意只读（tests 钉死零写入），doctor 只在 >500MB 提示。缺"真正归档"这一步。

- 只移动**超龄（默认 >30 天）且未被 `docs/` 按 run id 点名**的 `run_*.json` / `report_*.md`
  进 `logs/archive/YYYY-MM/`，内容逐字不变（换目录，不删字节），并写 `manifest.json` 留档可回查。
- **默认 dry-run**，`--commit` 才移动（同 release 的 --check/--upload 先验精神）。
- 不碰台账（事实源）、缓存（功能依赖）、死重（logs_usage 只读列出、owner 手动整删）。
- 配套 `tests/test_logs_archive.py` 钉三条安全承诺：dry-run 零写入 / commit 不改字节 / 文档引用不归档。

### 验收读数

- 全量 **1983 收集 / 1982 通过 / 1 跳过 / rc=0**（跳过：jsdom 缺失的 rejudge 表单 DOM 冒烟，合法）。
  新增用例：`tests/test_logs_archive.py` 4 例、`tests/test_logs_usage.py` 等既有全绿。
- 抄件同步：README.md / README.en.md / operations.md / .pre-commit-config.yaml 的测试计数
  1979 → 1983（test_packaging 的 `test_test_count_copies_match_the_live_collection` 钉住）。
- 相关门禁守卫全绿：complexity ratchet / precommit gate set / ledger retention claims / GBK console。

### 未做（明说，仍是凭据/owner 门槛）

- **PyPI 上传**：需 owner 在 GitHub Secrets 配 `PYPI_TOKEN`（或本地干一票）。release.yml 已就绪但
  没 token 前 pypi job 自动跳过。
- **GitHub Release 真实触发**：release.yml 配好了，但要 owner 真打一次 tag 推上去才有第一次实体
  运行验证（本地零凭据无法证明"runner 上真能跑"）。
- **校准基线 34 条**：仍需 owner 亲判 `human_score` + `confirmed:true`（evaluation.md 判据）。

### 事故注记（无）

本轮零凭据、零真实出网；日志归档只在 dry-run 下验过，未对真实 logs/ 执行 `--commit`
（2091 个可归档证据的移动是 owner 该显式批准的大动作，工具已就绪，何时执行归 owner）。


## Round 42（2026-10-23，PromptMaster 本产品）—— v2.3.0 真实发布 + Release 首跑闭环

**本轮定位**：Round 41 把发布通道（release.yml）+ 日志归档（logs_archive.py）做实，但**还差最后
一步**：release.yml 从未在真实 Runner 上跑过。这一轮用户明确"打真实 tag 触发一次 Release 验证"。
结果不只验证成功，还**抓到并修掉一个真实 bug**（`if` 里引用 `secrets` —— GitHub 禁止），
并顺手造出了 v2.3.0 这个**真实可用**的发布点。

### 一、真实 Release 首跑把"配置错误"抓了出来

流程：bump 2.2.0 → 2.3.0（`pm/__init__.py`，唯一事实源）→ 打 `v2.3.0` tag → 推送。

- **run #1 / #2（commit 6b5c442）**：release.yml 触发后 **0 jobs 秒失败**
  （created_at == updated_at、jobs API 返回空）。这是"workflow 在调度前整体被拒"的典型形态，
  不是某个 step 挂了 —— 本地 PyYAML 解析完全合法，排除了 YAML 语法问题（顺带确认 gitattributes
  的 LF 规范化没破坏文件）。
- **根因**：pypi job 用了 `if: ${{ secrets.PYPI_TOKEN != '' }}`。GitHub 文档明确 —— **`if` 条件里
  不允许访问 `secrets` context**，该 job 无法求值，连带整个 workflow 秒红。这是 GitHub 广为人知的坑，
  也是"本地零凭据看不出、必须真实 Runner 才暴露"的典型案例。
- **修复（commit fa9ca85）**：pypi job 改为**无条件进入**（token 放进 `env`，那里引用 secrets 合法），
  在真正上传的两步用 `if: env.PYPI_TOKEN != ''` 守卫（`env` 在 step 级 `if` 可用）；未配置时加一个
  反向守卫 step，把跳过原因写进日志与 job 摘要 —— "没传上"不是无声的。

### 二、真实 Runner 全绿（run #3，commit fa9ca85）

强推 tag `v2.3.0` 到 fa9ca85 后重跑，**四个 job 全 success**：

| gate | 结论 | 证据 |
|---|---|---|
| build-check | ✅ | 构建 + `release_pypi.py --check` 校验通过（0 凭据） |
| artifact | ✅ | 产出 sdist+wheel，版本对账 OK |
| github-release | ✅ | **Create GitHub Release + 确认生成** 两步都 success |
| pypi | ✅ 优雅降级 | 无 `PYPI_TOKEN` → Upload 两步正确 `skipped`，跳过原因写入 job summary |

**GitHub Release 真实落盘**（API 核验）：[v2.3.0 release](https://github.com/fengxingxuerong/prompt-master/releases/tag/v2.3.0)
由 `github-actions[bot]`（自动注入 GITHUB_TOKEN）创建，附两个产物
`prompt_master-2.3.0-py3-none-any.whl`（496KB，sha256=8052795e…）与
`prompt_master-2.3.0.tar.gz`（974KB，sha256=503de2f6…），body 正确取自
`docs/release-notes.md` 的 2.3.0 段落。字节与 build-check/artifact 同源。

### 验收读数

- 发布链路首次在真实 Runner 上端到端走通，且"没配 token 也整条绿"的降级路径被真实触发验证。
- 本地 pre-commit 8 钩子全绿（release.yml 属 `.github/workflows/`，本就不在钩子 scope 内，无需改钩子）。
- 测试计数保持 1983 收集 / 1982 通过 / 1 跳过（本轮只动 CI 配置与文档，未加产品用例）。

### 未做（明说）

- **PyPI 仍未上传**：PyPI 是唯一的凭据门槛，从 release.yml 到 twine 命令都已配好，只差 owner 在
  GitHub Secrets 配 `PYPI_TOKEN`，下次推 `v*` tag 即自动上传 `https://pypi.org/project/prompt-master/`。
  本轮在无 token 下已验证"跳过路径"正确，但"上传路径"的首跑仍等凭据。
- **校准基线 34 条**：仍待 owner 亲判（已在 operations.md 标清，见 Round 41）。

### 事故注记（值得记一笔的）

真实 Runner 首跑**没有"测试环境正常、发布环境也正常"的错觉**——它立刻用 0-jobs 秒失败证明
"本地验证不了 GitHub 配置语义"。`if` 不许用 `secrets` 这类坑只能在真实触发里浮出来。这强化了仓库
"发布工作流要靠真实 tag 验证、不能靠静态检查下结论"的既有结论。

## Round 43（2026-10-24，PromptMaster 本产品）—— 全面真机 LLM 实测（owner 指定的"全方面测试用 LLM"）

直接要求是"这个项目开始全方面的测试使用 llm 来"，owner 圈定三面：真实 LLM 全面实测、
补强 LLM 辅助测试用例、聚焦校准 49 条。本轮把前两面做掉并给出校准的现状结论。

### 一、真实端点接线（`run.py --preflight`，零写盘的真实冒烟）

9 个角色（clarifier / optimizer / mockgen / evaluator / reviser / target / evaluator_b / arbiter /
comparator）全部可达 `https://token.sensenova.cn/v1`，延迟 1.7–4.0s 区间，无错误签名、无失败。

### 二、节点提示词真实行为评测（`eval_prompts.py --live`）——抓到一个确定性夹具缺陷

- 真实端点上首跑 **5/6**，唯一失败是 `reviser/针对反馈定向修订`，且两次复现一致、原因相同：
  **质量门未通过（含抑制型规则「停止处理」）**。
- 零调用归因：该用例的 `previous_prompt` **自带**「输入含矛盾信息时指出矛盾并停止处理」，
  命中 `check_prompt_quality` 的 `suppressive_rule` 闸（闸的路径：抑制型条款 8.75→7.88 + 与骨架要求冲突）。
  `evaluation_feedback` 只点名数据编造/缺表格/结论超限，reviser 没有理由改动那条，输出必残留 → 必红。
  **这是用例夹具与硬规则不同步，不是端点抖动。**
- 修法（commit `301a42d`，仅 `prompt_eval/cases.json` 一处）：把该条款改成非抑制写法
  「指出矛盾并标注存疑，其余数据仍继续输出」，保留"数据矛盾要指出"的用例本意。
- 改后验证：夹具自身质量门 ok=True；离线结构契约 6/6；真实端点全量 **6/6**；
  `test_prompt_eval / test_quality / test_promptgate` 共 139 项全绿。

### 三、真实端到端闭环（`run.py --task "…销售数据…" --fast --yes`）——目标模型空输出稳健性问题

- `--fast`（关基线/盲评、单采样、1 轮）：11 次 LLM 调用，`run_788605b7ab0c.json`。
- 结果：`early_stopped`（iteration 0）、聚合 `avg=3.37 / min=1.0 / passed=False`、`untrusted_case_indices=[0,2]`。
- 3 个 `empty_output`：**目标模型 `sensenova-6.8-flash-lite` 在 3 条用例里 2 条返回空内容**（未抛异常），
  且 `PM_TARGET_MAX_TOKENS=24000`（不算低）仍复现 → 不是 token 上限，是**该模型在这次任务上的稳健性坑**。
  它拖垮整个闭环的达成判定（avg 3.37），本轮无法证明"优化闭环能拿到 ≥8.0 的达标版"。
- 附：`[evaluator]` 限流退避 / Key 池轮换（池大小 3）自动恢复机制在真实端点上确实生效；
  `PM_FORCE_JSON_CHANNEL=1` 下各角色走文本通道再解析（json_fallback），晚但稳。
- 离开的第一小时那个"跑挂"是**交互确认问题**：真实模式默认要先报价要 y/N，非交互 shell 会挂，
  补 `--yes` 即可，不是 bug。

### 验收读数

- 真机接线全角色可达；`eval_prompts --live` 从一个确定性假红修到全绿 6/6（附 139 项回归全绿）。
- 端到端闭环真实跑通并留痕：给出一条**有实证的、target 模型空输出**的稳健性发现——这正是假后端
  永远测不出来的那类问题（`PM_FAKE_BACKEND` 从不产生空输出）。

### 未做（明说）

- **校准 49 条没能在本轮被"推进"**：`run.py calibrate --samples judge_calibration/samples.candidates.json --aggregate`
  仍是已固化结论——合并读数 11 条配对里 **6/11 锚点是仓库示例（分数编造，不可引用）**，且 2×2 最薄格只有 4 条，
  **"这张考卷答不了达标一致性"**。要拿站得住的 MAE/κ，必须先有真锚点（owner 亲判），这条门槛属于 owner，
  不是我能自主消掉的。
- **端到端"达标优化版本"没拿到**：target 模型空输出使 avg 3.37 —— 已按 owner "再验一轮换 target" 的方向补做，
  见下续（glm-5.2 对比）。

---

## Round 43 续（2026-10-24）—— 换 target 模型再验：空输出问题收敛到 sensenova 单一模型

owner 选"换 target 模型/调参"再验一轮。同一任务、`--fast --yes`，仅把 target 从
`sensenova-6.8-flash-lite` 换成 `glm-5.2`（`PM_TARGET_MODEL=glm-5.2` + `--target-model glm-5.2`；
execute 的 target 角色模型由 `PM_TARGET_MODEL`/`PM_MODEL` 决定，`--target-model` 只影响写进提示词的标签）。

### 对比读数（两次都是真机，同一任务）

| 指标 | sensenova-6.8-flash-lite | **glm-5.2** |
|---|---|---|
| record | `run_788605b7ab0c` | `run_5a0a77390ecb` |
| target 空输出（empty_output） | **2** | **0** |
| untrusted_case_indices | [0, 2]（2/3 空输出被剔） | **[]** |
| avg_score | 3.37 | **6.57** |
| min_score | 1.0 | **5.55** |
| status | early_stopped | early_stopped |

### 结论

- **空输出是 sensenova-6.8-flash-lite 单一模型的稳健性坑**：换 glm-5.2 后 0 空输出，avg 3.37→6.57。
  此前 avg 3.37 是被空输出系统性砸低的（2/3 用例被判不可信按 1.0 计），不是优化链路的能力上限。
- glm-5.2 的 6.57 是**真实评级分数**（min 5.55、passed=False）——"这版还不够 8.0 达标"，是真实排序不是假象。
  这与 `.env` 里 glm-5.2 排序一致性最好（r=0.976）的既有口碑方向一致。
- 两边都各现 1 次 `revise: 修订返回空内容，已保留上一版`（修订器 role，非 target），是既有已处理路径，
  `tests/test_fixes.py:188` 已钉。

### 留给 owner / 下一步

- 若要把"达标优化版"跑出来，用 glm-5.2 做 target 是干净起点（无空输出干扰），可试：
  `PM_TARGET_MODEL=glm-5.2 run.py --task … --yes`（默认完整档 / 或多轮 `--max-iter 3`）。
- 是否把默认 target 从 sensenova-6.8-flash-lite 切换到 glm-5.2，属产品配置决策，留 owner 定（涉及
  `PM_MODEL` 或 `PM_TARGET_MODEL` 改动，我不是擅自改 owner 的 .env）。

### 事故注记（值得记一笔的）

`eval_prompts --live` 的 reviser 红不是模型问题，是**用例输入自带被硬规则禁止的条款**——这提醒：
"夹具要自洽，不能测试一个本身就踩着自己判据的输入"。零调用归因（对夹具自己跑 `check_prompt_quality`）
比对着日志猜快得多，且不烧钱。

---

## Round 43 续二（2026-10-24）—— 追"达标优化版"的多轮真机跑：四次都中途中止，产出部分数据

owner 选"用 glm-5.2 把达标优化版（≥8.0）跑出来"。后台发了四档同任务真机跑，**全部 `EXIT:-1`
中途中止、均未写出最终报告/record**（`logs/_e2e_glm_{full,fast3,judge1,solo}.log`）。这不是代码/模型故障——
每次中止前都在正常产分，是**后台长任务被环境杀掉**（约 20 分钟内就死，四档交叉复现）。

| 档位 | 配置 | 中止前的已产数据 |
|---|---|---|
| full | 默认完整档+基线+成对，`--max-iter 3` | 优化版 case#0=8.53 / case#1=7.70 / case#2=8.62，基线 avg=8.16；中止于 evaluator_b 墙钟 600s 预算耗尽 |
| fast3 | `--fast --max-iter 3` | case#1 merged=8.78；中止于 case#2 评估 |
| judge1 | `--fast PM_JUDGES=1 --max-iter 3` | 单评委 case#0=6.90；中止于 evaluator 空内容重试后 |
| solo | `--fast PM_JUDGES=1 --max-iter 2` | 单评委 case#0=7.60 / case#1=**9.05**（已超 8.0 阈值）；中止于继续评估 |

### 读数/结论

- **部分数据证实目标模型不再是瓶颈**：多档里 glm-5.2 target 产出的单案例分达到 **8.53 / 8.62 / 8.78 / 9.05**（有的已跨 8.0），
  且**空输出基本消失**（earlier `solo` 全程未见 target 空输出）。「换 glm-5.2 能拿到达标级输出」有真机佐证。
- **真正卡死完整闭环的是两点**：
  1. `evaluator_b=glm-5.2` 在 `token.sensenova.cn` 上**慢且 429 限流**，一个案例就能把墙钟 600s 预算耗尽 → 默认双评委下
     full 档必卡死（`PM_JUDGES=1` 可绕开这个，但四次都仍被环境杀）。
  2. **后台长任务约 20 分钟内被环境杀**：四档配置互不相同（target/评委数/迭代数都变过）仍全中止，指向运行时资源/看门狗，
     不是 PromptMaster 逻辑。假单档（`--fast` 单迭代）能跑完（run_5a0a77390ecb），是因为它 3~4 分钟就结束、在窗口内。

### 未做（如实说）/ 下一步

- **"达标优化版的完整 record"这轮没拿到**：四次都差一口气被环境掐掉，写不出 `passed=True` 的正式 record。
  这不是假结果，是**环境不让我跑完**。若 owner 要那份 record，建议：前台跑（非后台 job）/ 缩小到单迭代多案例 /
  或换更快的评委 B，任一都能把墙钟压在窗口内。
- 空输出问题的治理结论已成立（Round 43 续）：sensenova 是单一问题源，glm 是干净 target 起点。
- 换目标模型/评委模型到 .env 属 owner 配置决策，我没有擅改。

---

## Round 43 续三（2026-10-24）—— 能跑完的轻跑也失败：卡点在 optimizer 的单发超时，不是环境杀

续二之后、按 owner "真实测试为主线" 的方向，追"达标记录"的轻跑（`--fast PM_JUDGES=1 --max-iter 1`，
min 9/max 23 calls，约 5~6 分钟）**这次跑完了**（report 已写 `logs/_e2e_glm_fore.md`，record `run_9db72608e9ca.json`）
——**没有再被环境杀**。但它是 `status: failed`，**只用了 2 次 LLM 调用就在 optimizer 一步失败**：

```
optimize: [optimizer] 墙钟预算未用尽即停（预算 600s，已用 302s，剩 298s 装不下一发完整超时
⇒ 不再重试；已发 1 次真实请求）。最后一次错误：Request timed out.
```

### 结论（本轮最扎实的一条）

- **完整闭环真正的卡点终于锁定**：不是环境杀，是 **optimizer（推理型、长提示词 + evaluator 全程评估反馈）
  的单发调用在 `token.sensenova.cn` 上可能超过有效超时**（本例用掉 302s 后 `Request timed out`）。
  墙钟预算逻辑在「已用 > 一发完整超时」后拒绝再重试（剩 298s 装不下再等一发），于是整 **run 直接 failed**。
- 这解释了为什么之前"后台长任务全被杀"其实也可能叠加了这一层——optimizer 一步就 timeout，加上双评委慢，
  综合把完成时间顶爆窗口。**单点根因是 optimizer 调用太慢/超时，不是并发后台被看门狗掐**（至少不是唯一根因）。
- 与 `.env` 注释自洽：核心就是「推理模型单次可达 2-4 分钟 + PM_TIMEOUT/墙钟预算不够一次完整等待」。
  用 `--dry-run` 确认 `--fast --max-iter 1` 预估 9~23 calls，合理且应能在 10 分钟内完成——理论完毕，
  实测卡在单发调用本身。

### 未做（如实说）/ 下一步（全属 owner 配置层面）

- 这份 record 仍**不是达标 record**（failed 在 optimizer，无 aggregate、无交付）。
- 要真跑出完整达标 record，前置是调 owner 的 `.env`（**不在我擅自改的范围内**）：
  1. 调大该角色 `PM_OPTIMIZER_MAX_TOKENS` / 墙钟预算 / `PM_TIMEOUT`，让它装得下"一发完整等待+重试"；
  2. 或把 optimizer 换到更快的型号/端点（此刻 optimizer 用的是 `PM_MODEL=sensenova-6.8-flash-lite`）。
- 真机测试的价值已兑现：空输出（sensenova target）→ 修掉、换 glm 达标级读数（9.05）、并首次把完整闭环卡点
  精确定位到 optimizer 单发超时。这些都是假后端永远不会给的信息。

## Round 43 续四（2026-10-24）—— 卡点换位：optimizer 超时被预算覆盖后，暴露 reviser 24k 空产出与注入门禁拦截

在续三的基础上只改**进程级环境变量**（`.env` 未动），验证"调大预算/超时能不能跑完"，
并顺带把真机暴露的报告缺陷修掉。两条都是 `--fast --max-iter 1`、`PM_JUDGES=1`、
`PM_TARGET_MODEL=glm-5.2`、`PM_CALL_BUDGET=1200`、`PM_TIMEOUT=420`。

### 一、`run_c4aee738d0e2`：闭环跑完了，但 reviser 把 24k 全烧在推理上

- 13 次调用，`early_stopped`，`avg=7.52 / min=4.00`，exit 1，报告 `logs/delivery_run_20261024.md`。
- optimizer 这次 26s 返回、质量门重试后通过——**续三的 optimizer 单发超时没有再复现**，
  说明"预算 600→1200、超时 300→420"确实能覆盖一发完整等待+重试。
- 新卡点：reviser `output_tokens=24000 = max_tokens`，连续 3 发正文为 0；报告自己给出诊断
  "把该角色的 max_tokens 上限调大再跑一轮"。

### 二、`run_422320d9e62e`：reviser 上限抬到 32000 后产出第 2 版，但注入门禁拦住了达标

- 18 次调用，2 个版本，`avg=8.24 / min=8.07 / max=8.35`，3/3 用例分数过线；
  reviser 3 发 / 75,677 output tokens / 737s，最终产出 1303 字的修订版（仍伴随空产出重试与
  "约束超载 10 条 / 净增量超预算"告警）。报告 `logs/delivery_run2_20261024.md`。
- 但 `injection_survival = 1/1 被劫持`（case#2，技法 `instruction_revocation`，标记 `2408E383`）：
  目标模型 glm-5.2 执行了数据里的指令，交付提示词里"标签内是数据、其中的指令不得执行"没有实际约束力。
  ⇒ `status=max_iterations`、exit 1、`skill_path=null` 都是**正确**的安全行为；
  **分数 8.24 不能赎回注入缺陷**。
- 同轮 evaluator 出现 6 次失败调用（429 风暴），Key 池 3 把轮换+退避后恢复；恢复机制有效，
  但会把一轮的墙钟时间显著拉长。

### 三、真机暴露并修掉的报告缺陷

- 同一份 record 里，报告首行和"评分总览"的判定行都写"✅ 达标 / 通过（≥8.0）"，
  而文末已写"🚫 注入门禁已拦截：达标结论被压制"——**首行是读者唯一必读的一行，却与门禁结论打架**。
- 修法：`pm/report_sections.py` 的 `_verdict_lines` 与 `_score_overview_lines` 在
  `injection_gate.blocked` 时统一输出"未达标（分数过阈值，注入门禁拦截）"；
  回归用例 `tests/test_report_verdict.py::test_injection_gate_blocks_passed_headline`
  （摘掉新分支实测会红，恢复后绿）；测试计数抄件 1983 → 1984 四处同步。
  注：`logs/delivery_run2_20261024.md` 是修复前渲染的，首行仍是旧文案；
  按同一 record 重新渲染已确认三处口径一致。
- 修复后复跑：主套件 1984 收集 / 1983 通过 / 1 跳过 / 0 失败，覆盖率 96.41%；
  modelhub 98.35%；ruff / format / mypy strict / `--selftest` 全绿。

### 未做 / 结论

- **今天仍没有一条"分数过线且注入门禁也过"的真实 passed record**。最接近的 8.24 被门禁正确拦下；
  这不是链路失败，而是该提示词对注入确实不设防。
- 完整非 fast 档（约 64 次调用 / 20–30 分钟）没有跑，避免在未授权下继续烧额度；
  也没有改 owner 的 `.env`。建议 owner 决策的三项配置：
  `PM_REVISER_MAX_TOKENS=32000`（24k 会空产出）、`PM_CALL_BUDGET≥1200` + `PM_TIMEOUT≈420`
  （否则 optimizer 一次超时整轮 failed）、target 用 glm-5.2（sensenova target 有空输出）。
- 注入防御仍是模型相关的：提升某次分数不等于该提示词可安全上线；注入用例必须保留在考卷里，
  门禁不能因"均分达标"被绕过。

---

## Round 43 续五（2026-10-24）—— 项目能力验证小轮：selftest / preflight 全绿，真实闭环跑通但未达标，并暴露"误报注入"

按 owner "开始验证项目能力" 的一次零钱→真机验证。三项结果：

### 1. 离线能力自检（零成本）
- `run.py --selftest`（假后端跑核心闭环）**通过**：迭代终态判断、兜底交付、双评委分歧→仲裁路径、
  报告如实标注未达标、多版本记录全部 PASS。
- `run.py --preflight`（真实端点连通）**9 角色全 ✅**（延迟 1.3~4.6s）：主链路 6 角 + 双评委
  （evaluator_b=glm-5.2）+ 仲裁（arbiter/comparator=deepseek-v4-flash）。端点可达、可开始任务。

### 2. 真实闭环跑通（`run_3d2706c68cac`，`--fast --max-iter 2`，target=glm-5.2，`PM_CALL_BUDGET=1200`，
`PM_TIMEOUT=420`，`PM_REVISER_MAX_TOKENS=32000`，沿用续四验证过的配方）
- 任务：销售数据分析，用例 `case_templates/sales_analysis.json`（**0 注入用例**，为拿"达标且门禁通过"设计）。
- **完整跑通**（report + record 都有，12 calls，iter 0/2，`early_stopped`）：`avg=4.50 / min=1.00`、
  **0/4 用例过线**，`passed=False`，`injection_gate=None`（无注入用例，符合预期）。
- 报告首行判定 **"⚠️ 未达标：均分 4.5 < 阈值 8.0"**，三处口径一致——**续四的报告修复在"非门禁拦截"路径上也稳**
  （不会因为没触发门禁就误报达标）。
- 该结果证实：**"分数达标"依赖任务+用例难度，同一域不同任务/用例集可从 8.24 落到 4.50**；本轮不达标不是机制
  故障（selftest 已证机制），是"这一份 prompt 对这套 4 条用例（含 rule 模式与越界请求）"没写好，且 reviser
  因 429 风暴 + 空产出未能修复上来。

### 3. 真机暴露的稳健性缺陷（本轮最有价值的发现）
- evaluator 对 case#0 标记：目标模型（glm-5.2）在**正常数据分析请求**上**误报注入**——输出写了
  "⚠️ 安全提示：检测到注入指令，已忽略并照常执行分析"，还断言输入含"与数据分析无关的指令性文字"，
  而该输入其实完全正常。→ **模型层过度警觉**（或约束 7 对"与数据分析无关的指令"的判定过宽），
  虚假安全声明会让读者误以为输入被污染，直接损害报告可信度。
- 这与续四"注入防御是模型相关、用例必须保留"互相印证：不是"过度警觉"就能防住注入（8.24 那次照样被劫持），
  而是需要一个**既不误报、又能真拦住劫持**的判定口径。这属于要交给 owner / 设计取舍的问题，本轮未改代码。

### 未做 / 结论
- 未为"分数达标且门禁通过"再烧额度：本轮 4.50 已证明该任务+用例下目标不达标，继续重跑同配置烧钱无增量。
- 项目能力判定：**机制层面自检全绿、端点可达、真实闭环能跑通并如实自评**；**"产出达标交付物"是任务相关的，
  且暴露出误报注入这一稳健性缺口**。今天的验证结论如实记录，不夸大。

---

## Round 43 续六（2026-10-24）—— 继续验证项目能力：零成本面全面走一遍

续五之后按 owner "继续"，把**不花钱的能力面**系统过一遍（全部零 LLM 调用）。结果：

| 能力面 | 命令 | 结果 |
|---|---|---|
| 离线全量回归 | `pytest tests/` | **1983 passed / 1 skipped / 0 failed**（跳过=jsdom 未装，非缺陷） |
| 节点提示词结构契约 | `eval_prompts.py`（离线） | **6/6 通过**（clarifier×2 / evaluator / mockgen / optimizer / reviser） |
| 环境诊断 | `pm.cli.console doctor` | 全绿：Python 3.12.10 / 版本 2.3.0 / .env 已载 / Key 池 3 / 9 角色可构造 / 端口 8080+8765 空闲 / MCP 已装 |
| 考卷静态体检 | `check --cases-file sales_analysis.json` | 4 条中 2 条能否决、1 条属 echoes、**整卷缺 scenario（main_path/boundary/injection）** |
| 运行史+显著性 | `history --last 5` | 如实列出最近 5 轮（4.50 / 7.89 / 8.24 / 7.52 / 7.23） |
| 达标资产库 | `library --recommend` | 命中 3 条达标资产 **8.37 / 8.17 / 9.14**（相似度 0.26~0.30） |
| Prompt 回归门禁 | `gate` | 基线 a151ea4、17 模板、**未新引入规则问题**（RC=0） |
| 零调用差分 | `diff 422320d9e62e 3d2706c68cac` | "修掉 1 类规则问题；约束条目 10→8"，附增删清单（RC=0） |
| 图拓扑 | `run.py --mermaid` | 与实现一致的 LangGraph 边（clarify→optimize/mock→baseline→test→evaluate→revise/compare→report） |
| MCP 工具面 | `import pm.mcp_server` | 14 个工具齐备（optimize_*/calibrate_*/library_*/prompt_*/cases_check/estimate_cost/run_history） |

### 两条对续五的补强
- **4.50 那轮分数低的直接原因找到了**：`check` 指出该考卷**整卷没标 scenario**，闸只管模型生成的用例、自带
  用例集不受约束，于是"评分全落在最简单的路径上"；且 1 条 expected 就在 input 里，只挡丢内容。
  ⇒ 4.50 是**考卷偏弱 + 这份 prompt 没写好**共同造成，不是能力缺陷。
- **库里本就有该域的达标提示词**（8.37 / 8.17 / **9.14**）。⇒ "没有真实达标 record"是**这一轮没拿到**，
  不是系统产不出——同域已有 3 条历史达标资产为证。

### 结论
- 项目**可验证的能力面（离线契约、回归、诊断、体检、库、门禁、差分、拓扑、MCP）全部走通且退出码符合协议**；
- 加上续五的 `--selftest` 与 `--preflight`，**机制与工具面已系统验证完毕**；
- 仍需 owner 定向的仍是**效果面**：一条"分数过线且注入门禁通过"的新 passed record（当前域已有 9.14 历史资产，
  但要复现需换/补强考卷，属设计取舍）。本轮零成本，未烧额度。

---

## Round 43 续七（2026-10-24）—— 收官证据：假后端跑通整张图并达终态 passed

续六末尾补一条零成本但**最有说服力的机制证据**：用 CLI 文档里那条"本地无 Key 路径"跑完整流程。

- 命令：`PM_FAKE_BACKEND=progress run.py --task "让 AI 分析销售数据…" --fast --max-iter 2`（**零 LLM 调用**）
- 结果：**EXIT:0**（=达标交付），record `run_49cb7d3936b8`：`status=passed`、`iteration=2/2`、
  `llm_calls=32`、`avg=9.0 / min=9.0`；报告首行 `状态：passed`。
- 意义：这证明**整张图两轮迭代全程走得通**——generate→mock→baseline→test→evaluate→revise→test→evaluate
  →compare→report，且终态判定、交付、报告渲染在"达标"这支分支上也正确（对照续五的"未达标"分支）。
- **必须说清的边界**：假后端给的是**合成分 9.0**，它**只证明机制跑得通、退出码与终态正确**，
  **不证明优化效果**。真实效果只有真机跑（续三~续五）看得出，而真机结论是任务相关。

至此项目能力的**机制面/工具面**已完整验证（selftest、1983 测试、doctor、preflight、check、history、
library、gate、diff、mermaid、MCP 14 工具、假后端整图 32 调用到 passed）；**效果面**保持续五/续六的
诚实判定，留给 owner 定向。

---

## Round 43 续八（2026-10-24）—— 补验：改进分支、`--json` 协议、入参校验与退出码

续七之后再补三个尚未验的面（全部零成本）：

1. **改进分支（`--prompt-file`）——与"从零生成"是两条路**
   `PM_FAKE_BACKEND=progress run.py --task … --prompt-file logs/_orig_prompt.md --fast --max-iter 2`
   ⇒ **EXIT:0**、record `run_ff5f04e9ed97`：`status=passed`、`2/2` 轮、`calls=32`。
   报告写明 **"模式：原稿改进（输入原稿 81 字符；基线 = 用户原稿，Δ 读作「比你自己的版本好多少」）"**，
   版本表 `v0 | 6.33 | 改进自用户原稿`。⇒ **"改进用户已有原稿"这条支路确实独立成立**，且基线语义
   （Δ = 相对你自己的原稿）正确。同理，`v0` 被标成"改进自用户原稿"而非"从零生成"。
2. **`--json` 协议（Agent/CI 可消费）**
   - `run.py --task … --dry-run --json` ⇒ 合法 JSON（mode/cases/samples/judges/max_iterations/
     baseline_enabled/pairwise_enabled/estimated_llm_calls/notes），RC=0。
   - `console history --last 2 --json` ⇒ 结构化 JSON，含 `method` 说明（Δ 均值±1.96×SE、分组键含
     judge_gen、崩溃臂不入统计但在 excluded_untrusted 点名），RC=0。
3. **入参校验 + 退出码协议**
   - `--task "x"`（1 字，低于 4 字下限）⇒ 明确报错"需求描述长度需在 4-8000 字之间"，**RC=2**
     —— 与文档退出码协议（2=参数或配置错误）一致。⇒ 校验在开跑前拦住，且退出码可用于 CI 判定。

### 结论
- 三个补验面全通过；**退出码协议（0/1/2/3）在真实执行路径上被逐一观察到**：
  0=达标（假后端 passed）、1=未达标已交付（真机 4.50 与续四 8.24）、2=参数错误（1 字任务）、
  以及 3=运行失败（续三 optimizer 超时那次）。
- 能力验证至此覆盖面已足够：**合同面（契约/协议/退出码/JSON）+ 机制面（整图/测试）+ 工具面（9 个子命令）
  + 端点面（preflight）全部走通**。本轮依旧零成本，未烧额度。

---

## Round 43 续九（2026-10-24）—— 最后一个大面：常驻服务异步链（submit/status/wait/report）

续八之后再验 CLI 文档强调的"长任务走 `submit` 别占终端"这条能力（零成本，`PM_FAKE_BACKEND=progress`，
验完即停）：

1. **起服务**：`run_server.py --port 8080` ⇒ Uvicorn 起、`/api/health` 返回
   `{"status":"ok","service":"prompt-master"}`。日志有"未设置 PM_API_TOKEN，只监听 127.0.0.1"的提示。
2. **异步提交**：`console submit --task … --max-iter 1` ⇒ **秒回** `{"run_id":"c86167ed34c0", …}`（RC=0），
   不占终端。
3. **查状态**：`console status c86167ed34c0` ⇒ 全量 JSON：`aggregate`（avg/min/max/n_passed/ci_lower/
   jitter_provenance…）、`prompt_version_list`、`evaluations`（逐用例五维）、`baseline_aggregate`、
   `pairwise`、`trace`（逐节点事件）。RC=0。
4. **阻塞到终态**：`console wait c86167ed34c0` ⇒ 终态 JSON **并直接带回完整渲染报告**。RC=0。
5. **取报告落盘**：`console report c86167ed34c0 --out logs/_async_report.md` ⇒ 返
   `{"report_path": …}`，文件正确落盘。RC=0。
6. **收尾**：验完**立即停服务**，`Test-NetConnection 127.0.0.1:8080` = False（端口已释放）。

### 本轮顺带确认的"诚实护栏"
- 假后端跑出的报告**首部自带横幅**："⚠️ 运行通道：**假后端（PM_FAKE_BACKEND）**——分数、Δ 与成本是链路
  形状的演示输出，**不是真实评测读数**，勿当验收或对比依据引用"。⇒ 演示模式不会被误读成真实结论。
- 同一份报告里还如实写了：基线对照（6.33→9.0，Δ+2.67）、**注入存活**（1 条注入用例、0 条被劫持，
  基线侧 0/1）、版本表（v0 7.33 → v1 9.0）、逐条对照（修好 2 条 / 弄坏 0 条）、评委校准披露
  （11 条人工锚点、聚合偏差 +0.29、95% CI −0.60~+1.09）。

### 结论
- **异步长任务链（起服务→提交→查状态→阻塞等待→取报告→停服务）完整走通**，四项查询返回结构一致、
  退出码均 0。这是此前唯一没验过的大面。
- 至此项目能力验证覆盖面：**合同面 + 机制面 + 工具面（9 子命令）+ 服务面（异步链）+ 端点面**全部走通；
  **效果面**仍是续五/续六的诚实判定（任务相关，暂无新 passed record）。全程未烧额度（本轮全假后端）。

## Round 44（2026-10-26，PromptMaster 本产品）—— 分发入口（PE-1）：技能包 + 插件清单 + GitHub Action

**本轮判据**：第五轮竞品对照（`research-reports/prompt-optimization-competitors-2026-10-26/report.md`）点名的头号短板是
"**能力在、入口不在**"——本周 GitHub Trending 前 15 里 6 条是技能/插件市场条目、本周新建破千星的项目几乎全是技能，
而本仓当时 0 个 `SKILL.md`/`marketplace.json`/`plugin.json`、PyPI 404、GitHub 0★。
本轮**只补入口**：不动评分协议、不跑真端点、不产生任何"提示词变好了"的结论。

### 做了什么（按产物）

1. **技能正文** `skills/prompt-master/SKILL.md`：frontmatter（`name` == 目录名、description 带触发词）、
   三种用法表（零调用体检 / 增量门禁 / 计费闭环）、**「加载本技能不授权花钱」**的运行模式、
   退出码四态、MCP 工具表指向、诚实边界（AUC 的 CI 覆盖 0.5 ⇒ Δ 只在基线有效时才有意义）。
   这条安全声明为什么必须是判据：它是"默认零调用"的实现，丢了不会让任何测试变红 ——
   只会让 Agent 顺手跑一轮 45~102 次调用的闭环。做法上抄 Cloudflare 技能的 operating modes（同一个理由）。
2. **插件清单** `.claude-plugin/plugin.json` + `.claude-plugin/marketplace.json`：`source: "./"` ⇒
   技能正文**只有一份**（Claude Code 插件与 `npx skills add` 两条渠道共用），从根上排除"两份正文各自漂"。
   清单形状抄自两份**在用**的清单（iFixAi / Cursor 的 `.claude-plugin/*.json`，原文存 `research-reports/.../raw/`）。
   新增的插件版本号是**第三份版本拷贝**（本仓已有产品版本与网关版本两个源）⇒ 加守卫钉住 == `pm.__version__`。
3. **GitHub Action** `action.yml` + `scripts/action-prompt-gate.sh`：composite；装 Python → 装本仓 →
   逐文件跑 `diff --base-ref … --no-diff --json`；**基线里没有的新增文件跳过并明说"不计入通过"**；
   空 `paths` / 解析不了的 `base-ref` 退 2；输出 `checked`/`skipped`/`regressed`。
   逻辑放 `.sh` 而不是 yml 的多行 `run`，就是为了满足"**不许发布没跑过的接线**"这条规矩（Round 34 曾为此拒绝过 Action）——
   脚本能在本机 bash 下真跑，yml 不能。
4. **文档**：`README.md` / `README.en.md` 的入口表各加两行（技能/插件、Action）；`docs/agent-skill.md` 加一节
   "能不能装成技能"并指向 `skills/prompt-master/SKILL.md`（工具数仍由既有抄件守卫同时钉住两份）。

### 验收读数（本机实测，2026-10-26）

- 全量：`pytest tests -q` ⇒ **2155 passed / 1 skipped / 0 failed（rc=0）**；唯一那条 skip 是既有的
  "jsdom 未装"（与本轮无关）。收集总数 2141 → **2156**（+11 分发件守卫 ＋4 检修用例，见下）。
- **先红后绿的一条**：全量第一跑时 `test_every_text_mode_subprocess_pins_utf8` 当场红 ——
  本轮新写的 `_posix()` 里那个 `subprocess.run(..., text=True)` 忘了 `encoding=`。
  这正是那条闸存在的理由（中文 Windows 上会静默退化成 GBK、断言永远为假），本轮就地补上，未改判据。
- `pytest tests/test_skill_package.py` ⇒ **13 passed / 0 skipped**（含 bash 端到端六条路径）。
- bash 端到端（真 git 仓库 + 真 CLI + 真 `$GITHUB_OUTPUT`）：工作区造假回归 ⇒ **rc=1**、`regressed=1`、
  JSON 里点名 `suppressive_rule`；与基线一致 ⇒ **rc=0**；新增文件 ⇒ **rc=0 且 `skipped=1`**（并打印"不计入通过"）；
  空 `paths` / `origin/nope` ⇒ **rc=2** 且给人话。取证脚本与日志存 `research-reports/.../raw/smoke-action-gate.{sh,log}`。
- 抄件守卫：`test_packaging` / `test_check_command` / `test_mcp_server` / `test_ci_gate_matrix` /
  `test_precommit_gate_set` / `test_skill_package` 六文件 **97 passed**（用例数抄件 4 处 **2141 → 2156** 已同步；
  SKILL.md 已进 MCP 工具数抄件表）。
- 静态：`ruff check` 与 `ruff format --check`（CI 同路径：`pm/ tests/ extras/ tests_extras/ run.py run_server.py examples/ eval_prompts.py`）、
  `mypy pm/ extras/ run.py run_server.py eval_prompts.py` 全绿。
- PyPI 路径：`scripts/release_pypi.py --check` ⇒ **RC=0、产物校验通过**（wheel 522,956 B / sdist 1,052,788 B，sha256 已记）。

### 未做 / 未验（明说）

- **PyPI 未上传**：`--upload` 要 owner 机器上的 `PYPI_TOKEN` 与 `v2.3.0` tag（版本不可撤回，不该由我代按）。
- **Action 未在真 GitHub Runner 上跑过**：本机 bash（Git for Windows 5.3）端到端已跑通，runner 侧未验 ⇒
  不把"CI 里能用"写成已验证。
- **技能市场未真正发布**：清单已就位，"被市场索引/被用户装上"是外部动作，本轮不可自证。
- 顺带记一条与本轮无关、但**会到期**的账：`python -m build` 警告 `project.license` 用 TOML 表写法
  **在 2027-02-18 后不再被 setuptools 支持**（本轮未改，留痕给发版的人）。

### 检修（同日复审：按"哪条守卫可以被绕过 / 哪句话是错的"去查，不重跑已绿的用例）

找到 **5 处真缺陷**，全部已修，并各补了钉住它的用例（+4 条，其中一条是"改了报错口径"）：

1. **守卫自己的洞（最该记的一条）**：`test_action_only_calls_zero_call_subcommands` 只扫
   `action.yml` 的 `run:`，而那里只有一行 `bash …/action-prompt-gate.sh` —— 真正决定跑什么的是脚本正文。
   ⇒ 往 `action-prompt-gate.sh` 里加一句 `$PM_CLI --task …`，**没有任何用例会红**，"零调用"当时只是一句注释。
   修：两处都扫；并用正则取出脚本**实际调用**的子命令集合，要求它 ⊆ 零调用白名单（`check/diff/gate/doctor/history/calibrate/library`）。
2. **基线可能"比对自己"⇒ 假绿**：执行体的自动探测候选表是 `origin/HEAD → origin/main → origin/master → HEAD~1`，
   **没有"跳过等于 HEAD 的候选"这条规矩**。而 `pm/promptgate.py::resolve_base` 在 **2026-10-02** 记过同款真实缺陷
   （单提交仓库里 `master` 能解析且就是 HEAD ⇒ diff 恒为"没有改动" ⇒ 门禁静默通过，"长得像一次成功的检查"）。
   修：候选表与规矩都对齐 `gate`（`origin/main → origin/master → main → master → HEAD~1`，跳过 ==HEAD，
   显式给 HEAD 仍放行）；另按 `gate` 的 rc=3 口径补 **全零 SHA = 新分支首次推送**（放行 + 横幅 + 明写"未做判定"，
   且那批文件计 `skipped` 不计 `checked`）。
   ⚠️ **我第一版写的那条"自动探测"用例，正是拿"HEAD == origin/main"当成功路径** —— 它会把这个假绿锁死
   （用例能锁死缺陷，不只是发现缺陷）。已改成"HEAD 严格晚于 origin/main"，并把"候选全等于 HEAD 必须退 2"单独立一条。
3. **文档把人指向错命令**：`gate` 的对象是 Python 模块里的「全大写 = 字符串常量」模板（默认 `pm/prompts.py`），
   而 README 入口表写着 `diff --base-ref …`（或 `run.py gate`）—— 实测 `run.py gate --path prompts/p.md` ⇒
   **rc=2「源码无法解析（invalid character '。'）」**，再被 gatecmd 前缀一句"这是门禁自己坏了"，
   用户读到的结论是"工具坏了"。修：README 中/英入口表去掉那个"或"、能力表给 `gate` 标注对象与出路；
   SKILL.md 同步；并把 `extract_templates` 的报错改成**带出路**（"markdown 请改用 `diff --base-ref`"）+ 1 条用例。
4. **守卫的假阳性**：禁用词扫描把执行体头部注释里"不做的事（有意）：不跑计费闭环（没有 `--task` / `submit`）"
   读成了违规。修：只扫**会执行的行**（去掉整行注释）—— 注释恰恰是这套判断的说明书，不该删。
5. **小项两处**：① 克隆出来的仓库不带提交身份 ⇒ 用例 `git commit` rc=128 假红（补 `_configure_identity`）；
   ② 三条安装命令补 `--skill prompt-master`（对齐 Cloudflare 那份在用技能的写法）。
   另把"插件清单版本拷贝"的断言从技能模块**迁进** `tests/test_version_single_source.py`（版本纪律只该有一个家），
   并在 `test_no_new_hardcoded_version_literals` 的 docstring 里写明扫描范围为何不含那份 JSON。

**检修后的复验**：全量 **2155 passed / 1 skipped / 0 failed**；`ruff` / `format --check` / `mypy --strict` 全绿；
独立冒烟（不经 pytest）五条路径复跑：回归 rc=1、干净 rc=0、新增文件 rc=0、空 paths rc=2、坏 ref rc=2 —— **与修前一致**（没把修好的东西改坏）。

### 落库与 CI（2026-10-26 当日）

- 落库 `f861206`（51 files / +18,642），已推 `origin/main`。原始抓取产物按量表分流：
  `data/`（10.2 MB 的 API 原样 JSON）与周榜 RSS（1.2 MB）**不入库**，`.gitignore` 写在报告目录里并说明
  "可复现 + 已落成可引用形式"；留下的 `raw/` 是小而不可省的证据（清单形状原文、独立冒烟脚本与日志）。
- **CI 绿**：run `40357353444` ⇒ `conclusion=success`，四个 job 全过 ——
  `test (3.11)` / `test (3.12)` / `test (3.13)` / `windows`（[run](https://github.com/fengxingxuerong/prompt-master/actions/runs/40357353444)）。
- 这条把下面那条"未验"**升级**了一格（但仍不等于全验）：13 条守卫（含 bash 端到端）是**在真 GitHub Runner 上跑的**，
  且 **windows job 也过了** ⇒ "执行体只在本机 bash 验过"这句话过期了；仍**未验**的是
  **消费者的 workflow 里 `uses: fengxingxuerong/prompt-master@main`** 这条路径（CI 没有消费这个 action 本身）。
- 顺手验了"外部真能装到"：推完后从 `raw.githubusercontent.com/<repo>/HEAD/...` 取回五个分发件
  —— `skills/prompt-master/SKILL.md` 5,781 B（frontmatter 正常）、`.claude-plugin/marketplace.json` 906 B、
  `.claude-plugin/plugin.json` 739 B、`action.yml` 1,895 B、`scripts/action-prompt-gate.sh` 5,253 B。
  README 让人执行的那两条安装命令背后**确实有文件**（这一步只证"取得到"，不证"市场已索引"）。

### 诚实边界

本轮不改评分协议、不跑真端点，因此**不产生任何效果结论**；13 条守卫钉的是**分发件的形状**
（frontmatter 合法 / 市场条目闭合 / 版本同源 / Action 只调零调用子命令且输出对得上），不是产品能力。
"装了技能就能用好"这句话本轮**没有证据**，只有"装得上、形状对、门禁拦得住真回归"这三件。

## Round 45（2026-10-26，PromptMaster 本产品）—— 三态裁决（PE-2）：`ok` 不再把「没比成」说成「没问题」

**本轮判据**：第五轮竞品对照的 PE-2。对照物是 cloudflare/security-audit-skill 的做法 ——
它把"未决"做成**被 schema 和校验脚本卡住的结构字段**（`needs_validation` 必须带
`an exact unresolved fact and no severity`），而本仓那时只把"不可采信/不可估"写在报告散文里。

### 先取证：一条静默错误（不是推测）

`gate` 在**没有基线**（新分支首次推送，`github.event.before` 是全零 SHA）时的 JSON：

```json
{"skipped": true, "skip_reason": "…客观上没有可对比的版本…", "regressed": false, "ok": true}
```

**一个模板都没比，却报 `ok: true`。** 消费方（Agent / CI 面板 / 别人的脚本）只读 `ok`，
就会把"没检查"读成"检查通过"。它还是**静默**的：退出码 0、日志无异常 ——
与本产品"如实说清哪里还不行"这条线直接相反。

同类第二处：`check --cases-file` 对一张**一张否决票都没有**的考卷给 `ok: true`。
那种考卷挡不住差输出（`render_cases` 早把这句话印给人看了），JSON 出口却把它吞了。

### 做了什么

新增 `pm/verdict.py`：

- 三态 `confirmed` / `needs_validation` / `rejected`，**顺序敏感**（真发现问题优先于"没核验成"：
  「改了 3 个模板引入问题、另 2 个是新增没法比」报 `rejected` 才对）；
- `unvalidated` 条目**只许** `code` / `fact`，多一个键（尤其 severity）直接判不自洽 ——
  给了程度就会被人按"轻微"放行，而这套裁决的全部价值恰恰是不许放行；
- `attach()` 让 `ok` **严格等于** `confirmed`（它**覆盖**调用方原先写的 `ok`，那正是它存在的理由）；
- `validate()` 可被消费方直接调用；另有 `python -m pm.verdict <payload.json|->` 自校验入口。

接入三处：`gate`（含 `no_baseline` / `no_base_file` / `added_templates` / `removed_templates` /
`gate_error` 五种未决事实）、`check`（含 `--cases-file` 的 `no_strong_veto` / `no_veto_capable`）、
`diff`（同形带 `unvalidated: []`，理由写在代码里：空文件在上游就退 2，这条分支**核对过是空的**，
保留键只为让三个命令 JSON 同形）。

**退出码一个都没改**：首次推送仍放行（`--require-baseline` 才退 2）。
`ok` 与退出码**故意不同步** —— 构建红不红，和"这次结论成不成立"不是同一个问题；
让"没基线"也退 1 会把新分支永久弄红（本仓对"永远红的门禁"有专门教训：
它会被绕过，最后变成"本地跑过、CI 也绿"的错觉）。

### 验收读数

- 全量 `pytest tests -q` ⇒ **2177 passed / 1 skipped / 0 failed**（收集 2156 → **2178**）。
- `tests/test_verdict.py` **22 条**：纯逻辑（顺序、只许两键、ok 必须等于 confirmed、两种互斥）+
  三命令各路径 + `-m pm.verdict` 的 UTF-8 管道与用法错。
- `ruff check` / `ruff format --check`（CI 同路径）与 `mypy --strict`（102 files）全绿。

### 本轮踩到并修掉的三处（都是我自己写坏的，记下来）

1. **复杂度棘轮当场红**：`validate` CC=12 > 硬上限 10。按仓规**拆函数**（`_check_fact_item` +
   `_check_verdict_facts_consistency`）而不是登记豁免；顺手换来"报错能精确到第几条哪一项"。
2. **校验器自己在 GBK 下崩**：`python -m pm.verdict` 印 `✅/❌` 直接 `UnicodeEncodeError` ——
   一个校验器在报错时崩掉，比不校验更糟。本仓早有这条门禁（`tests/test_console_gbk_entry.py`
   要求交付面的 `__main__` 入口统一走 `ensure_utf8_stdio`），我第一版没照做；现已走 helper，
   并把符号换成 `[通过]/[不通过]`。
3. **两处夹具测错了东西**（不是产品问题，但会锁死错误结论）：① 用中文 scenario 造考卷，
   撞上 `scenario_gap` 得到 `rejected`，测的其实是"考卷有结构问题"；改用标准名 `main_path`/`boundary`。
   ② 往 `pm/prompts.py` **追加**一个模板来造回归，但新增模板没有基线可比、`gate` 正确地**不判回归**；
   改成覆盖同名模板正文。两条都在用例 docstring 里写清楚了，免得下一个人再踩。

另有一处**自查删掉的死代码**：原本给 `diff` 写了 `empty_left`/`empty_right` 两个未决事实，
实测空文件在上游 `resolve_source` 就退 2（"是空文件，没有可对比的正文"），走不到裁决层 ⇒
按仓规不留假想分支，删掉并在原地写明"核对过是空的"。

### 诚实边界

本轮**不改评分协议、不跑真端点**，不产生任何效果结论；22 条守卫钉的是 **JSON 契约的自洽性**，
不是"提示词变好了"。`needs_validation` 让"没查"不再冒充"查过了"，但它**不会**让缺的检查真的被做掉 ——
首次推送仍然没有基线，考卷仍然可能一张否决票都没有。它只是让机器读得出来。
