# 研究报告：开源提示词优化与评测工具竞品巡礼

- 日期：2026-09-24
- 对象：PromptMaster Suite（pm 提示词优化闭环内核）的同类竞品
- 格式：学术 / 完整版报告
- 置信度：中-高（核心竞品坐标经 GitHub API 当日实测；方法学细节经官方/论文来源交叉验证；生态判断含时效性风险）

---

## 摘要

本报告调研了 GitHub 等开源平台上与 prompt-master（提示词生成 → 测试 → 评估 → 迭代 → 交付闭环）同类的竞品与邻近工具。核心发现：

1. **竞品分三类，没有单一工具同时覆盖 prompt-master 的闭环**：程序化优化框架（DSPy 38.2k★ / TextGrad 3.7k★ / OPRO 778★ / PromptWizard 4.0k★ / Microsoft Trace 760★）、评测与红队工具（promptfoo 25.4k★ 已被 OpenAI 收购、DeepEval 18.4k★、OpenAI Evals 19.5k★）、提示词工作台（linshenkx/prompt-optimizer 35.6k★，唯一形态最接近的"优化+测试+评估+沉淀"开源产品）。
2. **生态在快速整合**：promptfoo 于 2026-03-09 被 OpenAI 收购（官方公告 + CNBC 实锤）；2026 年另有同类整合；市场报告显示 LLM 提示词工具市场 2024 年 4.56 亿美元、预计 2031 年 10.18 亿美元（CAGR 12%）。
3. **所有自动优化器的共同软肋是评估质量与成本**：TextGrad 有被论文与 issue 双重记录的上下文膨胀/梯度爆炸问题；DSPy 的 MIPRO 已在 0.4.0 移除；优化质量高度依赖 LLM 评委的可信度——这正是 prompt-master 双评委+仲裁+评委校准体系的价值锚点。
4. **prompt-master 的差异化不是单项能力，而是组合**：多评委仲裁防放水、注入存活门禁、预算总闸、跨 run 显著性检验、CLI 四态契约 + MCP + Web 多入口交付——这些在 12 个被检竞品中没有任何一个完整具备。
5. **两个风险信号**：a) 与消费级 Claude Skill「prompt-master」存在命名撞车（第三方文章已在使用该名字）；b) 直接竞品 linshenkx 无 LICENSE 文件（NOASSERTION），若其转闭源或商用化，工作台类用户可能迁移。

**给项目的建议**（详见结论）：对外输出应主打「给 Agent 用的一站式提示词质检与优化服务」这一空位；对内可借 DSPy/PromptWizard 的方法论补强优化器（如示例引导），借 promptfoo 的插件体系补强注入用例库；命名层面需评估撞车风险。

---

## 研究问题

1. 开源平台上存在哪些与 prompt-master 核心能力（提示词优化闭环）同类的项目？各自范式与能力边界？
2. 生态活跃度如何（stars / 最近提交 / license）？哪些已停滞？
3. prompt-master 的差异化位置：哪些能力是竞品普遍缺失的？
4. 竞品的方法论缺陷（尤其自动优化器的评估依赖与成本）如何反证 prompt-master 的设计？
5. 命名与商业化风险有哪些？

## 研究方法

### 检索策略

- **权威数据层**：GitHub REST API 当日实测（2026-09-24）——搜索 `prompt optimization` / `prompt evaluation` / `llm evaluation framework` 三个关键词按 stars 排序，外加 `topic:prompt-optimization`、`topic:prompt-evaluation` 主题检索；对 13 个指定候选仓库逐一 GET `/repos/{owner}/{repo}` 取 stars/最近 push/license/描述。
- **方法学层**：Web 检索 DSPy（MIPRO/BootstrapFewShot）、TextGrad（失败模式）、PromptWizard（双阶段机制）、linshenkx（工作台功能）、promptfoo（红队架构）——以官方文档（promptfoo.dev、labs.ai.azure.com、textgrad.com、openai.com）、arXiv 论文原文、GitHub issue 为一手来源。
- **生态层**：行业盘点文章（FutureAGI、DeepWiki、ToolRadar、PromptQuorum 市场报告）、媒体（CNBC、环球网/光明网、Infosecurity）用于市场整合与收购事实。
- **中文补充**：国内生态（PromptPerfect 商用定价、CSDN/插件市场对 linshenkx 的介绍）仅作补充维度，不与权威来源冲突。

### 纳入 / 排除标准

