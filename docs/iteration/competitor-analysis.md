# 竞品分析报告（ModelHub 对标）

> 调研日期：2026-09-21 · 方法：联网搜索公开资料（官网/文档/GitHub/评测），仅引用实际检索到的来源，未获取项标"—"
> 被分析对象：ModelHub v1.0.0（D:\projects\prompt-master，统一模型池网关）

## 1. 竞品对比矩阵

| 维度 | ModelHub v1.0.0（本项目） | LiteLLM Proxy | One API / New API | Portkey Gateway |
|---|---|---|---|---|
| 定位 | 本地单机统一模型池网关（商汤池专用优化） | 开源 AI 网关 + Router（Python/FastAPI） | 开源 LLM API 管理分发系统（Go，带管理后台） | AI 网关（开源核心 + 云服务） |
| 故障切换/重试 | ✅ 主备链切换 + 熔断冷却自愈 + 瞬时错误原地重试 | ✅ Router 级 retries/fallbacks，多 deployment 换模型/换 Key/换 base | ✅ 多渠道负载均衡，渠道禁用重试 | ✅ fallback 配置化路由 |
| 流式 SSE | ❌ v1 不支持（400 显式报错） | ✅ | ✅ 明确支持 stream 模式 | ✅ |
| 密钥管理 | ⚠️ 仅网关级可选令牌 | ✅ 虚拟密钥（virtual keys）+ 按 Key 限额 | ✅ 令牌/用户体系 | ✅ virtual keys |
| 用量/成本统计 | ⚠️ 台账有调用与耗时，无按 Key/智能体汇总端点 | ✅ spend tracking、按用户成本归属 | ✅ 额度/用量后台 | ✅ 分析看板 |
| 管理界面 | ❌ 纯 API + CLI | ⚠️ 有 Admin UI（较简） | ✅ 完整管理后台（New API 主打） | ✅ 云端看板 |
| 缓存 | ❌ | ⚠️ | ❌（New API 部分有） | ✅ smart caching |
| 守卫/审计 | ⚠️ JSONL 台账 | ✅ logging 集成 | ⚠️ 日志 | ✅ guardrails + observability |
| 部署形态 | pip 依赖即跑（随项目 venv） | pip/Docker | Docker/二进制 | npx 一条命令 / 云 |
| 定价 | 开源自用 | OSS 免费（企业版另计） | OSS 免费 | OSS 免费 + 云收费 |
| 口碑/热度 | 项目内部组件 | "目前最流行方案"（One API 34.6k stars 语境下对比对象）；Retries/Fallbacks 文档完善 | One API 34.6k stars；New API"国内最活跃的开源 LLM 网关" | 1600+ 模型路由；企业就绪 |

**来源链接（实际检索到）**：
- LiteLLM 可靠性（Retries/Fallbacks）：https://docs.litellm.ai/
- LiteLLM Proxy 能力（virtual keys、spend tracking、rate limits、load balancing、fallbacks、logging）：https://futureagi.com/
- LiteLLM Router 定位：https://github.com/BerriAI/litellm
- One API 负载均衡 + stream 模式 + 管理扩展：https://github.com/songquanpeng/one-api
- One API 统一接口/密钥管理/负载均衡：https://cloud.tencent.com/
- One API 热度（34.6k stars）与选型对比：https://zhuanlan.zhihu.com/
- New API 定位（国内最活跃、完整管理后台）：https://www.cnblogs.com/
- Portkey 能力（observability、guardrails、governance、prompt management）：https://portkey.ai/
- Portkey 开源网关（1600+ 路由、virtual keys、smart caching）：https://github.com/Portkey-AI/gateway 与 https://docs.portkey.ai/
- 网关横向对比（架构/路由/缓存/自托管/定价）：https://api7.ai/ 、https://www.truefoundry.com/

## 2. 可借鉴优点清单（→ 优化项映射见 optimization-mapping.md）

| # | 竞品优点 | 来源 | 对 ModelHub 的适配价值 |
|---|---|---|---|
| A1 | 流式 SSE 转发（One API 明确支持、LiteLLM 支持） | one-api GitHub | 补齐最大 DX 短板（K-3），三类接入框架全兼容 |
| A2 | 虚拟密钥 + 按 Key 限额（LiteLLM virtual keys / Portkey） | futureagi.com、Portkey GitHub | 多智能体各自持独立密钥，可停用/限额，密钥不外泄主 Key |
| A3 | 用量与成本归属（LiteLLM spend tracking / per-user attribution） | futureagi.com、builder.aws.com | 台账已有数据，补按智能体/模型/密钥的汇总端点即可 |
| A4 | 完整管理后台（One API/New API 主打） | cnblogs.com | 轻量 Web 控制台（池状态/台账/密钥/对话测试）替代"裸 API" |
| A5 | 健康感知路由（LiteLLM cooldown per deployment / Statsig 短重试优先） | docs.litellm.ai、statsig.com | 熔断已有，补"同优先级内优先零失败模型"加权 |
| A6 | 配置化持久化（One API 渠道落库） | songquanpeng/one-api | 修 K-4：智能体注册落盘，重启不丢 |
| A7 | 指标端点（Kong/Portkey benchmark 惯例） | konghq.com | /v1/metrics 暴露调用量/成功率/切换次数，供外部探针 |

## 3. 不采纳项（明确理由）

| 竞品特性 | 不采纳理由 |
|---|---|
| 1600+ 模型接入面（Portkey） | 本项目定位商汤池专用统一层，池可配置扩展即可，广度无需求 |
| 语义缓存（Portkey smart caching） | 对话缓存有正确性风险（角色/上下文差异），台账显示上游延迟可接受 |
| 云服务/多租户治理 | 单机本地部署定位，无多租户需求；鉴权用虚拟密钥已够 |
| 完整多用户后台（One API 用户体系） | 单运维者场景，轻量控制台 + CLI 已覆盖；避免引入用户系统复杂度 |
| Guardrails 内容审查 | 网关层做内容审查越权，保留给上层智能体 |
