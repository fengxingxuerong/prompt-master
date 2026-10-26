# 优点 → 优化项映射（含优先级与采纳结论）

> 来源：competitor-analysis.md §2。P0=本轮必做（直接影响评分短板）、P1=本轮做、P2=记录不采纳/延后。

| 优化项 | 对应优点 | 优先级 | 实施方式 | 状态 |
|---|---|---|---|---|
| OPT-1 流式 SSE 转发 | A1 流式 | P0 | 网关 stream=true 时向上游流式转发（SSE 逐 chunk 透传，兼容 OpenAI SDK/LangChain），失败自动切换照常生效（切换前无正文输出即无感） | ✅ 已实施（`pm/modelhub/streaming.py`，`server.py` 的 `_stream_response`；能力性拒绝 401/404 会降级为非流式） |
| OPT-2 虚拟密钥（agents 维度） | A2 虚拟密钥 | P0 | /v1/keys 签发 vk-xxx（落盘 JSON），chat 按 Authorization 归属 agent 并校验启用态；PMH_GATEWAY_TOKEN 仍为管理员口令 | ✅ 已实施（`pm/modelhub/vkeys.py`，路由 `server.py` 的 `/v1/keys`、`/v1/keys/{key}`） |
| OPT-3 用量统计端点 | A3 用量归属 | P0 | /v1/usage 按 agent/model/key/day 聚合台账（calls/tokens 建议值/失败/切换次数） | ✅ 已实施（`server.py` 的 `/v1/usage`；日聚合落 `data/usage_daily.json`，2026-10-15 修掉跨进程写坏） |
| OPT-4 轻量 Web 控制台 | A4 管理后台 | P0 | /console 单页（Fathom 严谨风）：池状态、台账检索、虚拟密钥管理、对话测试窗；只读 + 密钥操作 | ✅ 已实施（`pm/modelhub/console.py`，路由 `server.py` 的 `/console`） |
| OPT-5 智能体注册持久化 | A6 落库 | P0 | 注册表落盘 data/agents.json（mtime 热载），修 K-4 | ✅ 已实施（`vkeys.py` 的 `_agents_path()` / `AgentRegistry`） |
| OPT-6 /v1/metrics 指标端点 | A7 指标 | P1 | JSON 指标（calls/success_rate/switches/by_model），外部探针友好 | ✅ 已实施（`server.py` 的 `/v1/metrics`） |
| OPT-7 健康感知加权 | A5 | P2 | 已有熔断冷却 + priority 主备链足够单机场景；加权路由收益边际小 → 记录不采纳理由（避免调度不确定性） | 不采纳 |
| OPT-8 语义缓存 | Portkey caching | P2 | 正确性风险 → 不采纳（competitor-analysis §3） | 不采纳 |
| OPT-9 多租户/用户体系 | One API 用户 | P2 | 单运维者场景 → 不采纳 | 不采纳 |

## 版本规划

- v1.1.0 = OPT-1 ~ OPT-6（+既有 v1.0.0 全部能力）
- 评分目标：评分卡六维度总分 10/10（每维 ≥9）

---

## 第二轮（2026-10-02）：PromptMaster 本产品对标

> 第一轮（上表）对标的是 ModelHub 网关。本轮对象换成本产品（提示词优化闭环），
> 竞品调研见 `competitor-analysis-prompt.md`。P0=本轮必做，P2=记录不采纳。

