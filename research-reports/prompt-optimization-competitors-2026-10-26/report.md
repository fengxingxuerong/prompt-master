# 开源竞品对照更新 · 本周（2026-10-19 ~ 2026-10-26）

> 口径：本文件所有 stars/license/最近 push 均为 **2026-10-26 当日实测**。
> 逐仓坐标来自 GitHub REST `/repos/{owner}/{repo}`（本轮恰好撞上匿名限额 60/h 用尽，见 §六 的取证说明）
> 与 `/search/repositories`（`repo:` 限定符补齐）；关键词/topic/新建仓来自 7 组搜索；本周 trending 来自
> GitHub Trending 周榜（第三方镜像 RSS，pubDate 2026-10-25）；功能描述取自各仓库 README 原文（存 `raw/`）。
> Δ 基线 = 本仓 `research-reports/prompt-optimization-competitors-2026-10-13/report.md`（2026-10-13 当日实测）。
> **未做运行时基准**，功能对照为文档/文件树级测绘；每条主张后面标了出处，未复核的写"未复核"。
> 复现：`fetch.ps1`（搜索+坐标）→ `fetch2.ps1`（trending+README）→ `analyze.py`（汇总，输出 `data-summary.txt`）。

## 一、当日坐标系与 13 天 Δ

| 仓库 | 10-13 | **10-26** | Δ(13天) | License | 最近 push | 备注 |
|---|---|---|---|---|---|---|
| linshenkx/prompt-optimizer | 37,735 | **39,241** | **+1,506** | AGPL-3.0 | 2026-10-26 | 全赛道最快，六端分发 |
| stanfordnlp/dspy | 38,601 | **38,790** | +189 | MIT | 2026-10-25 | 标准答案，增长放缓 |
| langfuse/langfuse | 35,701 | **36,486** | +785 | NOASSERTION | 2026-10-26 | 自述已改成 "Open source **agent evals** & observability" |
| promptfoo/promptfoo | 25,937 | **26,429** | +492 | MIT | 2026-10-26 | README 首行确认「now part of OpenAI」，仍 MIT；新增 code scanning |
| comet-ml/opik | 22,530 | **22,925** | +395 | Apache-2.0 | 2026-10-26 | 观测+评测 |
| openai/evals | 19,591 | **19,654** | +63 | NOASSERTION | 2026-04-14 | 停滞半年+ |
| confident-ai/deepeval | 18,770 | **19,104** | +334 | Apache-2.0 | 2026-10-23 | 评测框架 |
| Arize-ai/phoenix | 11,808 | **12,004** | +196 | NOASSERTION | 2026-10-25 | 观测+评测（本轮走搜索读数） |
| gepa-ai/gepa | 6,955 | **7,148** | +193 | MIT | 2026-10-22 | 见 §三.3 |
| Kiln-AI/Kiln | 5,190 | **5,233** | +43 | NOASSERTION | 2026-10-25 | 桌面工作台 |
| microsoft/PromptWizard | 4,018 | **4,022** | +4 | MIT | 2025-10-13 | **恰好停摆一整年** |
| zou-group/textgrad | 3,758 | **3,770** | +12 | MIT | 2025-07-25 | 停摆 ~15 个月 |
| UKGovernmentBEIS/inspect_ai | 2,989 | **3,094** | +105 | MIT | 2026-10-26 | 官方评测框架，仍在维护 |
| meta-llama/prompt-ops | 1,037 | **1,027** | **−10** | MIT | 2026-04-21 | **负增长**，停摆半年 |
| **fengxingxuerong/prompt-master（本仓）** | — | **0** | — | MIT | 2026-10-25 | 2026-09-26 建仓，公开，**未上 PyPI**（`pypi.org/pypi/prompt-master/json` 404） |

三条读数直接改写旧判断：