- 纳入：GitHub 上以「提示词优化」「提示词评测」「LLM 应用测试」为核心能力的开源项目；近 24 个月内存在任何更新的活跃项目与已停滞但仍有参考价值的项目。
- 排除：纯提示词工程文档/Awesome 清单（无执行引擎）；纯 LLM 编程框架（如 Guidance 仅作参考列入但不作为直接竞品）；闭源 SaaS（PromptPerfect、LangSmith、Helicone、PromptLayer 等仅作市场背景）。

### 评估框架

- **事实层**（stars、license、日期、收购）以 GitHub API 与官方公告为准；
- **方法层**以官方文档与论文原文为准，行业文章只作背景；
- **能力对比**基于官方 README/文档的功能描述，不做运行时实测（本报告为静态竞品测绘，不含基准测试）。

## 研究发现

### 维度一：程序化优化框架类（自动优化「怎么做」的范式源）

| 项目 | Stars（2026-09-24 API）| 最近 push | License | 核心范式 |
|---|---|---|---|---|
| stanfordnlp/dspy | 38,243 | 2026-09-23 | MIT | 声明式 LLM 程序 + 优化器（BootstrapFewShot / MIPROv2）；把提示词当可编译的"程序"而非字符串 |
| zou-group/textgrad | 3,744 | 2025-07-25 | MIT | PyTorch 风格文本反向传播；LLM 生成自然语言"梯度" |
| microsoft/PromptWizard | 4,012 | 2025-10-13 | MIT | 任务感知、智能体驱动的双阶段优化（指令 + 上下文示例），合成 CoT 与专家人格 |
| google-deepmind/opro | 778 | 2024-12-04 | Apache-2.0 | LLM 作为优化器的元提示词迭代 |
| microsoft/Trace | 760 | 2026-06-17 | MIT | 面向 Agent 的端到端生成式优化 |

关键事实与边界：

- **DSPy 的 API 不稳定**：MIPRO 优化器已在 0.4.0（2024 年中）移除，现役主优化器为 BootstrapFewShot 系列——"程序化优化"范式仍在快速迭代、迁移成本高（来源：theneuralbase、ai-tldr，两处一致）。
- **TextGrad 有被正式记录的深度缺陷**：arXiv 2601.21064 指出文本反向传播存在"梯度爆炸/消失"（反馈随层数指数膨胀、LLM 评委偏置累积、"lost in the middle"）；GitHub issue #2 记录约 98 个误分类样本即触发 context_length_exceeded（每样本 600-1000 字符梯度无界累积）；GitCode 博客实测单任务梯度串膨胀到 184 万字符。这些缺陷恰好落在 prompt-master「评委可信度 + 预算闸 + 迭代轮次护栏」设计的射程内。
- **PromptWizard 是方法论上最接近 prompt-master 的**：自我进化、反馈驱动、指令与示例联合优化——但其形态是研究框架（需自行接数据与评估），无服务化交付、无注入防御、无预算控制（来源：microsoft labs.ai.azure.com、DeepWiki）。
- **所有此类工具的共同前提**：需要你自带评估集与指标。评估质量决定优化质量——DSPy 社区文章普遍建议用 LLM-as-judge 指标（来源：datasops、ai-tldr）。

### 维度二：评测与红队类（评估侧，prompt-master 的邻居而非对手）

| 项目 | Stars | 最近 push | License | 定位 |
|---|---|---|---|---|
| promptfoo/promptfoo | 25,409 | 2026-09-24 | MIT | LLM 应用评测 + 自动红队（50+ 漏洞类型插件：注入/jailbreak/PII/越权）；2026-03-09 被 OpenAI 收购，保持开源 |
| confident-ai/deepeval | 18,419 | 2026-09-23 | Apache-2.0 | LLM 评测框架（指标分解、pytest 集成、Python 锁定） |
| openai/evals | 19,500 | 2026-04-14 | NOASSERTION | 评测框架 + 基准注册表（半停滞，最近 push 已 5 个月） |

关键事实：

- **promptfoo 收购是市场信号**：OpenAI 官方公告（2026-03-09）确认收购并整合进 Frontier 平台；promptfoo 官方博客 + CNBC + 环球网多源一致。其社区版保留 MIT 开源、350k 开发者、25%+ 财富 500 强使用（来源：openai.com、promptfoo.dev、cnbc.com）。评估/红队/合规被证实是企业 AI Agent 落地的"把关者"——prompt-master 的注入门禁与质量门同属这一被市场验证的品类。
- **DeepEval 的 Python 锁定**被行业文章明确视为采用摩擦（scrolltest.com）——与 prompt-master 的零依赖 + CLI/API/MCP/Web 多入口形成对照。
- **评测工具与优化工具是分开的两类**：行业盘点（FutureAGI、dev.to）一致认为"没有单一工具覆盖版本管理+评测+优化"——dev.to 一篇实证文章记录"一个词的改动掉 9 个点而聚合均值没暴露"的现象，说明**分片/分层的评估粒度**是真实刚需（prompt-master 的最低分/分维度报告正对应此缺口）。

