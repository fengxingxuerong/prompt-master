# 套件（三服务）总入口 —— ModelHub / 龙虾站 / 任务台账

> 本文件从仓库根 README 拆出（2026-09-25）。**根 README 讲的是本产品
> （PromptMaster：给 Agent 用的提示词质检与优化闭环）**；这里是随仓发布的
> 三个演示/基础设施服务，接手这三样的人从这里出发，不需要问人。
>
> 产品自身的上手请看 `docs/agent-cli-guide.md`（CLI/子命令）与 `docs/rest-api.md`（HTTP）。

## 一、三服务一览

| 服务 | 地址 | 健康检查 | 用途 | 详细文档 |
|---|---|---|---|---|
| **ModelHub** 统一模型池网关 | http://127.0.0.1:8687 | `/api/health` | 12 模型主备链自动切换、固定角色、虚拟密钥、统一 OpenAI 兼容接入 | `docs/modelhub/`（验收报告、操作手册）与 `docs/suite/api-reference.md` |
| **龙虾站**（演示） | http://127.0.0.1:8791 | `/api/health` | 澳洲龙虾演示发布包：落地页/订单台账/海报 | `releases/lobster/docs/handoff.md`（或 `/ledger` 页面内链接） |
| **任务台账** | http://127.0.0.1:8792 | `/api/health` | 任务分配、状态流转、撤销、负载看板 | `/rules` `/exceptions` `/handoff` |

## 二、启动与守护

```bash
PY = D:\projects\prompt-master\.venv\Scripts\python.exe
# 单独启动任一服务：
%PY% run_modelhub.py --port 8687          # ModelHub
%PY% releases\lobster\app.py --port 8791  # 龙虾站
%PY% releases\taskboard\app.py --port 8792 # 任务台账

# 看门狗（按需）：崩溃/缺失自动拉起
%PY% releases\suite\watchdog_suite.py --once     # 单次巡检+拉起（手动触发）
%PY% releases\suite\watchdog_suite.py            # 前台守护
# 注意：--install 已停用（2026-10-01）。本项目不注册计划任务，不做后台自动拉起，
#       只有上面两条手动命令会触发巡检；历史任务可用 --uninstall 清理。
```

三服务当前健康：以 `/api/health` 实测为准；历史巡检证据 `releases/suite/monitor_log.jsonl`、
`watchdog_log.jsonl`（⚠️ 这两个 jsonl 被 git 跟踪，任何一次巡检都会让工作树变脏 ——
`git status` 里它们常驻，别把自己的改动误归因给它们，也别把它们的内容当"当前状态"）。

鉴权（M3.3，2026-09-23）：默认本地全开放；在 `.env` 写 `TASKBOARD_TOKEN` / `LOBSTER_TOKEN` /
`TRIAGE_TOKEN`（网关另有 `PMH_GATEWAY_TOKEN`）后，受保护接口需 `X-API-Key` 头。看门狗会把
`.env` 的 KEY=VALUE 透传给拉起的服务进程（schtasks 极简环境下同样生效）。
口径详见 `docs/suite/api-reference.md` 头注与会审台 README。

## 三、报告与台账索引（按主题）

| 主题 | 文件 |
|---|---|
| 验收与评分 | `docs/modelhub/acceptance-report.html`（60/60）、`docs/final-test/final-acceptance-report.html`（28/28）、`docs/iteration/scoring-report.html` |
| 竞品对标 | `docs/iteration/competitor-analysis.md` + 优点落地映射 `optimization-mapping.md` |
| 缺陷台账 | `docs/release/defect-ledger.md`（D 系列）、套件 T 系列（CHANGELOG-v1.2.0.md §04） |
| 基线与复测 | `releases/suite/five_dim_baseline.json` / `five_dim_retest2.json` / `five_dim_v13b.json` |
| 完成度盘点 | `releases/suite/completion-audit.html` + `completion-ledger.md` |
| 现状评估 | `releases/suite/health-assessment.html`（4.2/5 评分卡） |
| 缺点分析 | `releases/suite/weakness-report.html`（17 条，含豁免登记） |
| 安全豁免 | `releases/suite/security-waiver.html`（W7/W8/W9 经用户指令豁免，2026-09-21） |

## 四、快速上手（新成员 10 分钟）

1. 读本文档 §1 §2，确认三服务健康；
2. 任务台账 http://127.0.0.1:8792 → 读 `/rules` `/handoff` → 建/领任务；
3. 接 ModelHub → 读 `docs/suite/api-reference.md` §一，POST /v1/chat/completions 试一发；
4. 需要改 UI → 读 `docs/suite/design-tokens.md`（三服务视觉契约）；
5. 出问题 → 先看 `releases/suite/watchdog_log.jsonl` 与各服务 `/v1/ledger`、`/api/orders`，
   再读对应 handoff 的故障排查节。

## 五、责任边界与已知边界

- 安全维度经用户指令豁免（2026-09-21），详见 `security-waiver.html`；公网暴露前建议收回豁免；
- **存储口径以代码为准**：三服务（taskboard / lobster / triage）自 v1.4.6 起真值源是
  **SQLite（WAL）**，同目录 JSON 只是只读种子快照与导出物。旧 README 写的"数据均为单机
  JSON 落盘（原子写）"已过期，别照它去回滚（回滚走 git tag，见
  `releases/suite/CHANGELOG-v1.4.6.md`）；
- 桌面 `D:\Desktop\新建 Text Document.txt` 含明文密钥，处置权在用户（W9 豁免中）；
- ModelHub 网关的流式通道（`stream=true`）在 2026-09-25 之前**从未通过**：
  `pm/modelhub/server.py` 这个文件是 54d6f51（v1.4.0）建立的，当时就把
  `_stream_response()` 尾部的 `return StreamingResponse(sse_gen(), ...)` 丢了
  （旧版代码在 `releases/v1.1.0/` 快照里是完整的）—— 不是"后来某次重构改坏"，
  是搬代码时漏了尾巴。当时无任何自动测试覆盖 SSE，所以跨三个版本没人发现。
  回归与修复见 `tests/test_modelhub_stream_contract.py`；同轮把 modelhub 覆盖率
  从 33.5% 补到 58.6% 并加了 CI 分项线。**对外承诺过的能力，优先补的是测试而不是文档。**
