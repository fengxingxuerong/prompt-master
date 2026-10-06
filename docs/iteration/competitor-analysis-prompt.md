# 竞品分析报告（PromptMaster 本产品对标）

> 调研日期：2026-10-02 · 方法：联网检索公开资料（官网/文档/GitHub/评测/论文），仅引用实际检索到的来源，未获取项标「—」
> 被分析对象：**PromptMaster 本产品**（提示词优化/评测闭环，`pm/` + `run.py`）
> 前一份同族报告 `competitor-analysis.md` 对标的是**套件里的 ModelHub 网关**，不是本产品；两者别混。

## 0. 为什么这份报告和上一份不一样

`competitor-analysis.md`（2026-09-21）对标的是 ModelHub 网关（LiteLLM / One API / Portkey），
结论是"补齐流式、虚拟密钥、用量端点、控制台"——那批 OPT-1~6 已实施并终评 60/60。

**但 PromptMaster 本产品（提示词优化闭环）从未做过竞品对标**。本次补上，
且沿用同一条纪律：只列**检索到的一手来源**，并明确写出**不采纳项及其理由**。

## 1. 竞品地形（2026-10 实测检索）

| 竞品 | 类型 | 核心机制 | 与本产品的关系 |
|---|---|---|---|
| **DSPy**（stanfordnlp，~35k★） | 声明式编译框架 | Signature + Module + Optimizer（BootstrapFewShot / MIPROv2 / GEPA），把提示词当参数编译 | 范式不同：它优化的是**程序**，本产品优化的是**一段交付给人的提示词**并给出可核对的交付报告 |
| **TextGrad**（zou-group，~3.6k★） | 文本梯度 | LLM 自然语言批评当"梯度"回传，无需数据集 | 无指标也能跑；本产品的立场相反（判定权尽量给代码与事实断言） |
| **promptfoo** | CLI/CI 评测 | YAML 定义用例，对 40+ 供应商跑断言，**进 CI 抓回归** | **最直接的对标物**：同样是"提示词进版本库、每次提交都跑" |
| **PromptLayer** | 版本管理 | git 式提示词版本 diff、审计轨迹 | 它的第一入口就是**版本对比**，本项目此前没有 |
| **PromptHub** | 协作 + 市场 | 版本化、A/B 变体、多人评审 | 团队协作面，本项目是单机 Agent 场景 |
| **Braintrust** | 评测平台 | 自定义评分函数、实验看板、生产日志 | 评测面强，优化器要外挂 |
| **LangSmith / Helicone** | 可观测 | trace + eval + **成本看板** | 把**成本**与质量并列成一等指标 |
| **FutureAGI** | 一体化 | 6 种优化器 + 50~60 评测模板 + guardrails + trace | 集成度最高，闭源为主 |
| **DeepEval** | 评测即 CI | pytest 风格提示词回归 | 与 promptfoo 同族 |

一手来源：
- DSPy 优化器总览（含 GEPA/SIMBA 分类）：https://deepwiki.com/stanfordnlp/dspy/4.1-optimization-and-teleprompting
- DSPy 的 `compile` 需要 metric + trainset：https://dreaming.press/posts/2026-06-21-dspy-vs-textgrad-vs-adalflow.html
- 工具横向对比表（谁有优化器/谁有 eval/谁有 guardrails）：https://futureagi.com/blog/top-10-prompt-optimization-tools-2025/
- promptfoo 的 CI 定位与 YAML 用例：https://www.promptquorum.com/prompt-engineering/best-prompt-optimization-tools-for-teams
- PromptLayer 的 git 式版本 diff：同上
- **负结果**：GEPA 在 holdout 上过拟合、跨模型迁移未验证：https://dev.to/jangwook_kim_e31e7291ad98/...
- **负结果（论文）**：72 次优化运行里 49% 低于 zero-shot，"提示词优化近似抛硬币"：https://arxiv.org/abs/2604.14585v1

## 2. 本产品的差异化在哪（不是"我也有这个功能"）

诚实地写清楚哪些**不打算追**，比列功能表有用：