| 优化项 | 对应竞品做法 | 优先级 | 实施方式 | 状态 |
|---|---|---|---|---|
| PB-1 提示词差分（`diff` 子命令） | promptfoo 的 CI 回归门禁 / PromptLayer 的 git 式版本 diff / Braintrust 的实验对比 | P0 | 零调用差分三层：规则层（修好/新引入）、结构层（约束与条目增删）、文本层（逐行 unified）。退出码 1 = 有新引入的规则问题，可直接当 CI 门禁；判别只认"新引入"——修好 0 条不判失败 | ✅ 已实施（21 条用例） |
| PB-2 金额折算与成本外推 | LangSmith / Helicone / FutureAGI 把成本与质量并列 | P0 | `pm/cost.py`：按角色折算（支持按角色覆盖单价）；报告新增「成本折算」段；`--dry-run` 按最近一次运行的实测均价外推金额区间。**不内置价目表** | ✅ 已实施（18+2 条用例） |
| PB-3 MCP 暴露差分能力 | 竞品普遍把 diff 做成 API/工具 | P1 | `pm/mcp_server.py` 新增 `prompt_diff` 工具（临时文件中转，与 CLI 单一口径） | ✅ 已实施（13 个工具） |
| **PB-1b 把差分接成 CI 门禁** | promptfoo 的"提示词进版本库、每次提交跑断言"定位 | P0 | `run.py gate`（`pm/promptgate.py` + `pm/cli/gatecmd.py`）：自动从 git 取基线、静态 AST 抽取 `pm/prompts.py` 的模板、**只拦本次新引入**的规则失败模式。CI 两个 job 都跑，checkout 改 `fetch-depth: 0`。判据必须增量——17 个模板有 16 个天然命中规则，绝对判据会永久假红 | ✅ 已实施（37 条用例） |
| **PB-1c 结构契约进 CI** | promptfoo / DeepEval 的"提示词进 CI、每次提交跑断言" | P0 | `.github/workflows/ci.yml` 两个 job 各加一步 `python eval_prompts.py`（零调用）。与 gate 互补：gate 判增量（有没有变坏），这一步判绝对（模板是否残破）。修掉"子串判据被只删一半标签绕过"的真实漏洞 | ✅ 已实施（22 条用例） |
| PB-4 自动最优提示词搜索 | DSPy MIPROv2 / GEPA | P2 | **不采纳**：论文实测 49% 的优化运行低于 zero-shot；GEPA 在 holdout 上过拟合、跨模型迁移未验证 | ❌ 记录不采纳 |
| PB-5 云端提示词版本库 | PromptLayer / PromptHub | P2 | **不采纳**：单机交付物是文件；云端库与本产品"零出网也能用"承诺冲突 | ❌ 记录不采纳 |
| PB-6 图形化看板 | LangSmith / Helicone | P2 | **不采纳**：报告即界面（Markdown 可版本化、可 diff、可进 CI） | ❌ 记录不采纳 |

### 本轮的诚实边界

PB-1 与 PB-2 都**不改变评分与优化效果**，只补"可核对性"（改了什么）与"成本可见性"（花了多少）。
所以本轮不含任何"提示词变好了"的结论——那仍然必须配真实 Key 跑 e2e。
两者都刻意做成零调用：一旦调 LLM，版本对比就无法每次提交都跑，成本读数也退化成事后计算。

---

## 第三轮（2026-10-06）：对标"量具"而不是对标功能

> 竞品调研见 `competitor-analysis-prompt.md` 第三轮。判据与实测读数见 `docs/evaluation.md` §三十一。
> P0=本轮落地，P1=记录待做，P2/N=不采纳并写理由。