### 维度三：提示词工作台类（形态最近的直接竞品）

| 项目 | Stars | 最近 push | License | 形态 |
|---|---|---|---|---|
| linshenkx/prompt-optimizer | 35,562 | 2026-09-21 | **NOASSERTION（无 LICENSE）** | Web 应用 + Chrome 插件 + Docker；一键优化/多轮迭代/双模式（system/user）/原版对比测试/多模型（OpenAI、Gemini、DeepSeek、智谱、SiliconFlow）/文生图/高级测试（变量、多轮、函数调用）/版本回溯/纯客户端处理 |

关键事实：

- 这是**形态上最接近 prompt-master** 的开源产品：同样是「优化 → 测试 → 评估 → 沉淀」闭环，同样是多模型、有对比测试与版本回溯。
- 三处差异：a) 它面向**人**（工作台/插件 UI），prompt-master 同时面向**人与 Agent**（CLI 四态契约 + MCP 工具可直接被 OpenClaw/Loomy 挂载）；b) 它无独立评测层（评估依赖用户自行对比输出），prompt-master 有双评委+仲裁+校准；c) **无 LICENSE 文件**——NOASSERTION 意味着下游使用与商业化存在法律不确定性，且 35k stars 的项目若转闭源/商用会形成生态空位。
- stars 交叉验证：GitHub API 当日 35,562（权威）；第三方工具站记录 33.2k（略旧）；besthub 文章称"四个月涨 9.1k★"——高速增长是近年现象。

### 维度四：生态活跃度与整合（背景）

- **活跃**：DSPy（9-23 推）、promptfoo（9-24 推）、DeepEval（9-23 推）、linshenkx（9-21 推）、OpenAI Evals（4-14，偏缓）。
- **停滞**：TextGrad（2025-07，已 14 个月无更新）、OpenPrompt（2024-07）、OPRO（2024-12）——论文类项目普遍在论文发表后停止维护，这从侧面说明：**优化方法论开源易、工程化交付难**，正是 prompt-master 的工程纵深（测试集锁定、缓存幂等、断点续跑、多 worker 共享）的稀缺点。
- **市场整合**：PromptQuorum 市场报告（2026-03）——2024 年 4.56 亿美元、2031 年预计 10.18 亿美元（CAGR 12%）；2026 年初两起重大收购标志市场整合期开始。
- **中文生态**：商用端 PromptPerfect（Jina AI，闭源，$20/月，23 模型）偏消费者场景；开源端 linshenkx 是中文圈最响的；prompt-master 的「给 Agent 用 + 中文场景实测」在中文生态里暂无对位竞品。

## 分析

### 综合分析

竞品地图可归为「优化引擎」「评测关卡」「消费工作台」三块，prompt-master 是唯一把三块**闭环且服务化**的项目：引擎侧有 clarify→optimize→revise 图编排（对应 DSPy/PromptWizard 的优化能力，且已把修订方向收敛为「只改点名句」等硬约束）；关卡侧有测试集锁定、双评委+仲裁、注入存活门禁、跨 run 显著性（对应 promptfoo/DeepEval 的评测与红队，且多了评委校准与防放水设计）；交付侧有 CLI 四态 + MCP + REST + Web 五入口（对应 linshenkx 的工作台，但多了 Agent 可编程消费）。

### 矛盾分析

- **promptfoo stars 口径**：GitHub API 当日 25,409 vs 第三方文章 21,884（2026-09-11 记录）→ 日期差异，取 API（高权威）。
- **linshenkx stars 口径**：API 35,562 vs ai-tldr 33.2k vs besthub 早期 9.1k 四个月 → 不同时点数据，取 API；增长率是估计（besthub 单源，低权威）。
- **"prompt-master"名称撞车**：aiec.fun（2026-08）与 mcp.csdn.net 均在介绍一个名为 prompt-master 的 Claude Skill（理念是"最好的 prompt 不是最长的"），与本项目重名。撞车本身不构成功能冲突（一个是消费级 Skill，一个是工程化服务），但影响检索与品牌区分——这是需要用户决策的命名风险，非技术事实。