| 竞品在做 | 本产品立场 | 理由 |
|---|---|---|
| DSPy 把提示词当程序编译 | **不追**。本产品交付的是"一段给人/给 Agent 用的提示词 + 交付报告" | 目标物不同。DSPy 的产物是程序内的 prompt 对象，本产品要在报告里如实说清"哪里还不行" |
| TextGrad 无数据集也能优化 | **不追**。已有立场：无基线/无有效样本时报告明写"不可采信" | 本产品的卖点是"诚实"，放宽证据标准会毁掉它 |
| 60+ 评测模板商店 | **不追**。5 份领域用例集 + `--cases-file` 自定义 + `custom:<name>` 断言 | 模板广度不是瓶颈，评测口径的可解释性才是 |
| trace / 生产日志看板 | **部分不追**。已有 trace/台账/report，不另建 UI | 单机 Agent 场景，报告即界面 |
| 团队协作 / 市场 / 审批流 | **不追** | 单运维者场景（与 ModelHub 那份报告同一结论） |
| 多租户与权限 | **不追** | 同上；REST 侧已有可选 token |

## 3. 学到的、且本轮落地了的两项（P0）

| # | 竞品做法 | 本产品缺 | 落地 |
|---|---|---|---|
| **B1** | promptfoo / PromptLayer / Braintrust 都把**版本对比**当第一入口 | `check` 只看单个时点，`history` 只看聚合分；**没人回答"这一版相对上一版改了什么"** | `run.py diff A B`（零调用）：规则层"修好几条/新引入几条"、结构层约束与条目增删、文本层逐行 diff。**退出码 1 = 有新引入 → 可直接当 CI 门禁** |
| **B2** | LangSmith / Helicone / FutureAGI 把**成本**与质量并列成一等指标 | 报告有 token、次数、耗时，**唯独没有金额**；读者要自己开计算器 | `pm/cost.py` + 报告「成本折算」段 + `--dry-run` 金额外推。**不内置价目表**（会过期且错得看不出来），单价由 `PM_PRICE_*_PER_M` 显式给，支持按角色覆盖 |

B1 与 B2 都刻意做成**零调用**：版本对比要能每次提交都跑，一旦调 LLM 就退化成"偶尔跑一次"；
成本读数则必须在报告生成时就算完，否则又变成"事后开计算器"。

## 4. 明确记录不落地项（避免下一个人重新论证一遍）

| # | 竞品特性 | 不采纳理由 |
|---|---|---|
| N1 | 提示词云端版本库 + 服务端 A/B 分流 | 单机交付物是文件（`logs/report_*.md` 与 SKILL.md）。云端版本库需要账号与网络，与本产品"零出网也能用"的承诺冲突 |
| N2 | 自动最优提示词搜索（MIPRO 式贝叶斯搜索） | 论文实测 49% 的优化运行低于 zero-shot，收益接近抛硬币；本产品现有的"用例 + 基线 + 断言 + 早停"已是**保守可解释**的那部分。真要接入，得先解决"优化器与评委同源 → 学会骗评委"这一失败模式 |
| N3 | GEPA 式反思优化 | 同上，且实测 holdout 过拟合、跨模型迁移未验证（见 §1 的负结果来源）。收进本产品等于把未验证的东西包装成能力 |
| N4 | 团队审批流 / 角色权限 | 无需求；引入用户系统会显著扩大攻击面（本仓库 W7/W8/W9 安全豁免尚未收回） |
| N5 | 语义缓存（对话级） | 正确性风险，与 ModelHub 那份报告的结论一致 |
| N6 | 图形化看板 | 报告即界面（Markdown 可版本化、可 diff、可进 CI），比自建 UI 更符合本产品的使用方式 |

## 5. 本轮落地清单与验证

| 项 | 文件 | 验证 |
|---|---|---|
| B1 差分能力 | `pm/promptdiff.py`（新）、`pm/cli/diffcmd.py`（新）、`pm/cli/main.py`（接线）、`pm/mcp_server.py`（`prompt_diff` 工具） | `tests/test_promptdiff.py` 21 条；subprocess 打真实入口验分发与"无 Key 可跑" |
| B2 成本折算 | `pm/cost.py`（新）、`pm/report.py`（成本段）、`pm/cli/main.py`（`--dry-run` 外推）、`.env.example` | `tests/test_cost.py` 18 条；`tests/test_report.py` +2 条 |
| 门禁同步 | README / `docs/operations.md` / `.pre-commit-config.yaml`（999 条）、MCP 工具数 12 | `test_test_count_copies_match_the_live_collection` 与 `test_packaging` 系列 |