| 优化项 | 对应竞品做法 | 优先级 | 实施方式 | 状态 |
|---|---|---|---|---|
| PC-1 评委分判别力审计 | Phoenix「judge 模板须过 F1 出厂闸」/ Langfuse κ+混淆矩阵 | P0 | `pm/calibration.py`：`_auc_vs_label`、`discrimination_stats`、`render_discrimination`；`analyze()` 与 `aggregate_history()` **两处**接线（README 抄的是聚合那行） | ✅ 已实施 |
| PC-2 阈值净增益必须赢过平凡基线 | DeepEval 早期的 `find_threshold`+混淆矩阵（现已删除） | P0 | `_best_cut`（只在评委分可表达区间内扫）+ `_loo_cut_accuracy`（留一折重定阈值）⇒ 报告直接给"净增益 +0.000（留一折 0.933 = 平凡基线 0.933）"这种可以为零/为负的数 | ✅ 已实施 |
| PC-3 κ/一致率的可估性门槛 | 同族：power 分析、显著性检验（**调研对象里无人做**） | P0 | 2×2 四格各 ≥10（κ 标准误反解 + 蒙特卡洛核对）且重抽 CI 半宽 ≤0.3，否则报告改印「这张考卷答不了达标一致性」 | ✅ 已实施 |
| PC-4 补采队列按人工标签格子排 | Langfuse Annotation Queue 的设计核心 | P0 | `harvest_anchors.py`：`--coverage`（只读、零写入）+ `--prefer-deficient`（最缺格 × 历史命中率排序，**只认 owner 缺口**） | ✅ 已实施 |
| PC-5 留出集与缓存的 split 维度 | GEPA 缓存键 =(候选哈希,样例 id,**split**)，train/val 命名空间隔离 | P1 | 本产品 target/eval 缓存按内容哈希、**无 split 维**；做留出集时必须先给缓存键加 split，否则留出侧被训练侧热缓存重放 | ⏳ 未做（写死在文档，防顺手踩） |
| PC-6 廉价优先级联（`inspect cascade()` 同形） | inspect_ai `cascade()` | ~~P1~~ **❌ 实测否决** | 判据预写后当场量：10 份真端点归档里"评委真调用落在确定性层已判死用例"占 8/40=20%，但**剔除后聚合均分只动 0.03**（4.53→4.56）⇒ 没有可修的信噪污染，只剩一两成调用节省；代价是主动按内容决定"这条不用打分"，而 `min_score`/噪声带估计依赖每条都有分（§三十·一 刚为"崩臂被好意过滤删掉"开过刀）。理由与读数见 `docs/evaluation.md` §三十一·九(1) |
| PC-7 考卷身份做成内容哈希 | openai/evals 声明式记录 / Langfuse 数据集按时间戳定版本 | P1 | 现状用 n + 协议名推断"是不是同一张考卷"；n 相同不代表集合相同（两批各 45 条可以完全不同） | ✅ 已实施（`pm/calibration.py` 的 `_paper_identity()` 按锚点集内容哈希出指纹，`pm/cli/calibrate.py` 的 `_anchors_stamp()` 把人工分也并进指纹；⚠️ 早前标「未做」是错的——`docs/evaluation.md` 已就地更正：当时只查了 `aggregate_history` 一处就下了结论） |
| N13 自动最优提示词搜索 / GEPA 式反思 | DSPy MIPROv2 / GEPA | 不采纳 | 理由**替换**：上一轮引的二手负结果未能复核到一手（见 §5 引用降级），本轮改用自家实测——评委在 owner 标签上 AUC 0.452（owner 子集）~0.623（全集，CI 覆盖 0.5），以这种量具为目标的搜索优化的是它没读出来的那一维 | ❌ 维持不采纳 |
| N9 G-Eval logprob 加权 / `rubric=` 区间约束 | DeepEval | 不采纳（暂缓） | 需端点回传 logprobs，经 modelhub 网关**未验证任何上游提供**；先验端点再谈 | ❌ 待前置验证 |
| N12 合成用例 + critic 复筛 | DeepEval synthesizer | 不采纳（暂缓） | critic 仍会是同一个评委，等于用没读出信息的量具筛题 | ❌ 阻塞在 PC-1 之后 |

### 本轮的诚实边界

PC-1~PC-4 全部**零调用、零出网、不改评分协议**，因此本轮不产生任何"提示词变好了"的结论，
也不改动 §十七～§三十 的任何 Δ 读数——它改的是"这些读数有没有资格被报出来"。
本轮唯一新增的人工依赖：**所有者亲判的"达标"样本**（门槛每格 10 条，现在 owner 达标格 10 条、≥9 格 1 条）。
补料池当场核过（§三十一·十）：按输出内容去重后评委 ≥8 的素材只剩 **2 条**没标过，另有 **3 条**
AI 代判落在校验 8.0-8.9 段可请所有者改判 ⇒ **零算力共 5 条**；其余必须再跑真端点批次。
在达标格补到 10 条以上之前，`calibrate` 与 `--aggregate` 的达标一致性读数一律标"不可估"。

---

## 第四轮（2026-10-13）：同类项目坐标刷新与三条可抄项

> 竞品调研见 `research-reports/prompt-optimization-competitors-2026-10-13/report.md`
> （GitHub API 当日实测 17 个仓库 + 2 组关键词搜索；Δ 基线 = 9-24 报告）。
> P1=记录待做，P2=暂缓/前置未满足，❌=不采纳并写理由，🔭=只监控。

