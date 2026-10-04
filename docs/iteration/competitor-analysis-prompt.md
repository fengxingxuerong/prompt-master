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
