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

## 2026-10-31：一次只读语义审计的结论（三服务）

三套服务此前**只被 CI 跑测试、从未做过语义审计**。审计方式：TestClient 进程内 + 临时数据目录
（不写 `releases/*/data/`），零出网。

### 已修（本轮）

| 缺陷 | 后果 | 修法 |
|---|---|---|
| `cost-board.html` 的 `startLive` 写在文档结束标签**之后** | 浏览器把它当散落文本 ⇒ `startLive()` 抛 ReferenceError 被 `.catch` 接住 ⇒ 整块成本看板变成"加载失败：startLive is not defined（需经会审台访问）"，归因还是错的。打开即 100% 复现 | 函数搬进同一段 `<script>`；加"文档结束标签之后不许有残留"的守卫 |
| 台账页 / 历史页 / 看板页把**用户可写字段**裸拼 `innerHTML` | `name`（公开 POST /api/orders）、`subject`、`feedback.comment`、任务 `title` 都是用户写入的 ⇒ `<img src=x onerror=alert(1)>` 原样落盘，运营者一开页就执行（存储型 XSS） | 三页各加 `esc()` 并逐个字段接上；守卫钉"裸插值形态" |
| `/api/notifications` 的 `count` 硬编码 0 | 实测 3 条告警时 `items=3` 而 `count=0` ⇒ 前端按 count 判空，通知中心永远"没有告警" | `count = len(items)` |

### 已知但**未修**（每条都实测过，按危害排序）

1. **跨进程丢数据**：`lobster/app.py` 与 `taskboard/app.py` 的读-改-写只靠**进程内**
   `threading.Lock`，而落盘是全量 `DELETE` + re-INSERT。实测两个模块实例各持一把锁
   （≈ 两个进程）并行新增 2 条，最后只剩 1 条，接口全回 200；taskboard 的 `seq` 也在锁内
   自增，两进程会算出同一个 `TASK-…-001` 被 `INSERT OR REPLACE` 静默合并。
   **修法有范式**：`triage/app.py` 已经是"同一事务内读改写、只 INSERT 新行"。
   没在本轮动是因为它要重写三套服务的存储路径，属于"改事实源"级别的决定。
2. **开 token 后三套页面取不到数**：页面 JS 从不发 `X-API-Key`（页面能取到 token 的途径
   本身就没有）。实测设 token 后 `/api/orders`、`/api/board`、`/api/sessions` 全 401，
   而页面把 401 画成"暂无订单 / 暂无数据"——**用空集冒充没数据**。
   正确修法是给页面一条拿令牌的途径（或让 401 明确说"需要令牌"），需要产品决定。
3. **watchdog / monitor 只看 HTTP 200**：把 `orders.db` / `sessions.db` 写成垃圾字节，
   服务仍回 200 + `status:"ok"`（只把 `storage` 记成 error），探针照样报"活着"；
   而带 token 的部署里探针自己又不带令牌 ⇒ 恒 401，超时告警静默不触发。
4. 其余小项：一致率按"有效票"算（1 票有效即 100%）、健康度与列表两套条数口径、
   撤销连按两次只回滚一步却每次报成功、`PATCH due` 绕过格式校验（逾期恒 0）、
   token 用 `!=` 而非 `compare_digest`、taskboard 前缀式放行（新增 GET 默认免鉴权）、
   一条缺 `ts` 的会话让 `/api/sessions` 500、`five_dim_audit` 固定一条误报、
   `rotate_ledger` 硬编码路径且异常被吞。

> 这三套是**发布套件**，不是本产品（见根 README「仓库结构」）。上面第 1、2 条已实测复现，
> 处置权在所有者：要么按 triage 的范式重写存储、要么在文档里明确"单进程部署"是前提。