**测得的**：全量 999 条 rc=0、覆盖率 93.07%（地板 92）、modelhub 分项 86%（地板 82）、
三套 releases 全绿、stub e2e 三场景通过、`--selftest` 通过。
**没有测的**：这两个功能都不改变评分与优化效果，所以**本文不含任何"提示词变好了"的结论**——
那仍然需要配真实 Key 跑 e2e。这一点必须在文档里写死，否则新增的 diff/成本段会被误读成效果证据。

## 6. 为什么门禁的判据必须是「增量」而不是「绝对」（本轮实测结论）

把 `check` 的绝对判据直接搬进 CI 是一个很自然的想法，实测下来不可行：

- `pm/prompts.py` 里 **17 个节点提示词模板有 16 个天然命中** `pm/quality.py` 的规则。
  它们是模板 —— 本来就含 `<<task_description>>` 这类占位符（→ `context_leak`）、
  `<输出前自检>` 这类标签（→ `delimiter_unbalanced` 的"未闭合"、`meta_leak`），
  而 OPTIMIZER/REVISER 的正文里必然出现"不要写『停止处理』"这类抑制式措辞
  （引号内的**禁止示例列举**，属规则假阳性，→ `suppressive_rule`）。
- 绝对判据会让门禁步骤从第一天起就是红的。**一个永远红的门禁等于没有门禁**：
  它会被绕过、被无视，最后变成"本地跑过、CI 也绿"的错觉
  （本仓库对"配置存在但从未真跑"的状态有过专门记录，见 `.pre-commit-config.yaml` 顶部）。
- 所以门禁只回答：**相对基线版本，这次改动有没有引入新的规则失败模式？**
  这恰好是唯一有客观判据的那一半 —— "够不够好"没有确定性判据，不该装成有。

另外记录一个由变异测试逼出来的盲区：**某规则本来就命中时，往里再加一条同类真条款，
code 集合前后完全相同**（`OPTIMIZER_SYSTEM` 正是这种模板）。
只比 code 的判据会说"无回归"，而门禁恰好对它最该管的模板失效。
现在补了命中项级通道（并剥离引号内的引用），实测能抓住。

---

## 第三轮（2026-10-06）：把对标对象换成"量具本身"

> 方法：两路并行调研公开仓库（优化器一族 / 评测与评委一族），star、许可证、最近推送
> 全部当场取自 shields.io 与 GitHub HTML + PyPI/npm 元数据，未取到的标「—」。
> **本轮不再引用二手博客的结论**（见文末"上一轮引用的复核结果"）。

### 1. 竞品地形（2026-10-06 实测检索）

