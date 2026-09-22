# PromptMaster Suite · 总索引（三服务 + 套件资产）

> 最后更新 2026-09-22 · 本文件是三服务套件的**唯一总入口**。接手方从这里出发，不需要问人。

## 一、三服务一览

| 服务 | 地址 | 健康检查 | 用途 | 详细文档 |
|---|---|---|---|---|
| **ModelHub** 统一模型池网关 | http://127.0.0.1:8687 | `/api/health` | 12 模型主备链自动切换、固定角色、虚拟密钥、统一 OpenAI 兼容接入 | `/handoff` 无 → 见 `docs/modelhub/`（验收报告、操作手册）与 `docs/suite/api-reference.md` |
| **龙虾站**（演示） | http://127.0.0.1:8791 | `/api/health` | 澳洲龙虾演示发布包：落地页/订单台账/海报 | `releases/lobster/docs/handoff.md`（或 `/ledger` 页面内链接） |
| **任务台账** | http://127.0.0.1:8792 | `/api/health` | 任务分配、状态流转、撤销、负载看板 | `/rules` `/exceptions` `/handoff` |

## 二、启动与守护

```bash
PY = D:\projects\prompt-master\.venv\Scripts\python.exe
# 单独启动任一服务：
%PY% run_modelhub.py --port 8687          # ModelHub
%PY% releases\lobster\app.py --port 8791  # 龙虾站
%PY% releases\taskboard\app.py --port 8792 # 任务台账

# 看门狗（推荐）：崩溃/缺失自动拉起
%PY% releases\suite\watchdog_suite.py --once     # 单次巡检+拉起
%PY% releases\suite\watchdog_suite.py            # 前台守护
%PY% releases\suite\watchdog_suite.py --install  # 注册计划任务（开机自启+5min 巡检）【已注册】
```

三服务当前健康：以 `/api/health` 实测为准；历史巡检证据 `releases/suite/monitor_log.jsonl`、`watchdog_log.jsonl`。

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
5. 出问题 → 先看 `releases/suite/watchdog_log.jsonl` 与各服务 `/v1/ledger`、`/api/orders`，再读对应 handoff 的故障排查节。

## 五、责任边界与已知边界

- 安全维度经用户指令豁免（2026-09-21），详见 `security-waiver.html`；公网暴露前建议收回豁免；
- 数据均为单机 JSON 落盘（原子写），上量前迁 SQLite（各 handoff 有指引）；
- 桌面 `D:\Desktop\新建 Text Document.txt` 含明文密钥，处置权在用户（W9 豁免中）。
