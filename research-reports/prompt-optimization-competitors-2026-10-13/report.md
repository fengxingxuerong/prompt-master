# 开源竞品对照更新（2026-10-13 实测）

> 口径：本文件所有 stars/license/最近 push 均为 **2026-10-13 当日 GitHub REST API 实测**（`/repos/{owner}/{repo}` 与 `/search/repositories`）；
> 功能描述取自各仓库 README/仓库文件树原文。Δ 基线 = 本仓 `research-reports/prompt-optimization-competitors-2026-09-24/report.md`（2026-09-24 当日 API 实测）。
> 标「（9-24 报告）」的陈述引自上一份报告，本轮未复核；标「（本轮实测）」的有当日证据。
> 未做运行时基准，功能对照为文档/文件树级测绘。

## 一、当日坐标与三周 Δ

| 仓库 | 2026-09-24 | **2026-10-13** | Δ | License | 最近 push | 定位 |
|---|---|---|---|---|---|---|
| stanfordnlp/dspy | 38,243 | **38,601** | +358 | MIT | 2026-10-12 | 编程式提示词/程序编译框架（标准答案） |
| linshenkx/prompt-optimizer | 35,562 | **37,735** | **+2,173** | **新增 LICENSE（AGPL-3.0，34.7KB）** | 2026-10-09 | 人用提示词工作台（直接形态竞品） |
| promptfoo/promptfoo | 25,409 | **25,937** | +528 | MIT | 2026-10-12 | 评测/红队 CLI（9-24 报告：已被 OpenAI 收购） |
| confident-ai/deepeval | 18,419 | **18,770** | +351 | Apache-2.0 | 2026-10-12 | LLM 评测框架 |
| openai/evals | 19,500 | **19,591** | +91 | NOASSERTION | 2026-04-14 | 评测登记册（停滞半年+） |
| **gepa-ai/gepa** | （9-24 报告未收录） | **6,955** | 新入榜 | MIT | 2026-10-09 | 反射式进化优化（"优化任何文本参数"） |
| comet-ml/opik | — | **22,530** | — | Apache-2.0 | 2026-10-12 | 观测+评测平台（邻近） |
| langfuse/langfuse | — | **35,701** | — | NOASSERTION | 2026-10-12 | 观测+标注+评测（邻近） |
| Arize-ai/phoenix | — | **11,808** | — | NOASSERTION | 2026-10-12 | 观测+评测（邻近） |
| microsoft/PromptWizard | 4,012 | **4,018** | +6 | MIT | 2025-10-13 | 任务感知优化框架（**停摆整一年**） |
| zou-group/textgrad | 3,744 | **3,758** | +14 | MIT | 2025-07-25 | 文本梯度（**停滞 ~15 个月**） |
| UKGovernmentBEIS/inspect_ai | — | **2,989** | — | MIT | 2026-10-12 | 官方严谨评测框架（无优化环） |
| meta-llama/prompt-ops | — | **1,037** | 新 | MIT | 2026-04-21 | Meta 的优化工具（含 PDO 对偶赌博机论文） |
| Kiln-AI/Kiln | — | **5,190** | 新 | NOASSERTION | 2026-10-11 | 桌面版评测+优化工作台 |

两个三周内发生的事实，直接命中 9-24 报告点名的风险项（「linshenkx 若补上 LICENSE 并加评估层，是唯一可能整体逼近的对手」）：

1. **linshenkx 补了 LICENSE**（AGPL-3.0）与 **评估层**（README「Analysis and Compare Evaluation」「evaluation-driven smart rewrite」，本轮实测）——"无 LICENSE + 无独立评测层"这两条旧判断**均已过期**。
2. **GEPA 是 9-24 报告的最大遗漏**：6,955★、MIT、活跃（10-09 push），且已进 DSPy 生态。README 自称 50+ 生产用例（Shopify/Databricks/Dropbox/OpenAI/MLflow/Comet），论文 arXiv:2507.19457，宣称「35x faster than RL」「90x cheaper」。

## 二、三类对手与 prompt-master 的位置

