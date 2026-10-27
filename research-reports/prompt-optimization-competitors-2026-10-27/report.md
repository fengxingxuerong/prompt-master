# 开源竞品对照 · 第六轮（2026-10-27，24h 增量 + 交卷后的位置重估）

> 口径：所有 stars/push/license 为 **2026-10-27 19:2x GitHub REST 实测**（`/repos/{o}/{r}` 21 个 + 7 组搜索，`data/` 存原始 JSON）。
> Δ 基线 = **第五轮（2026-10-26）** 的当日读数（见 `research-reports/prompt-optimization-competitors-2026-10-26/report.md`）。
> 功能判断仍只到 README/文件树级，**没有跑任何竞品代码**。
> 本轮特有的问题不是"谁更强"，而是：**我们刚交的三件事（技能包 / 三态裁决 / 澄清未果披露），
> 在昨天之后的市场里有没有对等物，以及为什么我们的星数是 0。**

## 一、24h 坐标：一条刺眼的数据

| 仓库 | 10-26 | **10-27** | Δ24h | 最近 push | 备注 |
|---|---|---|---|---|---|
| linshenkx/prompt-optimizer | 39,241 | **39,402** | +161 | 10-26 | 仍是最快的直接竞品 |
| stanfordnlp/dspy | 38,790 | **38,816** | +26 | 10-26 | 放缓 |
| langfuse/langfuse | 36,486 | **36,583** | +97 | 10-27 | 仍自称 "agent evals" |
| **ifixai-ai/iFixAi** | 34,502 | **35,351** | **+849** | **10-24** | **涨幅第一，且三天没提交** |
| promptfoo/promptfoo | 26,429 | **26,488** | +59 | 10-26 | OpenAI 旗下，MIT |
| comet-ml/opik | 22,925 | **22,960** | +35 | 10-27 | |
| openai/evals | 19,654 | **19,660** | +6 | 04-14 | 停滞 |
| confident-ai/deepeval | 19,104 | **19,135** | +31 | 10-23 | |
| Arize-ai/phoenix | 12,004 | **12,022** | +18 | 10-27 | |
| gepa-ai/gepa | 7,148 | **7,158** | +10 | 10-26 | 仍是"你给 evaluator" |
| Tencent/AI-Infra-Guard | 7,091 | **7,140** | +49 | 10-27 | 技能扫描仍在推 |
| Q00/ouroboros | 6,275 | **6,282** | +7 | 10-26 | |
| Kiln-AI/Kiln | 5,233 | **5,236** | +3 | 10-27 | |
| microsoft/PromptWizard | 4,022 | **4,022** | **0** | 2025-10-13 | 停摆整一年 |
| zou-group/textgrad | 3,770 | **3,776** | +6 | 2025-07-25 | 停摆 15 个月 |
| truera/trulens | 3,611 | **3,614** | +3 | 10-26 | 按人工标注给评委打分 |
| UKGovernmentBEIS/inspect_ai | 3,094 | **3,109** | +15 | 10-27 | 官方评测，无优化环 |
| meta-llama/prompt-ops | 1,027 | **1,027** | **0** | 04-21 | 死 |
| hashgraph-online/hol-guard | 996 | **1,000** | +4 | 10-27 | 插件信任分 |
| sutro-sh/jev-align | 311 | **311** | **0** | 10-03 | 停了 |
| **fengxingxuerong/prompt-master（本仓）** | 0 | **0** | **0** | **10-27（今天）** | 0 issue、0 fork |

三条读数：

1. **iFixAi 一天 +849，是第二名（linshenkx +161）的 5.3 倍**，而它最后一次提交是 **10-24**。
   ⇒ 这波涨幅**不来自代码，来自被看见**（1,720 fork / 415 watcher，典型 launch 扩散形态）。
   它是本赛道上唯一在"审计 / assurance"叙事里跑起来的项目。
2. **我们昨天到今天交了三轮**（`f861206` 技能包 + 插件清单 + Action、`6755d0b` 三态裁决、
   `a9eea72` 澄清未果披露），**星数仍是 0**。这不是"做错了"，而是这三件都属于
   「装得上 / 说得清」，**没有一件属于「被搜到」**。
