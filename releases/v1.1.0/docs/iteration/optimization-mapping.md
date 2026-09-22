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