| 仓库 | 语言/许可证 | ★ | 最近一次活动 | 与本产品最相关的那个机制 |
|---|---|---|---|---|
| [langfuse/langfuse](https://github.com/langfuse/langfuse) | TS / MIT（`/ee` 另计） | 35k | 今日有推送 | Annotation Queue：人工标注按队列+Score Config 收敛，配 κ/混淆矩阵分析 |
| [stanfordnlp/dspy](https://github.com/stanfordnlp/dspy) | Python / MIT | 39k | 2026-10-05（v3.4.0，9-25） | `ScoreWithFeedback` + `warn_on_score_mismatch`：metric 必须自带理由，且自检一致性 |
| [promptfoo/promptfoo](https://github.com/promptfoo/promptfoo) | TS / MIT | 26k | 今日有推送 | ~70 种断言（`assert-set(threshold)`/`select-best`/`derivedMetrics`/cost/latency）+ CI 里跑对比 |
| [confident-ai/deepeval](https://github.com/confident-ai/deepeval) | Python / Apache-2.0 | 19k | 昨日（PyPI 4.2.2） | G-Eval：整数判定按 **token 概率归一** + `rubric=` 把分数限制在指定区间；DAG 确定性判据 |
| [openai/evals](https://github.com/openai/evals) | Python / 见 README | 20k | 2026-04（≈6 个月未动） | 声明式 eval 记录（`id/metrics/class/args/samples_jsonl/eval_type`） |
| [Arize-ai/phoenix](https://github.com/Arize-ai/phoenix) | Py+TS / **Elastic-2.0**（evals 包） | 12k | 今日有推送 | 出厂纪律：judge 模板必须在 golden set 上 **F1≥85%** 才可用（docs 自述，未独立复现） |
| [gepa-ai/gepa](https://github.com/gepa-ai/gepa) | Python / MIT | 6.9k | 2026-10-01（v0.1.4，7-15） | rollout 缓存键 = **(候选哈希, 样例 id, split)** 且 train/val 命名空间隔离 → 留出集不可能被训练侧重放 |
| [codelion/optillm](https://github.com/codelion/optillm) | Python / Apache-2.0 | 4.3k | v0.4.0（9-28） | 推理期方法做成代理：best-of-n / self-consistency / majority k=6 |
| [UKGovernmentBEIS/inspect_ai](https://github.com/UKGovernmentBEIS/inspect_ai) | Python / MIT | 2.9k | 今日有推送 | `cascade()`：**确定性 scorer 先定案，评委只跑未定案的**；`multi_scorer(mode/majority)` + 各评委原始判定留存 `metadata.panel` |
| [prometheus-eval/prometheus-eval](https://github.com/prometheus-eval/prometheus-eval) | Python / — | 1.1k | **2025-04 → 约 18 个月未维护** | 开源评委模型对人工的 Pearson 0.6~0.7（README 自述） |
| [Agenta-AI/agenta](https://github.com/Agenta-AI/agenta) | Py+TS / MIT | 4.8k | 今日有推送 | （文档是 JS SPA，未取到一手内容 → 不下结论） |
| [braintrustdata/braintrust-sdk](https://github.com/braintrustdata/braintrust-sdk) | TS / Apache-2.0 | **28** | — | 仓库本身近乎无活动，不作为对标物 |
| [zou-group/textgrad](https://github.com/zou-group/textgrad) | Python / MIT | 3.8k | **2025-07-25 → 约 15 个月未动** | 梯度记忆复用 / `constraint_text` 编辑预算（不采纳，见 §4） |
| [microsoft/Trace](https://github.com/microsoft/Trace)、[google-deepmind/opro](https://github.com/google-deepmind/opro) | Python / MIT、Apache-2.0 | 762、782 | 2025-08、**2024-12** | OPRO 的可抄点只有：scorer 与 optimizer **分开配模型** + train/eval/test 三段切分 + 花钱前先冒烟 |

### 2. 本轮学到的、并且已经落地的（P0，全部零调用）

| # | 竞品做法（一手出处） | 本产品此前的缺 | 落地 |
|---|---|---|---|
| **C1** | 用标签算**判别力**而不是只算误差：Phoenix 的 F1 出厂闸、Langfuse 的 κ+混淆矩阵分析（llm-as-a-judge / pre-built-metrics 文档） | MAE / r / 一致率三个数都不回答"评委分关于达标有没有信息"；AUC 在本仓库**代码与文档里 0 命中** | `discrimination_stats()`：AUC + 重抽 CI 对 0.5 + 按 owner/ai_proxy 拆开，进 `analyze()` 与 `aggregate_history()` 两处报告 |
| **C2** | 任何阈值收益必须赢过**平凡基线**（DeepEval 曾把这做成 `find_threshold`+混淆矩阵；现行文档已删） | 报告里有"过线判定一致率"，没有"一律判不达标能得多少"，也没有留一折 | `_best_cut`（限可表达区间）+ `_loo_cut_accuracy` ⇒ 净增益可为负；实测 45 条那轮 **−0.044** |
| **C3** | **可估性/统计门槛**先于结论（同族做法：显著性、power 分析） | `MIN_PER_DECISION_SIDE=1` 把"κ 有定义"当成"κ 可读" | 四格各 ≥10（由 κ 标准误反解 + 蒙特卡洛核对）且 CI 半宽 ≤0.3，否则报告改印"这张考卷答不了" |
| **C4** | 补采队列按**人工标签格子**排，不按被校对象的输出排（Langfuse annotation queue 的设计核心） | `harvest_anchors.select()` 按**评委分带**分层——评委无判别力时与人工格子完全脱钩，实测补一轮 owner 达标格仍个位数 | `--coverage`（只读）+ `--prefer-deficient`；且**只认 owner 的缺口**，AI 代判缺格不驱动队列 |

四项都不改变评分本身，也都不产出一条"提示词变好了"的结论——它们产出的是
**下一件事该做哪个**：现在缺的是 20~30 条所有者亲判的"达标"样本，不是又一轮真端点跑批。

### 3. 本轮记录但暂不落地的（附不做的理由）

| # | 竞品特性 | 本轮判 |
|---|---|---|
| N7 | `inspect cascade()`：确定性断言先定案、评委只跑未定案的 | **P1 下轮做**。它同时省钱和减少评委决策面，是本轮最实际的未采纳项；没做是因为要动 execute/judge 的调用编排，与本轮"只加算术、不动链路"的边界冲突 |
| N8 | 多评委 panel + `mode/majority` 归约、原始判定留存（inspect） | 部分已有（双评委 + 仲裁 + 分歧阈值）。缺的是**逐用例**的 panel 与原始判定归档，不是聚合级——记为 P2 |
| N9 | G-Eval 的 logprob 加权与 `rubric=` 分数区间约束（deepeval） | 需要端点回传 logprobs；本仓库经 modelhub 网关，**未验证任何一家上游给 logprobs** ⇒ 先验端点再谈 |
| N10 | 声明式 eval 记录 / 数据集按时间戳定版本（openai/evals、langfuse） | 与 §三十·十 的"归档≠当前代码会算出的值"同源，值得做：把**考卷身份**做成锚点集合的内容哈希，而不是靠 n 和协议名推断。P1 |
| N11 | 红队语料（promptfoo red team：100+ plugins × ~40 strategies） | 本产品有注入存活检测但攻击文本要用户自带。发一份版本化攻击语料是产品面扩，需先定"跑一轮多少钱"的预算判据。P2 |
| N12 | 合成用例 + `critic_model` 过滤（deepeval synthesizer） | 本产品的用例是 LLM 生成、无 critic 复筛；与 §三十一·四 的结论冲突（评委没判别力时 critic 也是同一个评委）→ 阻塞在 C1 之后 |
| N13 | 自动最优提示词搜索 / GEPA 式反思（沿用第二轮 N2/N3 的不采纳） | **维持不采纳，且理由换掉**：上一轮引的是二手博客的负结果（未复核，见 §5）；本轮的直接理由是本仓库评委在 owner 标签上 **AUC 0.48~0.64**——以这种量具为目标的搜索，优化的是它没读出来的那一维 |
| N14 | 把提示词当程序编译（DSPy 范式）、云端版本库、图形看板、多租户 | 维持第二轮结论不变（目标物不同 / 与"零出网也能用"冲突 / 报告即界面 / 单运维者） |

### 4. 一处口径借法（已在实现里）

GEPA 的 rollout 缓存键带 **split**、train/val 命名空间隔离（`core/engine.py`、`core/state.py`）。
本产品的 target/eval 缓存按内容哈希（含模型与采样指纹），**没有 split 维度**——
将来任何"留出集"改造若复用这份缓存，留出侧的分数会被训练侧的热缓存重放出来。
本轮不做留出集，但把这条写死在这里，防止下一轮顺手就踩。

### 5. 上一轮引用的复核结果（必须公开）

第二轮 §1/§4 的两条关键"负结果"——`arXiv:2604.14585`（72 次优化里 49% 低于 zero-shot）
与「GEPA 在 holdout 上过拟合、跨模型迁移未验证」（dev.to 一篇博客）——**本轮两路调研都
未能取到一手来源**。它们当时被用来支撑 N2/N3 的不采纳结论。处理方式：
结论维持（本轮有自家的 AUC 实测作替代理由），但**引用降级为"未证实的二手说法"**，
后续文档不得再以它们为主要依据。同理，`Google-Prompt-Opt/prompt-opt`、
`zou-group/awesome-textgrad`、`keirp/GPT-Optimizer`、`langchain-ai/opthax` 现在
HTML/API/raw 三路 404——不存在或已改名；TextGrad 的 org 已从 `purir` 迁到 `zou-group`。
竞品分析里"某项目已死"与"某项目不存在"是两种说法，本轮只写能证的那一种。