| 类别 | 代表 | 它们优化的目标 | 与 prompt-master 的核心差异 |
|---|---|---|---|
| A 程序化优化框架 | DSPy、GEPA、TextGrad、PromptWizard、prompt-ops | **用户自带的客观 metric**（准确率/F1/任务成功） | 它们有"外部真值"这一锚；prompt-master 面向**无客观 metric 的开放任务**，用评委替代真值——所以它必须自建"评委可信度"这一整层，这也是它独有也是它最大的风险面 |
| B 评测/红队 | promptfoo、DeepEval、OpenAI Evals、Inspect | 把"测"做好（断言/插件/数据集） | 它们**不产优化后的交付物**；prompt-master 把"测"作为环内环节并有交付物（提示词+报告+SKILL.md） |
| C 人用工作台 | linshenkx、Kiln、ChainForge | 人肉对比、版本管理、收藏沉淀 | 它们服务**人**；prompt-master 同时服务 **Agent**（退出码契约/`--json` 单对象/MCP 12 工具/长任务 submit+wait） |
| D 观测平台（邻近） | Langfuse、Opik、Phoenix | 生产流量的 trace/标注/评测 | 面向"上线之后"；prompt-master 面向"交付之前"，两者是互补而非竞品 |

**净定位没有变**（与 9-24 一致）：没有单一开源项目同时覆盖「生成→测试→评估→迭代→交付」全环，且**没有一个把"量具本身可信吗"当产品功能做**。

## 三、逐家优缺点（只挑与 prompt-master 直接可比的核心对手）

### 3.1 DSPy（38.6k★）——"没有对手"的对手

- **优点**：生态位最大（学术引用+社区+集成 MLflow/LangSmith 等）；`dspy.GEPA` 等优化器齐全；有大量公开 benchmark 数字；文档/教程/课程成体系。
- **缺点**：要求用户**先有可信 metric** 与标注数据，很多业务场景根本没有；API 动荡史（9-24 报告：MIPRO 于 0.4.0 移除）；学习曲线陡（"编程而非提示"）。
- **对照**：prompt-master 的 `--cases-file` 事实断言（一票否决）解决了"没有真值"里能拿到真值的那部分；剩下拿不到真值的部分，它选择承认（Δ 不可采信）而不是假装。**DSPy 不会告诉你它的优化目标不可信；prompt-master 的核心功能之一就是告诉你。**

### 3.2 GEPA（7.0k★，本轮新收录）

- **优点**：方法论清晰（读全轨迹的反射式进化 + Pareto 选择）；论文+生产案例+跨域（提示词/代码/调度策略）；对外部生态开放（DSPy 集成）。
- **缺点**：同样以"you can measure it"为前提；反射依赖 LLM 读长轨迹（成本另一头）；年轻项目，边界仍在变。
- **对照**：GEPA 与 prompt-master 是**两种范式的对标本**：GEPA 优化"有确定 metric 的系统"，prompt-master 处理"只有模糊标准的开放任务"并把这个模糊本身量化（AUC/κ/CI）。后者更难、更不成熟，但也更少有人做。

### 3.3 linshenkx/prompt-optimizer（37.7k★，直接形态竞品，三周 +2.2k）

- **优点**：三周涨 2.2k（增速超过 DSPy），人用体验完整（Web/插件/桌面/Docker/Vercel/CF 全家桶）；已有 e2e 测试目录与 Playwright 配置（本轮实测文件树）；补了许可证与评估层；有 MCP。
- **缺点（对照后仍成立的部分）**：评估是**UI 级"分析+对比"**，没有量化层（无锚点校准、无显著性、无噪声带、无 CI 门禁）；无事实断言（ground truth 一票否决）；无注入存活门禁；无预算总闸；面向人。
- **风险更新**：旧风险（无许可证）**已消除**——但它选了 **AGPL-3.0**（copyleft）：自部署自用没问题，**想把它嵌进闭源产品/商业服务会被传染**。这对 prompt-master 反而是逆向机会：若 prompt-master 未来开源，选宽松许可（MIT/Apache）即可吃到"想商用集成"的用户。

### 3.4 promptfoo（25.9k★）+ DeepEval（18.8k★）——"测"这一块的天花板

- **优点**：promptfoo 的红队插件生态（50+，注入/PII/jailbreak）与测试矩阵是 prompt-master 的注入用例库远不能比的；DeepEval 的指标/数据集工具链完整且 Apache-2.0。
- **缺点**：不产优化后交付物；评委阈值调优是"你来定"，没有"校准到人工锚点并量化偏差"的产品化流程（9-24 报告同判）。
- **对照**：prompt-master 的 `--cases-file` 断言体系是"自建小号 promptfoo"，广度必输；正确姿势是**抄它**：把 promptfoo 的插件清单作为注入用例库来源（见第五节）。
- **风险**：promptfoo 被 OpenAI 收购（9-24 报告），中立性/路线存在变数。

### 3.5 TextGrad / PromptWizard / OpenAI Evals——"停摆区"

- TextGrad：3.8k★，停更 ~15 个月；PromptWizard：4.0k★，停更整 1 年（push 恰为 2025-10-13）；OpenAI Evals：停更半年。
- **含义**：这个赛道的"自动优化"叙事冷却快，工具要么进入生态（DSPy/GEPA/DeepEval），要么停摆。**长期存活靠的是生态接入与人用价值，不是单点算法。**