| 优化项 | 对应竞品做法 | 优先级 | 实施方式 | 状态 |
|---|---|---|---|---|
| PD-1 注入用例库扩容 | promptfoo 红队插件（50+ 注入/PII/jailbreak 场景库） | P1 | 本产品注入存活靠自研判据（校验码口径），**考卷广度**是短板；把 promptfoo 的插件清单映射成 MockGen 注入用例。前置：先盘点现有注入用例数，算出缺口再抄，勿整库搬运 | ✅ **已实施（2026-10-13）**：盘点结论是"缺口不在用例条数、在**装束种类**"——伪造指令此前只有 1 种写法（【系统通知】权限已提升），每次运行测同一招。落地 `pm/injection_pack.py`：10 种装束（伪造系统通知/作废式更正/伪造工具返回/角色重指派/伪造审计工单/定界符逃逸/HTML 注释/英文系统更新/少样本示范/零宽混淆），按 (run_id, 序号) 确定性轮换 + details 带技法名；适用性裁剪（只收单轮文本注入族，PII/内容审核族不收）与三条不变量写明在模块 docstring | ✅ 已实施（+3 条守卫，变异验证 2 组） |
| PD-2 轨迹式 revise（读失败轨迹而不只读分数） | GEPA（LLM 反射读全轨迹：错误消息/日志/推理，而不是坍缩成标量） | P2 待验证 | revise 节点已吃评委 issues 与分差——先核对"失败轨迹"是否已等价覆盖，再决定要不要把执行侧错误原文（trace 的 *_empty / 失败调用）也喂进去。**先核对现状，别为了对标而加料** | ✅ **核对完成（2026-10-13）：已等价覆盖，不新增**。逐项清点 `_build_feedback` 现喂给修订器的内容：① `_hard_failures`——事实断言失败（case#+mode+detail+期望片段）与注入得手 details（PD-1 起还带技法名）；② fixed/broken 逐条得失账（净赔先止盈）；③ 各维度均分（由低到高）；④ 最该反思用例（最低分/采样不稳）；⑤ `_contrastive_slice`——**通过/失败两条用例的输入与输出原文并排**（各 160 字符），这就是"读轨迹"在本产品形态下的等价物。刻意**不喂**的东西：目标调用基础设施错误（不可归因于提示词，喂了会诱导为基础设施问题改提示词——与废样本/崩臂口径一致）。 | ✅ 核对完成（等价覆盖，不加料） |
| PD-3 标注队列 | Langfuse Annotation Queue / Phoenix golden set | — | **等价物已在**：`scripts/build_owner_rejudge_queue.py`（PC-4 同轮落地）就是同一形态（按缺口出表、`human_score` 留空、回填可审计、非法行整次作废） | ✅ 无新增 |
| PD-4 部署矩阵（Vercel/CF/桌面/插件五端） | linshenkx（Web+Chrome 插件+桌面+Docker） | ❌ 不采纳 | Python 服务（FastAPI+CLI+MCP），Vercel/CF Pages 是 JS 生态不适用；Dockerfile 已有；桌面形态无需求 | ❌ |
| PD-5 监控条目更新 | GEPA 6,955★/MIT/活跃、`dspy.GEPA` 已集成、README 称 50+ 生产用例；linshenkx 三周 +2,173★**且补了 AGPL-3.0 许可证 + 评估层** | 🔭 监控 | 9-24 报告点名的两个风险（无许可证、无独立评测层）**三周内全部落地** ⇒ 下一观察点 = 它的评估层是否做深（量化/断言/门禁）。GEPA 若补上"无 ground truth 场景"的解法，会正面冲击本项目空位 | 🔭 持续 |

### 本轮的诚实边界

本轮调研**不含任何代码改动**（PD-3 是确认等价物已存在，不是新实施），也不产生任何效果结论。
唯一状态变更是 PD-5：两条外部风险（linshenkx 无证照/无评估层）在 2026-09-24 → 10-13 之间**已被外部解决**，
本表的旧判断已就地更新，避免下一个人照旧结论做决策。

---

## 第五轮（2026-10-26）：本周热门项目坐标刷新 —— 空位还在，入口不在