3. 停摆区继续扩大（PromptWizard 整年 0 增长、prompt-ops 0、jev-align 停）——
   这条赛道的"自动优化"叙事确实在冷却，**还活着的是"审计/评测/技能"三种形态**。

## 二、昨天之后的新面孔：技能包还在加速，而且出现了"个人技能包"品类

`created:>2026-10-19` + `created:>2026-10-25` 两组搜索（`data/s1-*.json` / `s2-*.json`）里，同题相关的：

| 仓库 | stars | 建仓 | 一句话 |
|---|---|---|---|
| Jakeschincariol/day-trader-skill | **2,179** | 10-20 | 11 个 Claude 技能（昨天 1,709，**+470/天**） |
| **t3dotgg/tstack** | **1,766** | **10-24** | 「The agent skills I use every day」（知名开发者，3 天） |
| morganlinton/BeautifulRust | **1,261** | 10-23 | 教 agent 写地道 Rust 的技能（昨天 844，**+417/天**） |
| DenisSergeevitch/ex-ussr-kinship-research | 920 | 10-25 | 「证据优先」家谱技能（昨天 552，+368） |
| ZiChuanLan/agent-purge | 541 | 10-25 | 跨 codex/claude/opencode **注入或替换 system prompt** |
| songjianyang/xss-audit | 494 | 10-24 | 14 层静态审计技能 |
| huaxialucaicaizi/ai-startup-skills | 366 | 10-22 | 7 步创业 Agent Skill 合集 |
| daniellockyer/dstack | 116 | 10-26 | 「The agent skills I use daily」 |

加上搜索到的技能聚合仓：`obra/superpowers` **305,875**、`mattpocock/skills` **304,475**（10-27 还在推）、
`anthropics/skills` **184,121**、`VoltAgent/awesome-agent-skills` **36,435**、
`VoltAgent/awesome-openclaw-skills` **53,372**（5,400+ 技能）、`ComposioHQ/awesome-claude-skills` **78,051**。

**结论**：技能包已经是标准交付单位，并且出现了**目录层**（3.6 万～7.8 万星）。
我们的 `skills/prompt-master/SKILL.md` 已经就位，但**没有任何一个目录指向它** —— 这是可验证的结构性缺口（见 §四）。

## 三、位置重估：我们交的三件事，市场上有没有对等物

| 我们昨天交的 | 竞品里有对等物吗 | 判断依据 |
|---|---|---|
| **PE-1 技能包 + 插件清单 + Action** | **有，而且比我们早** | 技能包已成品类（§二）；iFixAi 早就同时发 plugin + skill 两条安装路径 |
| **PE-2 三态裁决（`confirmed`/`needs_validation`/`rejected`）** | **没有** | `llm-as-a-judge` 搜索（`s6`，305 个仓）前 12 里，除 langfuse/opik/deepeval/gepa 外无新玩家；**没有任何一家把"没核验成"做成 JSON 结构字段** |
| **PE-3 澄清未果披露（带着推断开跑要记账）** | **没有** | iFixAi 的 audit 是审计**agent 行为**（越权/欺骗），不是审计**"需求前提有没有被确认"**；ouroboros 有歧义门（≤0.2 才开工）但门槛在"要不要开始"，不在"带着假设跑完并披露" |

所以：**能力侧的空位还在，而且这两条（三态裁决 / 前提未验证披露）经本轮复核仍无人占据。**
但同一张表也说明：**这两条都不可见** —— 它们只在 `--json` 与报告正文里，
而 0★ 的仓库没有读者。

## 四、真正的瓶颈：不是能力，是发现性（三条可验证的证据）

1. **0★ / 0 issue / 0 fork**（本轮实测）—— 我们交的东西没有进入任何外部入口。
2. **外部可见的三个文档（README 中/英、SKILL.md）里 0 张图**（实测：无 `![`、无 `<img>`、无 `.gif`）。
   而本周破千星的技能仓（tstack / BeautifulRust / day-trader-skill）全部带演示资产或强个人品牌。
3. **iFixAi +849 且三天无提交** —— 同赛道里涨幅最大的一天，来源是曝光而非代码。
   ⇒ **在同一赛道，"被看见"目前比"多做一件能力"回报高一个量级。**