### 3.6 Langfuse / Opik / Phoenix——"校准纪律"的邻居

- 它们把"golden set + 人工标注 + 评测"做成了平台流程（标注队列/数据集/回归），但**"评委分 vs 人工分的偏差量化"**（MAE/bias/AUC/κ + 可估性门槛）不是它们的头等功能——那是 prompt-master 唯一做到"把量具自己拿来量"的地方。
- 对照结论：prompt-master 的 `calibrate` 模块如果抽出来做成独立小工具，在 D 类产品里也有位置（补它们缺的那块），但当前形态耦合在闭环里。

## 四、对照后的净结论：prompt-master 的优缺点

**相对优势（开源圈对照后仍独特）**：
1. **量具自证链**：评委校准（锚点 MAE/bias/AUC/κ + 95% CI + 可估性门槛 + 跨轮分代）没有任何一个被检竞品有对等产品化实现。
2. **诚实性作为功能**：README 主动印「AUC 0.623 CI 覆盖 0.5、调阈值净增益 +0.000」——竞品 README 全是"best/better/90x"；它自己的负结论照发。
3. **Agent 一等公民**：退出码四态契约 + `--json` 单对象 stdout + submit/wait 长任务协议 + 12 个 MCP 工具 —— linshenkx 有 MCP 但主体仍面向人，DSPy 面向开发者写代码。
4. **零调用能力线**：`check`/`diff`/`gate`（规则体检、版本差分、git 基线增量门禁）——竞品里没有对标物。
5. **工程纪律**（本机实测口径）：1681 条测试 / mypy strict / 覆盖率地板 92%+ / 变异验证过的守卫；这在个人开源项目里属于极少见档位。

**短板（对照后会显得更刺眼的）**：
1. **生态 = 0**：无社区、无集成（Comet/MLflow/LangSmith）、无论文/benchmark 背书。GEPA 用"50+ 生产用例 + 论文"建立信誉；prompt-master 用"自己承认量具未收敛"建立信誉——后者诚实但传播力弱。
2. **核心量具仍未收敛**（项目自认）：评委 AUC CI 覆盖 0.5、owner 达标样本每格不足 10 —— 这意味着对**外部用户**，目前能放心用的只有零调用能力线（check/diff/gate）与资产库；Δ 数字类功能还在"实验室"状态。
3. **单人 + 单机的边界**：多进程是显式前提（README §五·3）；无云/多租户；bus factor = 1。
4. **注入防御广度**：只有自研注入存活判据，没有 promptfoo 那种插件库；对抗样本覆盖靠补。
5. **成本/时长**：自身实测一轮 4~5 小时 / 80+ 次调用（§十七 系列），对"随手优化一下"的场景太重。

## 五、可抄清单（按性价比排序）

| # | 抄谁 | 抄什么 | 动机（对照证据） |
|---|---|---|---|
| S1 | promptfoo | 红队插件清单 → 扩注入用例库 | 它是这块的天花板；prompt-master 已有注入存活门禁，缺的只是"考卷广度" |
| S2 | GEPA/DSPy | 反射式优化的轨迹利用方式（读失败轨迹而不只读分数） | revise 节点目前只吃评委 issues；GEPA 证明"读全轨迹"是廉价高收益 |
| S3 | Langfuse/Phoenix | golden set → 标注队列 → 回归评测的产品化流程 | prompt-master 的锚点补料正卡在"缺 owner 亲判样本"，标注队列是成熟解法 |
| S4 | linshenkx | 部署矩阵（Vercel/CF/Docker/桌面） | 它三周 +2.2k 的一半功劳在"随手就能跑起来" |

**跟踪建议**（延续 9-24 报告的风险追踪）：
- linshenkx：已从"无证照"变 AGPL；下一个观察点 = 它是否把评估层做深（量化/断言/门禁）。
- GEPA：作为方法论上游持续跟踪；若它补上"无 ground truth 场景"的解法，prompt-master 的核心空位会被正面冲击。
- promptfoo（OpenAI 旗下）的许可/路线变化。

## 来源

- 本轮实测：GitHub REST API（17 个仓库坐标 + 2 组关键词搜索，2026-10-13）+ 上述仓库 README/文件树原文。
- 基线：本仓 `research-reports/prompt-optimization-competitors-2026-09-24/report.md`（含其来源清单）。
- 未复核项：promptfoo 被 OpenAI 收购、DSPy「MIPRO 于 0.4.0 移除」——均转引自 9-24 报告，本轮未独立验证。