1. **linshenkx 从"唯一可能逼近的对手"变成"增长最快的对手"**：13 天 +1,506★（DSPy 同期 +189，约 8 倍），
   且今天还在推。它的评估层（本轮 README 复核）仍是 **UI 级**「Analysis and Compare Evaluation / 单结果评估 /
   多结果对比评估 / evaluation-driven smart rewrite」——**没有量化层**（无锚点校准、无显著性、无噪声带、无 CI 门禁）。
   ⇒ 第四轮 PD-5 的观察点（"评估层是否做深"）本两周**没有发生**；它把力气继续投在分发（在线版/Web/桌面/插件/Docker/Vercel/CF/MCP）。
2. **停摆区扩大到反而倒退**：prompt-ops −10★、PromptWizard 整年 +4★、textgrad 15 个月 +12★。
   这个赛道"自动优化"的叙事冷却时，**活下来的是生态接入与人用价值**（DSPy/GEPA/DeepEval 靠生态，linshenkx 靠体验）。
3. **本仓自身在这张表上是 0★、未发布包**：所以本轮所有"靠齐方向"里，分发类（§五 PE-1）不是锦上添花，是当前第一短板。

## 二、本周（10-19~10-26）热门：GitHub 周榜几乎全是「Agent 技能/插件」

GitHub Trending 周榜（pubDate 2026-10-25）15 条：

| # | 仓库 | 性质 |
|---|---|---|
| 1 | **cloudflare/security-audit-skill** | **Agent 技能**：多阶段安全审计 + 独立验证（见 §三.1） |
| 2 | morluto/rea | 逆向工程 Agent（与提示词无关，但它是本周星数最高的同名现象） |
| 3 | **anthropics/claude-plugins-official** | 官方插件市场 |
| 4 | ruvnet/ruflo | Agent 编排/控制台 |
| 5 | stablyai/orca | Agent 运行时 |
| 6 | anomalyco/opencode | 编码 Agent |
| 7 | **cursor/plugins** | 编辑器插件市场 |
| 8 | **android/skills** | **技能**（大厂把技能当交付物发） |
| 9 | **anthropics/knowledge-work-plugins** | 官方知识工作插件 |
| 10 | heygen-com/hyperframes | 视频渲染 |
| 11 | Comfy-Org/ComfyUI | 生成管线 |
| 12 | akitaonrails/ai-memory | 记忆 |
| 13 | Graphify-Labs/graphify | 代码库知识图谱 |
| 14 | HKUDS/CLI-Anything | CLI 化 |
| 15 | vllm-project/vllm | 推理 |

**本周新建、且两天内破千星的项目，也几乎全是技能/插件**（搜索 `created:>2026-10-19` 实测节选）：
`Jakeschincariol/day-trader-skill`（1,709★，11 个 Claude 技能）、`morganlinton/BeautifulRust`（844★，Claude Code 插件，
教模型写地道 Rust）、`songjianyang/xss-audit`（425★，14 层静态审计技能）、`rossark0/rossark-motion-skill`（494★）、
`actionow-ai/vvv`（377★，Agent 技能形式的视频模板）、`DenisSergeevitch/ex-ussr-kinship-research`（552★，"证据优先"家谱技能）。

**本周在榜的同类"审计/验证"侧**（搜索 `topic:llm-evaluation` + `llm-as-a-judge` 实测）：