反面数据也要放上：`affaan-m/ECC` **290,316** 星，是"agent harness 优化系统"（311 skills + 71 agents）；
`obra/superpowers` 305,875、`mattpocock/skills` 304,475 —— **这个量级不是"质量更好"，是"叙事更宽 + 分发更早"**。
我们不可能在这个量级竞争，但**在一个具体目录/榜单里被找到**是可以做的。

## 五、靠齐方向（PF = round-6 新增；PE-4 优先级上调）

| # | 方向 | 证据 | 谁来做 | 成本 |
|---|---|---|---|---|
| **PF-1a** | **给外部文档配演示资产**：一条 `run.py check` 的真实输出截图 / asciinema cast 放在 README 顶部与 SKILL.md 里（**零调用、零出网**，正好是我们最强的展示面） | §四·2：三个对外文档 0 张图；本周破千星技能仓都带演示 | 我（本地可做） | 低；不需要 Key |
| **PF-1b** | **进目录**：向 `VoltAgent/awesome-agent-skills`(36k)、`ComposioHQ/awesome-claude-skills`(78k)、`awesome-openclaw-skills`(53k)、skills.sh 索引各提一次收录 | §二：目录层 3.6 万～7.8 万星，是本周新技能的曝光入口 | **需要你的 GitHub 账号**（提 PR / 提交） | 低，但对外动作 |
| **PF-2** | **把零调用那条线改叫「audit」叙事**：`check`/`gate` 在 README 与 SKILL 顶部讲成 *independent prompt audit*（对齐 iFixAi 已经验证过有市场的词），并保留我们独有的两条（三态裁决 / 澄清未果披露）作为差异点 | §一：iFixAi 的 "Independent Auditing" 是唯一跑起来的叙事；我们的能力语义上就是审计 | 我（改文档措辞 + 样例） | 零调用 |
| **PF-3** | **把 PE-4（锚点补料：不确定性采样 + 固定随机审计切片）提到下一步** | §三：三态裁决/前提披露无人对等，但支撑"评委可信"的读数仍是「不可采信」（AUC CI 覆盖 0.5）——**没有这条，我们唯一的护城河无法公开一次可信读数** | 我 | 中（人工标注是外部成本） |
| **PF-4** | 监控：iFixAi（一天 +849 的叙事竞品）、tstack / dstack（"个人技能包"品类）、`agent-purge`（跨 agent 改 system prompt，与"提示词作为交付物"相邻） | §一、§二 | 监控 | — |

**推荐顺序**：**PF-1a（当天可做，零成本）→ PF-2（改措辞 + 挂演示）→ PF-1b（要你的账号）→ PF-3（PE-4）**。
理由：PF-1a/PF-2 不需要任何外部依赖，且直接回答"为什么 0★"；PF-1b 是把前两步的成果送进流量的动作；
PF-3 是"被看见之后站得住"的那一步，但它有外部人工成本，不该排在曝光之前。

**明确不做**（沿用第五轮否决，本轮复核仍成立）：DSPy 式程序编译、GEPA 式自动搜索（N13）、
Vercel/CF 部署矩阵（PD-4）。本轮新增一条**不追**：不追 ECC / superpowers 那种"全栈 agent harness"叙事
（290k★ 那个量级由叙事宽度与时间累积决定，不是单点能力能补的）。

## 六、诚实边界

1. **24h 的 Δ 本身噪声很大**：+849 可能是某一天的爆款，明天回落也不奇怪；它证明的是"叙事+曝光有效"，
   **不证明** iFixAi 的产品比我们好。同理，我们的 +0 只说明"尚未被分发"，不说明能力缺陷。
2. **没有跑任何竞品代码**，功能对照仍是 README/文件树级；`llm-as-a-judge` 搜索结果只能证明"前 12 里没有新玩家"，
   **不能证明**"305 个仓里没有任何一家做三态裁决"（没做全量扫）。
3. **本仓记忆工具本轮不可用**（Hindsight 未配 API token，`knowledge-base/tree` 返回 401），
   所以位置判断完全基于仓库内这两份报告 + 本轮实测，没有引用任何记忆页。
4. 星数不能当质量：`affaan-m/ECC` 290k 与 `openai/evals` 19.6k（停更半年）在同一个榜上 —— 本报告只把星数当
   **注意力指标**用，所有"谁更强"的判断都另附了功能依据。
5. 本轮**不改代码**，只出方向；PF-1a/PF-2 落地时才会动 README/SKILL。