### 置信度评估

- 高：stars/license/日期/收购（GitHub API + 官方公告多源）；TextGrad 缺陷（论文 + issue + 博客三方一致）；promptfoo 收购（OpenAI/promptfoo 官方 + CNBC）。
- 中：能力对比基于文档而非运行时实测；linshenkx 的"最接近竞品"判断依赖功能清单对照；市场数据（PromptQuorum）为行业报告单一来源。
- 低：生态影响推断（"若 linshenkx 转闭源则空位"属情景分析）。

## 局限性

1. 本报告是**静态功能测绘**，未对任何竞品做运行时基准对比（未验证各自优化后的实际提分幅度）。
2. 闭源 SaaS（LangSmith、PromptLayer、Helicone、PromptPerfect）未深入，仅作市场背景——若用户要评估"商用替代品"维度需另立研究。
3. linshenkx/prompt-optimizer 的详细能力来自官方站点与商店描述，未做源码级验证（35k stars 的 repo 未逐一核对功能实现）。
4. 活跃度以"最近 push"近似，未统计 issue/PR 速率与贡献者数。
5. 中文生态的闭源企业工具（如各大云厂商 prompt 套件）未纳入——它们属于另一种竞争面。

## 结论

1. **不存在完整对位的开源竞品**：没有任何被检项目同时具备「自动化优化 + 可信评测（双评委/仲裁/校准）+ 注入防御 + 预算控制 + Agent 可编程交付」。prompt-master 的护城河是组合而非单项。
2. **最大的直接形态竞品是 linshenkx/prompt-optimizer**（35.6k★，无 LICENSE）——建议持续追踪其动向；其无 LICENSE 状态既是它的风险也是 prompt-master 的机会。
3. **市场已验证"评测关卡"价值**：promptfoo 被 OpenAI 收购（2026-03）说明评测/红队/合规是企业 AI 落地的刚需——prompt-master 的注入门禁与质量门处于同一被验证品类。
4. **方法论可借力**：DSPy 的示例引导（BootstrapFewShot）与 PromptWizard 的指令+示例联合优化可反哺 prompt-master 的 optimizer/revise 节点；promptfoo 的 50+ 漏洞插件体系可作为注入用例库的扩充参考。
5. **命名撞车需用户决策**：消费级 Claude Skill「prompt-master」已在使用同名；若本项目要对外开源/宣传，需评估更名或品牌前缀。

**置信度：中-高。** 建议下一步：若做产品化定位，可基于本报告撰写一份一页《对位表》（prompt-master vs linshenkx vs DSPy+promptfoo 组合），并考虑为 linshenkx 建立 star/版本追踪。

## 参考文献

**官方 / 一手（高权威）**
1. GitHub API — 12 个仓库的 stars/forks/pushed_at/license 实测（2026-09-24 当日拉取）：stanfordnlp/dspy、linshenkx/prompt-optimizer、promptfoo/promptfoo、guidance-ai/guidance、openai/evals、confident-ai/deepeval、mshumer/gpt-prompt-engineer、thunlp/OpenPrompt、microsoft/PromptWizard、zou-group/textgrad、google-deepmind/opro、microsoft/Trace、PromptBranch/promptbranch
2. OpenAI — 《OpenAI to acquire Promptfoo》（openai.com/index/openai-to-acquire-promptfoo，2026-03-09）
3. Promptfoo 官方 — 《Promptfoo joining OpenAI》（promptfoo.dev/blog，2026-03-09）
4. Microsoft Foundry Labs — PromptWizard 页面（labs.ai.azure.com/innovations/promptwizard）
5. TextGrad 官网 — textgrad.com
6. Promptfoo 官方文档 — intro / red-team architecture（promptfoo.dev）
7. arXiv 2601.21064 — 《Textual Equilibrium Propagation for Deep Compound AI Systems》（文本梯度爆炸/消失的形式化分析）
8. arXiv 2506.00400 — 《Scaling Textual Gradients via Sampling-Based Momentum》（文本梯度缩放边界）
9. PromptQuorum — 《提示词优化与比较工具：2026 年市场概览》（市场数据 4.56 亿→10.18 亿美元，2026-03）