| 仓库 | stars | 它自称在做的事（README 原文要点） |
|---|---|---|
| ifixai-ai/iFixAi | **34,502** | 独立审计 AI Agent：把 Agent 放进业务仿真里跑 61 项检查，**评委来自与 Agent 不同的厂商**，A–F 评级 + 三条最低门槛，JSON/MD 报告，`--min-score` 直接进 CI，Plugin/Skill 双分发 |
| NVIDIA/garak | 9,699 | LLM 漏洞扫描 |
| Tencent/AI-Infra-Guard | 7,091 | 红队平台：Skill Scan / MCP Scan / 越狱评测，**`aig-skill-scan` 公开 F1/精确率/召回/FPR 表**并挂 SkillTrustBench T01–T09 分类学，供企业 CI/CD |
| Q00/ouroboros | 6,275 | "Agent OS"：访谈（歧义分 ≤0.2 才准写码）→ 规格 → 执行 → **三级评估闸：机械($0) → 语义 → 多模型共识**，稳定性 ≥0.95 才收敛 |
| Giskard-AI/giskard-oss | 5,923 | Agent 评测/测试库 |
| truera/trulens | 3,611 | 评测/追踪，**"TruLens judges are graded against human annotations, out of the box"** + 公开数字（Agent GPA 捕获 95% vs 基线 55%，arXiv:2510.08847） |
| hashgraph-online/hol-guard | 996 | Agent 杀软 + `plugin-scanner`（技能/插件信任打分、**SARIF** 报告、GitHub Action、8 种运行时的插件打包） |
| Nanako0129/pilotfish | 711 | 多模型编排：`verifier` 用**全新上下文证伪**实现结果，文档明写"claim limits / behavioral evidence" |
| zgcagi/Harness-RSI | 415 | Harness 自演化：目标/理论/评估/**上报证据** 的更新规则（10-01 建仓，本周仍在推） |
| ckelsoe/prompt-architect | 325 | **提示词技能**：31 个框架 × 7 类意图，自带质量评分，Claude Code 插件/npm/zip 分发 |
| sutro-sh/jev-align | 311 | **校准式标注 + GEPA**：挑"模型不确定"的样本请人标（含随机审计样本）→ 跑 GEPA → 报 certainty 变化；训练分再高也不自动采纳 |

**一句话**：本周生态的重心不在"把提示词写漂亮"，而在 **① 技能/插件是新的交付与分发单位；② "验证/审计 Agent 的产出"取代"优化提示词"成为显性叙事；③ 报告要机器可读（JSON schema / SARIF）+ 可进门禁**。

## 三、逐家核对：只列与 prompt-master 直接可比的

### 3.1 cloudflare/security-audit-skill（本周周榜第 1）

- **它做了什么**：六阶段审计（侦察 → 覆盖账本驱动的狩猎 → 候选验证 → 结构化产出 → 独立记录验证 → 目标中立报告）。
  关键设计：**"验证者永远不是发现者"**；裁决词表三态 `confirmed` / `needs_validation` / `rejected`，
  其中 `needs_validation` 必须带"一个确切未决事实且不给严重度"；`report-schema.json` + 两个零依赖校验脚本
  （`validate-findings.cjs` / `validate-coverage-ledger.cjs`）强制形状；MIT。
- **对照**：prompt-master 的"如实说清哪里还不行"目前是**报告里的散文 + 退出码四态**；
  Cloudflare 把"未决"做成了**被 schema 和校验脚本卡住的结构字段**。这是本项目"诚实"叙事最缺的那道工程化。

### 3.2 iFixAi（34.5k★，本轮最大新面孔）

- **与 prompt-master 撞的设计**：跨厂商评委（"picks a judge from a different provider than your agent"）、
  `--dry-run` 先报测试数+评委调用数、JSON/Markdown 双报告、`--min-score` 进 CI、Plugin/Skill 双分发。
- **它的独有件**：61 项检查的**成套考卷**、A–F 评级 + **三条一票上限门槛**（任一不过封顶 D）、
  每条检查附证据、按业务仿真发"越权请求"的对抗样本生成器。
- **它没有的**：**评委可信度**。README 全篇没有校准/人工锚点/一致性读数（grep `calibrat|human|anchor|reliab` 实测 0 命中）。
  ⇒ prompt-master 的"量具自证"仍是差异点；但 iFixAi 证明了**"审计"这个词本身有 34k★ 的市场**，且它用它拿了分发。

### 3.3 GEPA（7.1k★）——从"方法论上游"变成"评委校准的引用来源"

- 本轮 README 实测两处新证据：① `optimize_anything` 已把优化对象扩到"任何文本制品"；
  ② 生产案例里出现 **LLM-as-a-judge 提示词的优化**——Nubank 案例自述把 GPT-4.1 vs GPT-4.1-mini 的
  **Cohen's κ 从 0.00 抬到 0.745**（并给出 E2 准确率 68.88%→88.89%）；Microsoft AI 案例用 ~2,000 条人工标签筛预训练页。
- **仍未解决的**：README 全程"you provide an evaluator"，**没有 ground truth 就没有目标函数**——
  这条既保住了 prompt-master 的空位，也说明"评委校准"正在变成**有标签者**的常规动作，而 prompt-master 卡在**没标签**。

### 3.4 truera/trulens（3.6k★）——旧判断需要降级的一条

- 它明写"**judges are graded against human annotations, out of the box**"，并给公开基准（95% / 0.81 F1）。
- **诚实更正**：`docs/evaluation.md` 与两份旧报告里"**没有任何竞品把量具自己拿来量**"的说法，
  在"用**公开基准**的人工标注给自己的评委打分"这个意义上**已被部分证伪**。
  仍成立的部分（要写清楚）：它量的是**它自己基准集**上的一次性判别力，不是**你任务上**的偏差/κ/CI，
  也不报"样本不足 ⇒ 不可估"。**结论不是"我们独有"，而是"别人量在一张固定卷子上，我们量在你自己的卷子上并且敢说不可估"。**

### 3.5 Tencent/AI-Infra-Guard（7.1k★）+ hol-guard（996★）——静态体检线的天花板

- AI-Infra-Guard 的 `aig-skill-scan`：对齐 SkillTrustBench T01–T09 的**分类学**，公开 **F1/精确率/召回/FPR 表**，
  一条命令进企业 CI/CD，另有 Agent 技能形态（ClawHub）。
- hol-guard 的 `plugin-scanner`：**信任分有公开的权重与基线适配器**，报告支持 text/json/markdown/**SARIF**，
  带 GitHub Action，一条命令把插件打包到 8 种 Agent 运行时。
- **对照**：prompt-master 的 `check`/`diff`/`gate` 是**规则层**，但**没有一张"规则判据的精确率/误报率/覆盖率"公开表**。
  竞品把"体检"的可靠性也量化公开了 —— 这是本仓目前**只对评委做、没对自己规则做**的那一半。

### 3.6 ketsoe/prompt-architect（325★）与 jev-align（311★）——小仓，但各拿走一样我们缺的

- **prompt-architect**：把"提示词优化"直接做成**技能**（31 框架 + 质量评分 + 框架选择决策树），分发走 Claude Code 插件/npm/zip。
  它证明**这个空位可以被技能形态占掉**，而且门槛很低。
- **jev-align**：**不确定性采样 + 随机审计样本 → 人工标注 → GEPA**，报 certainty 变化，metric 调用有预算（默认 300），
  且明写"训练分更高**不**自动采纳提案"。它给出了 prompt-master 卡点（缺 owner 亲判锚点）的**成熟解法形状**。

### 3.7 直接形态竞品 linshenkx（39.2k★）——两周未变的部分

- 分发：在线版 / Web / 桌面（含自动更新）/ Chrome 插件 / Docker / Docker Compose / Vercel / Cloudflare / MCP Server。
- 评估：仍是 UI 级分析+对比+评估驱动重写；**无量化层、无断言、无 CI 门禁**（本轮 README 逐节复核）。
- ⇒ prompt-master 在"可信评估/零调用门禁/Agent 协议"上仍领先，**但在"随手装起来"上落后一个数量级**，且差距在扩大。

## 四、净结论：定位没变，但"独有"要收窄措辞

**仍然独有（本轮核对后站得住）**：
1. **在用户自己的任务锚点上量化评委**（偏差/κ/AUC/CI + 可估性门槛 + 跨轮分代），并**在样本不足时拒绝给结论**。
   竞品要么量在固定基准卷子上（TruLens），要么有了标签才优化评委（GEPA/Nubank）。
2. **零调用能力线**（`check`/`diff`/`gate`）：规则体检、版本差分、git 基线增量门禁。竞品里没有同形态对标物
   （最接近的是 AI-Infra-Guard 的技能扫描与 hol-guard 的包扫描，但对象是包/技能，不是提示词文本）。
3. **Agent 一等公民**：退出码四态 + `--json` 单对象 + submit/wait 长任务协议 + MCP。iFixAi 有 `--min-score` 与插件，
   但没有"给 Agent 的分支协议"这层。
4. **诚实性作为产品功能**（负结论照发）。这一条本轮得到侧面确认：Cloudflare 技能把"未决"做成结构字段、
   pilotfish 明写"claim limits"——**"承认不知道"正在成为主流做法**，先做不亏。

**必须收窄的措辞（否则下一个人会照旧结论做决策）**：
- ❌「没有竞品把量具自己拿来量」→ ✅「竞品用**它们的**人工标注基准给评委打分；我们在**你的**锚点上量，且报不可估」。
- ❌「分布式部署是我们的短板，但桌面/云不适用」→ 部分过期：**技能/插件形态与 Python 无关**，iFixAi 的 skill 走
  `uvx`、插件走 `/plugin marketplace add`，本仓**完全可以**只发一个技能包，不必上 Vercel/CF。

**最大的结构性风险（本周新增）**：
生态的注意力已从"优化提示词"移到"**让编码 Agent 自己去审、去验、去守门**"。
本仓当前 0★、无 PyPI、无技能包、无 Action —— **空位在，但入口不在**。这不是能力问题，是分发问题。

## 五、靠齐方向（按性价比排序，PE = prompt-master 第五轮）

| # | 抄谁 | 做什么 | 为什么（本轮证据） | 成本 |
|---|---|---|---|---|
| **PE-1** | iFixAi / prompt-architect / cloudflare 技能 / anthropics+kursor 市场 | **把已有的 CLI 包成一个可安装的技能/插件**：仓库内放 `skills/prompt-master/SKILL.md`（触发词：体检提示词 / prompt check / 门禁）+ marketplace 清单；同时用**已存在**的 `scripts/release_pypi.py` 发 PyPI，补一个 GitHub Action | 本周 trending 前 15 里 6 条是技能/插件市场；本周新建破千星的项目几乎全是技能；iFixAi 靠 plugin+skill 拿到了 34.5k★。本仓 README 已承诺"产出可选 SKILL.md"，却**仓库里一个 SKILL.md/marketplace.json/plugin.json 都没有**（实测） | 零 LLM 调用，纯打包；不动评分协议 |
| **PE-2** | cloudflare/security-audit-skill | **把"未决/不可采信"做成结构字段**：给 `--json` 契约加一个裁决枚举（如 `confirmed/needs_validation/rejected`），未决项必须带"确切未决事实"且不许带程度；配一个校验脚本（同仓已有同类校验风格） | 别人把"我不知道"做成了 schema + 校验脚本卡住的产物；我们仍写在报告散文里。这与本仓"诚实性"叙事同源，且**零调用** | 零调用；注意 `--json` 是既有契约，需按仓规加字段不破坏单对象 |
| **PE-3** | Q00/ouroboros | **补"何时停"的显式判据**：把"机械层 → 语义层 → 多模型共识"与"收敛/稳定性"写成产品契约（Δ 稳定到什么程度才停、什么时候升级评委），而不是每轮各自判断 | Ouroboros 明写歧义 ≤0.2 才开工、相似度 ≥0.95 才收敛。本仓 PC-6 已实测否决"级联省钱"（20% 调用、均分 +0.03），**所以只抄停止判据，不抄级联**，避免重复上一轮的否决 | 便宜：主要是文档契约 + 少量断言；零/低频调用 |
| **PE-4** | sutro-sh/jev-align | **改锚点补料的取样规则**：在现有 `scripts/build_owner_rejudge_queue.py` 上，用"评委间分歧/阈值邻域"做不确定性采样，并固定一条**随机审计切片**（防全是难题导致锚点集偏斜）；回填仍走既有审计路径 | 本仓 PC-4 的核心堵点是"缺 owner 亲判锚点"，而 jev-align 已给出成熟形状（不确定样本 + 随机审计样本 + 标注后优化 + certainty 变化）。**只抄取样与回填纪律，不抄 GEPA 优化**（N13 维持不采纳） | 生成队列零调用；人工标注是外部成本 |
| **PE-5** | AI-Infra-Guard / hol-guard | **给规则层体检公开一张可靠性小表**：在一份版本化语料上量 `check`/`gate` 规则判据的精确率/误报率/覆盖率，随文档落档 | 竞品把体检的可靠性量化公开了（skill-scan 的 F1/FPR 表、plugin-scanner 的权重/基线）；本仓只对评委做了这件事，**对自己规则没做**。这也是 `gate` 敢说"只拦增量"的前提 | 零调用、零出网，纯本地统计；产出是数字不是功能 |
| **PE-6** | AI-Infra-Guard 的 T01–T09 / promptfoo 插件 | **给注入装束挂公开分类学**：把 `pm/injection_pack.py` 已有 10 种装束映射到一套公开的技法分类（自建或对齐），让"覆盖缺口"可数 | PD-1 已把装束从 1 种扩到 10 种，但缺"覆盖了分类学里的哪几格、还缺哪几格"的可数口径；竞品都在用分类学说话 | 极小；零调用 |
| **PE-7** | —（措辞修正） | **改 README/`docs/evaluation.md` 的对外口径**：把"无人做评委自证"收窄成"别人量在固定基准上，我们量在你自己的锚点上并且敢报不可估" | TruLens 明写按人工标注给评委打分；GEPA 案例公开 κ 抬升。旧措辞已被部分证伪，照旧写会被一眼看穿 | 纯文档，且属于本轮**必须先做**的一条 |
| 🔭 | 监控清单更新 | ① linshenkx 评估层两周未做深（观察点不变，但要盯它是否把"评估"接成 MCP 工具）；② GEPA 是否把"无 ground truth"补上（正面冲击空位）；③ **新**：iFixAi / TruLens / AI-Infra-Guard 三家的"审计/标定"是否量化到能碰我们的 `calibrate`；④ **新**：hol-guard 的插件信任打分是否会吃掉"技能/插件门禁"这个位置 | 本轮三条新风险来源 | 监控 |
| ❌ | 维持不采纳 | DSPy 式程序编译、GEPA 式自动搜索（N13，理由仍是本仓评委判别力未收敛）、Vercel/CF 部署矩阵（Python 服务不适用） | 与第四轮一致；**但"技能包/插件"与"云部署"不同类，PE-1 不违反 PD-4 的否决** | — |

**若只做一件事**：PE-1。理由是它不改评分协议、不花钱、不产生任何效果主张，
而本周生态已经用星数证明"技能/插件"就是分发单位；本仓的 `check`/`diff`/`gate` 恰好是**零调用**能力，
最适合当技能的第一张脸 —— 现在缺的只是把它包起来。

## 六、诚实边界与取证说明

1. **本轮不是全量 API 实测**：匿名 REST 限额 60/h 在抓第 8 个仓库时耗尽（`data/rate-limit.json` 实测 `remaining: 0`），
   余下 6 家（phoenix / PromptWizard / textgrad / inspect_ai / prompt-ops / Kiln）改走 `/search` 的 `repo:` 限定符补数
   （同为 GitHub 官方接口，字段一致）；`Arize-ai/phoenix` 的搜索调用被 403 拦下，其 12,004★ 取自 `topic:llm-evaluation` 同日搜索结果。
2. **功能对照是文档级**：iFixAi/Ouroboros/TruLens/AI-Infra-Guard/hol-guard/prompt-architect/jev-align 的判断全部基于
   README 原文（存 `raw/*.md`），**没有跑它们的代码**；"它没有校准"这类否定判断是 grep 关键词的结论，不是穷举。
3. **两条未复核项**（转引自旧报告，本轮未独立验证）：promptfoo 被 OpenAI 收购的**交易细节**（本轮只核到 README 首行的官方声明）；
   DSPy「MIPRO 于 0.4.0 移除」。
4. **不产生效果结论**：本轮零代码改动、零真端点调用，不改任何 Δ/达标读数；PE-1~PE-6 全是"入口与口径"层的动作。
5. **仓库榜单的星数不可比时间窗**：GitHub Trending 周榜是"过去 7 天涨星速度"排序（第三方镜像），
   与 §一 的"绝对星数"不是同一把尺子，本报告分开列，未混算。