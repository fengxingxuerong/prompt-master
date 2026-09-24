# 一页对位表：prompt-master vs 三类竞品（2026-09-24）

> 依据：research-reports/prompt-optimization-competitors-2026-09-24/report.md（GitHub API 当日实测 + 多源交叉验证）。
> 口径：功能对照为文档级测绘，未做运行时基准。

| 维度 | **prompt-master（本项目）** | **linshenkx/prompt-optimizer**（工作台，35.6k★） | **DSPy**（框架，38.2k★） + **promptfoo**（评测/红队，25.4k★）组合 |
|---|---|---|---|
| 形态 | 服务化工具：CLI + REST + MCP + Web 五入口 | 人用工作台：Web / Chrome 插件 / Docker | 开发者框架 SDK + CLI（DSPy 库 / promptfoo CLI） |
| 消费方 | **人与 Agent 都能用**（CLI 四态契约、MCP 可被 OpenClaw/Loomy 挂载） | 仅人（工作台 UI） | 仅开发者（写代码） |
| 优化引擎 | 图编排：clarify→optimize→revise，修订收敛「只改点名句」 | 一键改写 + 多轮迭代（单模型流） | 编译式优化（BootstrapFewShot 等；MIPRO 已移除） |
| 评估可信度 | **双评委（跨厂商）+ 仲裁 + 评委校准 + 跨 run 显著性** | 依赖用户肉眼对比输出 | 用户自带指标；LLM-as-judge 存在偏置（论文有记录） |
| 用例与测试 | 测试集首轮锁定、事实断言一票否决、含注入存活门禁 | 无独立测试集/断言体系 | DSPy 训练集；promptfoo 50+ 红队插件（注入/PII/jailbreak） |
| 注入防御 | ✅ 注入存活专项检测 + 复述校验码口径 | ❌ 无 | promptfoo 有红队扫描（prompt-master 缺它的插件库广度） |
| 成本控制 | **预算总闸 PM_MAX_LLM_CALLS + 结果缓存幂等 + dry-run 预估** | ❌ 无 | ❌ 无（社区普遍提醒成本失控） |
| 交付闭环 | 交付报告 + SKILL.md + 资产库沉淀 + 记忆注入 | 版本回溯 + 收藏沉淀 | 各自独立，无统一交付 |
| 部署 | 本机 SQLite 三服务套件 + 看门狗守护 + 鉴权机制 | 纯客户端/自部署 | SDK 集成 |
| License/生态 | 私有（MCP 已接 Loomy） | **无 LICENSE**（NOASSERTION，商用有法律风险） | MIT / MIT |
| 已知缺陷 | 无开箱 IM 推送；评委依赖外部端点稳定性 | 无独立评测层；迭代质量随模型波动 | TextGrad 式上下文膨胀隐患；DSPy API 动荡 |

## 结论（三点）

1. **最直接形态竞品 = linshenkx**：它赢在「人用 UI + 生态可见度（35.6k★）」；prompt-master 赢在「可信评估 + 注入防御 + 预算控制 + Agent 可编程」——空位是**「给 Agent 用的一站式提示词质检与优化服务」**，目前无人占。
2. **组合打法可借力**：promptfoo 的插件体系 → 扩充注入用例库广度；DSPy/PromptWizard 的示例引导 → 反哺 optimize/revise 节点。
3. **风险项**：命名与消费级 Claude Skill「prompt-master」撞车；linshenkx 若补上 LICENSE 并加评估层，是唯一可能整体逼近的对手——建议为其建立 star/版本追踪。
