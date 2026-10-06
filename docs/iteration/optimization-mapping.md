# 优点 → 优化项映射（含优先级与采纳结论）

> 来源：competitor-analysis.md §2。P0=本轮必做（直接影响评分短板）、P1=本轮做、P2=记录不采纳/延后。

| 优化项 | 对应优点 | 优先级 | 实施方式 | 状态 |
|---|---|---|---|---|
| OPT-1 流式 SSE 转发 | A1 流式 | P0 | 网关 stream=true 时向上游流式转发（SSE 逐 chunk 透传，兼容 OpenAI SDK/LangChain），失败自动切换照常生效（切换前无正文输出即无感） | 待实施 |
| OPT-2 虚拟密钥（agents 维度） | A2 虚拟密钥 | P0 | /v1/keys 签发 vk-xxx（落盘 JSON），chat 按 Authorization 归属 agent 并校验启用态；PMH_GATEWAY_TOKEN 仍为管理员口令 | 待实施 |
| OPT-3 用量统计端点 | A3 用量归属 | P0 | /v1/usage 按 agent/model/key/day 聚合台账（calls/tokens 建议值/失败/切换次数） | 待实施 |
| OPT-4 轻量 Web 控制台 | A4 管理后台 | P0 | /console 单页（Fathom 严谨风）：池状态、台账检索、虚拟密钥管理、对话测试窗；只读 + 密钥操作 | 待实施 |
| OPT-5 智能体注册持久化 | A6 落库 | P0 | 注册表落盘 data/agents.json（mtime 热载），修 K-4 | 待实施 |
| OPT-6 /v1/metrics 指标端点 | A7 指标 | P1 | JSON 指标（calls/success_rate/switches/by_model），外部探针友好 | 待实施 |
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
| PB-3 MCP 暴露差分能力 | 竞品普遍把 diff 做成 API/工具 | P1 | `pm/mcp_server.py` 新增 `prompt_diff` 工具（临时文件中转，与 CLI 单一口径） | ✅ 已实施（12 个工具） |
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
| PC-7 考卷身份做成内容哈希 | openai/evals 声明式记录 / Langfuse 数据集按时间戳定版本 | P1 | 现状用 n + 协议名推断"是不是同一张考卷"；n 相同不代表集合相同（两批各 45 条可以完全不同） | ⏳ 未做 |
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
