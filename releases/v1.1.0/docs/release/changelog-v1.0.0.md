# v1.0.0 变更清单（发布构建）

> 发布日期：2026-09-21 · 快照：releases/v1.0.0/（20 文件，SHA256 见 MANIFEST.txt）

## 新增（本次交付）

| 文件 | 说明 |
|---|---|
| pm/modelhub/pool.py | 模型池核心：12 模型主备链、断路器（连败熔断/冷却自愈）、D-009 瞬时错误原地重试、自动切换 |
| pm/modelhub/ledger.py | 调用与切换 JSONL 台账（六维检索 + 汇总统计，坏行容错） |
| pm/modelhub/agents.py | 固定角色加载/渲染 + 智能体注册（幂等） |
| pm/modelhub/server.py | OpenAI 兼容网关（/v1/models、/v1/chat/completions、/v1/ledger、/v1/agents 等 9 端点） |
| pm/modelhub/__init__.py | 包导出 |
| run_modelhub.py | 网关启动脚本（默认 8687） |
| modelhub_cli.py | ping/status/ledger/chat/stats/backup/rollback 七命令运维工具 |
| config/modelhub.json | 12 模型池配置（密钥走 ${ENV} 占位符，不明文入库） |
| config/roles.json | 10 个固定角色系统提示词 |
| config/*.example.json | 两份配置模板 |
| docs/modelhub/agent-integration.md | 《智能体接入说明》（三种框架代码示例 + 自查清单） |
| docs/modelhub/operations.md | 《部署与操作手册》（含回滚步骤） |
| docs/modelhub/acceptance-report.html | v1 验收测试报告（HTML） |
| docs/release/test-plan.md | 发布测试计划（含性能基线口径适配声明） |
| docs/release/defect-ledger.md | 缺陷跟踪台账（D-001~D-009） |
| tests_modelhub/e2e_test.py | 端到端验收 8 场景 |
| tests_modelhub/exception_test.py | 异常注入 6 场景 |
| tests_modelhub/release_test.py | 发布测试（单元 3 + 并发 1 + 性能 2） |

## 变更（存量文件）

| 文件 | 变更 | 影响 |
|---|---|---|
| pyproject.toml | packages 追加 "pm.modelhub"（1 行） | 修复 test_packaging；不影响运行时 |
| .env.example | 追加 ModelHub 配置段（模板，无真实密钥） | 文档性 |
| .env | 追加 PM_API_KEY_1 / PM_API_KEY_AMD / PMH_* 参数（本地文件，不入库） | 本地配置 |

## 未变更（明确声明）

- pm/llm.py 及全部原有优化闭环代码：**零改动**（PromptMaster 原有功能不受影响，实测其测试套件通过情况与改动前一致）；
- 原 .env 的 PM_* 角色配置：未动，仅追加。

## 缺陷处置汇总（详见 defect-ledger.md）

- S2 修复 3：D-001 CLI ROOT 路径、D-002 打包声明、D-005 GLM-5.3-Flash 快速超时
- S2 缓解 1：D-009 上游瞬时 401 原地重试（网关侧 4/4 实证扛过上游 401 突发）
- S3 修复 2：D-003 ping 输出语义、D-004 配置错误文案
- S4 记录 3：D-006 上游限流特性、D-007 agents 注册表进程内存、D-008 存量测试顺序依赖