> 竞品调研见 `research-reports/prompt-optimization-competitors-2026-10-26/report.md`
> （GitHub API 当日实测：14 家基线坐标 + 7 组关键词/topic 搜索 + `created:` 新建仓 + Trending 周榜 + 11 份竞品 README 原文；
> Δ 基线 = 第四轮的 10-13 读数）。P1=记录待做，P2=暂缓/前置未满足，❌=不采纳并写理由，🔭=只监控。

| 优化项 | 对应竞品做法 | 优先级 | 实施方式 | 状态 |
|---|---|---|---|---|
| **PE-1 把 CLI 包成可安装的技能/插件 + 发 PyPI + 补 Action** | iFixAi（plugin + skill 双分发，34.5k★）、prompt-architect（Claude Code 插件/npm/zip）、cloudflare/security-audit-skill（`npx skills add`）、本周 Trending 前 15 里 6 条是技能/插件市场 | **P1（当前第一短板）** | 仓内放 `skills/prompt-master/SKILL.md`（触发词=体检提示词/PromptMaster/门禁）+ marketplace 清单，把 `check`/`diff`/`gate` 这三条**零调用**能力当第一张脸；发 PyPI 用**已存在**的 `scripts/release_pypi.py`；Action 走 `uses: <repo>@ref`。**与 PD-4 的否决不冲突**：PD-4 否的是 Vercel/CF 这类 JS 云部署，技能包与 Python 无关 | ✅ **本地侧已实施（2026-10-26，Round 44）**：技能正文 `skills/prompt-master/SKILL.md`（含"默认只读、计费要点头"的运行模式，抄 Cloudflare 技能的 operating modes 那条理由）、插件清单 `.claude-plugin/{plugin,marketplace}.json`（`source: "./"`，**一份正文喂两个渠道**，不复制）、Action `action.yml` + `scripts/action-prompt-gate.sh`（composite；只调 `diff --base-ref`，计费入口一律不进 CI）。守卫 13 条 `tests/test_skill_package.py`（含"自动探测基线""**候选全等于 HEAD 必须退 2**""全零 SHA = 首次推送"三条），并把 SKILL.md 纳入既有的 MCP 工具数抄件表（全量 2155 passed / 1 skipped / 0 failed）。同轮检修另修掉：守卫只扫 `action.yml` 不扫脚本的洞、`gate` 被 README 写成 `diff` 等价物的错指向（实测喂 markdown 退 2）、以及禁用词扫描把"声明不做"的注释读成违规的假阳性。**PyPI**：`scripts/release_pypi.py --check` 本机 RC=0、产物校验通过（sha256 入账）——`--upload` 要 owner 机器上的 `PYPI_TOKEN` 与 `v2.3.0` tag，⏳ 未做。**Action**：本机 bash 端到端已跑，且落库后 **CI 四个 job（3.11/3.12/3.13 + windows）全绿** ⇒ 执行体在真 Runner 上也跑通了；仍 ⏳ **未验**的是"消费者 workflow 里 `uses:` 本仓 Action"这条路径（CI 并不消费这个 action 本身）。缺口实测记录（本轮开工前）：0 个 `SKILL.md`/`marketplace.json`/`plugin.json`、PyPI 404、本仓 GitHub 0★ |
| **PE-2 把"未决/不可采信"做成结构字段** | cloudflare/security-audit-skill：裁决三态 `confirmed`/`needs_validation`/`rejected` + `report-schema.json` + 零依赖校验脚本，且 `needs_validation` **必须带确切未决事实且不许给程度** | P1 | 给 `--json` 契约加裁决枚举（未决项必须带"确切未决事实"），配校验脚本。**不破坏**单对象 stdout 与退出码四态 | ⏳ 未做（现状：`不可采信/不可估` 只写在报告散文里） |
| **PE-3 补"何时停"的显式判据** | Q00/ouroboros：歧义 ≤0.2 才准写码、相似度 ≥0.95 才收敛；三级闸 机械($0)→语义→多模型共识 | P1（只抄停止判据） | 把"机械层→语义层→共识层"与"Δ 稳定到什么程度才停/什么时候升级评委"写成产品契约 + 少量断言。**明确不抄级联省钱**：PC-6 已实测否决（20% 调用、均分只动 0.03） | ⏳ 未做 |
| **PE-4 锚点补料改不确定性采样 + 随机审计切片** | sutro-sh/jev-align：挑模型不确定的样本请人标（含随机审计样本）→ 标注 → 报 certainty 变化；训练分再高也不自动采纳 | P1 | 在既有 `scripts/build_owner_rejudge_queue.py` 上加"评委间分歧/阈值邻域"取样 + 一条**固定随机切片**（防锚点集全是难题而偏斜）；回填仍走既有可审计路径。**不抄 GEPA 优化**（N13 维持不采纳） | ⏳ 未做（PC-4 的堵点仍是缺 owner 亲判锚点） |
| **PE-5 给规则层体检公开一张可靠性表** | Tencent/AI-Infra-Guard `aig-skill-scan` 公开 F1/精确率/召回/FPR 并挂 SkillTrustBench T01–T09；hol-guard `plugin-scanner` 公开信任分权重与基线、报告支持 SARIF | P2 | 在版本化语料上量 `check`/`gate` 各规则的精确率/误报率/覆盖率，随文档落档。本仓**只对评委做了这件事，对自己规则没做** | ⏳ 未做（零调用、零出网） |
| **PE-6 注入装束挂公开分类学** | AI-Infra-Guard 的 T01–T09 分类、promptfoo 插件清单 | P2 | 给 `pm/injection_pack.py` 的 10 种装束做"分类学覆盖表"，让缺口可数而不是靠断言 | ⏳ 未做（PD-1 已把装束 1→10 种） |
| **PE-7 对外口径收窄（已在旧报告就地更正）** | truera/trulens「judges are graded against human annotations, out of the box」+ 公开数字；GEPA/Nubank 公开 κ 0.00→0.745 | P1（纯文档） | 一律用"竞品在**它们的固定基准卷子**上给评委打分；本产品量在**用户自己的锚点**上，样本不足时拒绝给结论"；10-13 报告 §四 的旧措辞已就地加更正块 | ✅ 已就地更正（10-13 报告 §四 顶部） |
| 🔭 监控条目更新 | ① linshenkx 39,241★（13 天 +1,506，全赛道最快）——**评估层两周未做深**，仍是 UI 级分析/对比/评估驱动重写；② GEPA 7,148★ 仍是"you provide an evaluator"，若补上无 ground truth 解法会正面冲击空位；③ **新**：iFixAi 34.5k★（跨厂商评委 + 61 项检查 + `--min-score` 进 CI，**无校准**）、TruLens（按人工标注给评委打分）、AI-Infra-Guard（技能扫描 F1 表）；④ **新**：hol-guard 的插件信任打分是否会吃掉"技能/插件门禁"位 | 🔭 监控 | 下一观察点：这三家谁先把"评委/审计的可信度"量化到能碰 `calibrate` | 🔭 持续 |
| ❌ 维持不采纳 | DSPy 式程序编译、自动最优提示词搜索（N13）、Vercel/CF 部署矩阵（PD-4） | — | 与第四轮一致：评委判别力未收敛时做搜索 = 优化它读不出的那一维。**PE-1 不属此列**（技能包不是云部署） | ❌ |
| 📉 旧坐标的坏消息 | microsoft/PromptWizard 4,022★（**恰好停摆整一年**）、zou-group/textgrad 3,770★（停摆 ~15 个月）、meta-llama/prompt-ops **1,027★（−10，负增长）**、openai/evals 停半年 | — | 叙事：自动优化赛道冷却时，**活下来的是生态接入与人用价值**，不是单点算法 | 📉 记录 |

### 本轮的诚实边界

本轮**零代码改动、零真端点调用**，不改任何 Δ/达标读数；PE-1~PE-6 全在"入口与口径"层。
本轮唯一已落地动作是 PE-7（把旧报告里已部分过期的措辞就地更正，防下一个人照旧结论做决策）。
取证限制也照实写进报告 §六：匿名 API 限额在抓第 8 家仓库时耗尽，余下 6 家改走 `/search` 的 `repo:` 限定符；
`Arize-ai/phoenix` 的搜索调用被 403 拦下，其读数取自同日 `topic:llm-evaluation` 搜索结果；
功能对照全部是 README/文件树级，**没有跑过任何竞品的代码**，"它没有 X"这类否定判断是关键词检索的结论，不是穷举。
