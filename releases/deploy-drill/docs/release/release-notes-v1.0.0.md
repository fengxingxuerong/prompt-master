# ModelHub v1.0.0 发布说明

> 发布日期：2026-09-21 · 产物：统一模型池网关（OpenAI 兼容）

## 一句话

PromptMaster 新增统一模型池网关 ModelHub：12 个模型（商汤 5 + AMD 端点 7）组成
主备链，任一模型不可用自动切换；角色人设固定不随模型漂移；任意框架的智能体按
OpenAI 兼容接口零改造接入；每次调用与切换全部留台账。

## 核心能力

1. **自动切换**：超时/429/5xx/401/空返回自动切下一个模型；瞬时错误原地重试一次再切；连败 3 次熔断 300s 冷却自愈。
2. **固定角色**：10 个角色系统提示词集中管理（config/roles.json），热重载，与模型解耦。
3. **统一接入**：base_url 指向 127.0.0.1:8687/v1 即可；model 可省略（自动主备链）；agent/role 扩展字段做归属与人设。
4. **全链路台账**：logs/modelhub_ledger.jsonl 一行一事件，CLI 与 API 双路检索。
5. **运维齐备**：CLI 七命令（含 backup/rollback）、发布检查清单、已知问题清单。

## 快速开始

```bash
cd D:\projects\prompt-master
.venv\Scripts\python.exe run_modelhub.py --port 8687
# 验证
curl http://127.0.0.1:8687/api/health
curl http://127.0.0.1:8687/v1/models
```

智能体接入三要素：base_url=`http://127.0.0.1:8687/v1`、api_key=PMH_GATEWAY_TOKEN（未设则任意）、model 可省略。详见 docs/modelhub/agent-integration.md。

## 已知问题清单（发布时点）

| # | 问题 | 影响 | 缓解/计划 |
|---|---|---|---|
| K-1 | amd-glm-5.3-flash 持续读超时（上游问题） | 该模型不可用 | 已配 45s 快速超时 + 熔断自愈；主链路不受影响；上游恢复后自动回归 |
| K-2 | 商汤上游限流（429）且偶发 401 突发 | 密集调用时部分请求切换/重试 | 网关已带原地重试 + 切换双兜底；高并发场景自行控速 |
| K-3 | v1 不支持流式（stream=true 返回 400） | 需要流式的智能体暂不能接 | roadmap：v1.1 实现 SSE 转发 |
| K-4 | 智能体注册表仅存进程内存 | 网关重启后注册关系清零 | 角色配置不受影响；重新注册幂等；v1.1 持久化 |
| K-5 | pm 存量 test_measurement 2 用例全量跑时失败 | 与 ModelHub 无关的存量顺序依赖 | 已复现实验证明；不在本版本修复 |
| K-6 | 6.7-flash-lite / u1 系列在 chat 端点 404 | 未能入池参与调度 | 上游开放路由后在 config 加条目即可 |

## 升级与回滚

- 升级：本版本为首次发布（存量零改动，无升级路径问题）。
- 回滚：`modelhub_cli.py backup` 备份 → 出问题 `modelhub_cli.py rollback` 一键恢复；
  更彻底的回退 = 删除 pm/modelhub/ 与 config/、还原 pyproject.toml 一行（改动清单见 changelog），
  原始状态快照在 backups/pre-modelhub-20260921-105501/。

## 监控与维护

- 健康：GET /api/health（建议外部探针 60s 间隔）
- 池状态：GET /v1/pool/status（看熔断/冷却）；GET /v1/ledger/stats（成功率/切换次数）
- 台账巡检：`modelhub_cli.py ledger --success 0 --limit 50` 查最近失败
- 连通性巡检：`modelhub_cli.py ping`（每周或异常时）
