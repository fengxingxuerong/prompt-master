# API 接口文档（三服务全端点 · v1.3.0）

> 生成日期 2026-09-22 · 覆盖 33 个路由（ModelHub 18 / 龙虾 7 / 任务台账 8；会审台端点见 `releases/triage/README.md`）
> 全部端点经实测核对；错误码为实测行为。鉴权说明：三服务默认本地开放；设
> `PMH_GATEWAY_TOKEN` / `LOBSTER_TOKEN` / `TASKBOARD_TOKEN` / `TRIAGE_TOKEN` 后，
> 受保护接口需 `X-API-Key` 头。注意会审台更严：设 `TRIAGE_TOKEN` 后除 `/api/health`
> 与页面 GET 外**全部**端点（含数据读）都要令牌（M3.1，2026-09-23）。

## 一、ModelHub（http://127.0.0.1:8687）

### 核心对话

#### POST /v1/chat/completions
OpenAI 兼容对话补全，带自动切换。
```json
// 请求
{"model": "deepseek-v4-flash",            // 可省略 → priority 主备链
 "messages": [{"role":"user","content":"你好"}],
 "stream": false,                          // true = SSE
 "agent": "my-agent",                      // 可选，台账归属
 "role": "assistant",                      // 可选，固定角色注入
 "max_tokens": 4096, "temperature": 0.7}
// 200 响应
{"id":"chatcmpl-…","model":"deepseek-v4-flash",
 "choices":[{"message":{"role":"assistant","content":"…"},"finish_reason":"stop"}],
 "usage":{…},
 "modelhub":{"request_id":"…","failovers":0,"latency_ms":2100,
             "agent":"my-agent","role":"assistant","transient_retried":false}}
```
| 错误码 | 触发条件 | 实测 |
|---|---|---|
| 400 | stream=true 在 v1.0（现版本已支持流式，不再返回） | 历史 |
| 401 | 设了 PMH_GATEWAY_TOKEN 且令牌不符 | 实测 |
| 422 | role 不在 roles.json | e2e F 实测 |
| 502 | 池内全部模型失败（附尝试顺序） | 异常注入 X4/X5 |
| 503 | 配置缺失/损坏 | 异常注入 X1–X3 |

流式（stream=true）：`text/event-stream`，逐块 `data: {...}`，终帧 `data: [DONE]`；
上游拒流时自动降级合成分块并带 `X-Modelhub-Degraded: true` 头；真流式分支 15s 心跳注释行保活。

#### GET /v1/models
池清单：`{"data":[{"id":"deepseek-v4-flash","priority":1,"family":"deepseek","enabled":true}, …]}`（12 条）。

### 管理端点

| 端点 | 方法 | 入参 | 返回/错误 |
|---|---|---|---|
| /v1/keys | GET | — | `{"keys":[{key,agent,enabled,issued_at}]}` |
| /v1/keys | POST | `{"agent":"名","note":""}` | 201 `{"key":"vk-…"}`；422 agent 为空 |
| /v1/keys/{key} | PATCH | `{"enabled":false}` | 200 更新；404 不存在 |
| /v1/keys/{key} | DELETE | — | 200；404 |
| /v1/agents | GET/POST/DELETE | 同上模式 | 持久化（data/agents.json） |
| /v1/usage | GET | `?agent=&model=&since=&until=` | 聚合 + `persistent_daily`（按日落盘，重启保留） |
| /v1/metrics | GET | — | calls/success_rate/switches/circuits |
| /v1/pool/status | GET | — | 断路器状态、last_config_error |
| /v1/ledger | GET | `?model=&success=&type=&since=&until=&request_id=&limit≤2000` | 六维检索 |
| /v1/ledger/stats | GET | — | 总调用/成功率/切换数/按模型分布 |
| /v1/roles | GET | — | 10 角色清单 |
| /console | GET | — | 管理控制台（HTML） |
| /api/health | GET | — | `{"status":"ok","version":"1.1.0"}` |

## 二、龙虾站（http://127.0.0.1:8791，demo 口径）

| 端点 | 方法 | 入参 | 返回/错误 |
|---|---|---|---|
| /api/orders | POST | `{"name"(1-40),"phone"(1[3-9]\d{9}),"spec"(A-D),"qty"(1-20),"deliver_date"?,"note"?}` | 200 `{"order_id":"LOB-…","amount_cny":…}`；422 无效规格/手机号/数量 |
| /api/orders | GET | `?status=` | 200；422 无效状态；**phone 已脱敏（136\*\*\*\*6003）** |
| /api/orders/{oid} | PATCH | `{"status":…,"note":…}` | 200；404 订单不存在；422 无效状态 |
| / /ledger /poster | GET | — | HTML 页面 |
| /api/health | GET | — | ok |

状态机：pending→confirmed→shipped→delivered；缺货 out_of_stock / 退款 refunded / 死虾赔付 dead_compensation。

## 三、任务台账（http://127.0.0.1:8792）

| 端点 | 方法 | 入参 | 返回/错误 |
|---|---|---|---|
| /api/tasks | POST | `{"title"(2-80),"assignee","priority"(P1-3),"source"(必填),"due"?,"desc"?,"acceptance"?}` | 200 `{"task_id":"TASK-…"}`；422 无效负责人/优先级/指派到占位角色 |
| /api/tasks | GET | `?status=&assignee=&limit≤500&offset=` | 200 `{"count","returned","offset","limit","tasks":[…]}` |
| /api/tasks/{oid} | GET | — | 单任务含 history；404 不存在 |
| /api/tasks/{oid} | PATCH | `{"status"/"assignee"/"due"/"priority"/"title"/"desc"/"note"/"actor"}` | 200 `{"changes":"状态→…"}`；422/404；history 留痕 |
| /api/tasks/{oid}/undo | POST | — | 恢复最近快照 + 追加撤销记录；422 无可撤销 |
| /api/board | GET | — | 负载聚合 + overdue + due_sorted |
| /api/roster | GET | — | 三角色定义 |
| /api/health | GET | — | ok |

## 四、通用约定

- 内容类型一律 `application/json; charset=utf-8`；中间件自动容错无 charset 客户端的 Latin-1 mojibake（龙虾/任务台账 PATCH 已实测）。
- 时间戳格式 `YYYY-MM-DD HH:MM:SS`（本地时区）；日期 `YYYY-MM-DD`。
- 幂等性：POST /keys、/agents 幂等；PATCH 非幂等但全量留痕可 undo。
- 限流：无内置限流（安全豁免范围）；上游 429 由切换链兜底。