**媒体 / 行业（中权威）**
10. CNBC — OpenAI to buy cybersecurity startup Promptfoo（2026-03-09）
11. 环球网/光明网 — OpenAI 收购 Promptfoo 报道（2026-03-12）
12. Infosecurity Magazine — OpenAI's Promptfoo Deal（2026-03-11）
13. FutureAGI — Top 10 Prompt Optimization Tools in 2026
14. DeepWiki — Prompt Optimization and Evaluation Tools 章节 / PromptWizard 词条
15. ToolRadar — Best Prompt Engineering Tools（2026-09-22）
16. dev.to（ethanwritesai）— 《A one-word prompt edit dropped our accuracy 9 points》（2026-07-29，分层评估必要性实证）
17. aiec.fun — 《用 prompt-master 给 Claude 装个提示词专家》（2026-08-04，命名撞车证据）
18. always200.com / Chrome Web Store — linshenkx prompt-optimizer 官方功能清单
19. scrolltest.com — DeepEval vs PromptFoo 2026
20. ai-tldr.dev — DSPy Optimizers / TextGrad / Prompt Optimizer 词条
21. theneuralbase.com — MIPRO 在 DSPy 0.4.0 移除的事实记录
22. GitHub issue — zou-group/textgrad #2（上下文膨胀实测）
23. GitCode 博客 — TextGrad 上下文长度限制技术方案 / PromptWizard 深析
24. appsecsanta.com / sofarbot — promptfoo 概况与用户规模（stars 口径与 API 有出入，已在矛盾分析记录）
25. besthub.dev — prompt-optimizer 增长文章（9.1k★ 四个月，低权威，仅作线索）

## 附录

### A. 搜索日志（本报告检索的查询与用途）

| 查询 | 平台 | 用途 |
|---|---|---|
| prompt optimization / prompt evaluation / llm evaluation framework（按 stars） | GitHub API | 全景扫描 |
| topic:prompt-optimization / topic:prompt-evaluation | GitHub API | 主题侧扫描 |
| 12 个候选仓库 GET /repos | GitHub API | 权威坐标（stars/活跃度/license） |
| 开源提示词优化工具 GitHub 竞品对比 | Web | 行业盘点与定位 |
| DSPy MIPRO BootstrapFewShot 适用范围 | Web | 范式细节与版本变更 |
| promptfoo red teaming 功能 | Web | 评测侧能力 |
| TextGrad textual gradient limitations | Web | 方法论缺陷（论文+issue） |
| Microsoft PromptWizard 机制 | Web | 优化方法论对照 |
| linshenkx prompt-optimizer 功能 | Web | 直接竞品深挖 |
| promptfoo acquired by OpenAI | Web | 收购事实交叉验证 |
| PromptPerfect 中文商用定价 | Web | 中文生态背景 |

### B. 竞品坐标原始数据（GitHub API，2026-09-24）

```
stanfordnlp/dspy            38,243★  push 2026-09-23  MIT
linshenkx/prompt-optimizer  35,562★  push 2026-09-21  NOASSERTION
promptfoo/promptfoo         25,409★  push 2026-09-24  MIT
guidance-ai/guidance        21,776★  push 2026-05-21  MIT（参考：LLM 编程框架，非直接竞品）
openai/evals                19,500★  push 2026-04-14  NOASSERTION
confident-ai/deepeval       18,419★  push 2026-09-23  Apache-2.0
mshumer/gpt-prompt-engineer  9,680★  push 2025-10-16  MIT
thunlp/OpenPrompt            4,898★  push 2024-07-16  Apache-2.0（停滞）
microsoft/PromptWizard       4,012★  push 2025-10-13  MIT
zou-group/textgrad           3,744★  push 2025-07-25  MIT（停滞 ~14 个月）
google-deepmind/opro           778★  push 2024-12-04  Apache-2.0（停滞）
microsoft/Trace                760★  push 2026-06-17  MIT
PromptBranch/promptbranch        9★  push 2026-09-21  MIT（提示词库+MCP，小体量线索）
huggingface/prompting-toolkit   404（不存在）
```

### C. 交叉验证记录

1. promptfoo 收购：OpenAI 官方 + promptfoo 官方博客 + CNBC 三方一致，日期 2026-03-09 → **高置信，无冲突**。
2. promptfoo stars：API 25,409（当日）vs sofarbot 21,884（2026-09-11 快照）→ 时点差异，取 API。
3. linshenkx stars：API 35,562 vs ai-tldr 33.2k vs besthub "9.1k/4 个月" → 时点差异，取 API；增长率单源低权威。
4. TextGrad 缺陷：GitHub issue#2 + GitCode 博客实测 + arXiv 论文形式化 → **三源一致**。
5. DSPy MIPRO 移除：theneuralbase + ai-tldr 两源一致 → 高置信。
