# 多模型会审台（Triage Panel）

客服工单智能分诊实战案例：**工单输入 → 6 个 LLM 评委并行研判 → 共识汇总 → 人工反馈闭环**。
所有 LLM 调用经 ModelHub 网关（127.0.0.1:8687）真实发生，逐条落 JSONL 台账。

## 架构

```
工单(subject+body)
   │ POST /api/reviews
   ▼
Triage Panel (FastAPI, 127.0.0.1:8793)
   │ ThreadPoolExecutor 并行 6 评委
   ▼
ModelHub 网关 (8687) ──→ SenseNova / AMD / StepFun / NVIDIA 上游
   │
   ├─ majority-return: ≥3 有效票即汇总，迟到票 late_review 留痕
   ├─ 评委健康度: 连续 2 败 → unhealthy（超时放宽到 80s，不阻塞）
   ├─ JSON 容错: 剥 ```json 围栏 + 正则提取首个平衡 JSON
   └─ 模型白名单: 坏模型名 <0.1s 快速失败（v1.1.1）
   ▼
共识: 多数分类 + 组内最高严重度 + 置信均分 + 异议/失格留痕
   ▼
data/sessions.json (会话) + data/ledger.jsonl (调用台账) + data/reviewer_health.json (健康度)
```

## 从零启动

前置：Python 3.12 + 依赖 `fastapi uvicorn pydantic`；ModelHub 已运行（`python run_modelhub.py --port 8687`，配置见 `config/modelhub.json` 与 `.env`）。

```powershell
cd D:\projects\prompt-master
.venv\Scripts\python.exe releases\triage\app.py
# 就绪后访问 http://127.0.0.1:8793
```

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | / | 会审台 UI |
| GET | /api/health | 健康检查（版本/评委数/台账数/会话数） |
| POST | /api/reviews | 提交工单 `{subject, body, customer?}` → 同步返回会审结果 |
| GET | /api/sessions | 会话列表 |
| GET | /api/sessions/{sid} | 会话详情（评委逐票+共识） |
| POST | /api/sessions/{sid}/feedback | 人工反馈 `{rating 1-5, comment?}` |
| GET | /api/ledger?limit=50&session=SES-xxx | 调用台账（可按会话过滤） |
| GET | /api/reviewer_health | 评委健康度（streak/成败计数/unhealthy） |
| GET | /api/sessions/{sid}/events | SSE 实时进度（progress 逐票/done 终态；已完成会话订阅立即收 done） |
| GET | /api/usage-board | 用量成本数据（按日/按模型聚合，只读 usage_daily.json） |
| GET | /static/{name} | 静态页（index.html / cost-board.html） |

## 版本与回滚

| 版本 | 文件 | 说明 |
|---|---|---|
| v1.2.0（当前） | app.py | 异步提交 + SSE 实时进度（G-07/G-12）+ /api/usage-board |
| v1.1.1 | app_v1.1.1.py.bak | majority-return + 健康度 + JSON 容错 + 白名单 |
| v1.1 | app_v1.1.py.bak | 无白名单校验 |
| v1.0 | app_v1.0.py.bak | 朴素基线（全员等待、裸 json.loads） |

回滚：`copy releases\triage\app_v1.1.py.bak releases\triage\app.py` 后重启（杀 8793 进程 → 重新运行 app.py）。数据文件向后兼容，无需迁移。

## 已知风险

1. **上游依赖**：全部评委经 ModelHub，网关停则服务退化为全失败（共识返回"需人工介入"，不崩溃）。
2. **NVIDIA 慢源**：glm-5.3-flash 冷启动可达 120s+，健康度机制会让它转为 late 票，不影响主流程时效。
3. **OpenRouter 未启用**（欠费 402），启用需充值后改 modelhub.json 的 enabled。
4. **成本**：每单 6 次调用 × ~700 max_tokens；台账 usage 字段可审计实际 token 消耗。
5. **输出质量波动**：LLM 分类非确定性（temperature=0.2 缓解）；consensus.agreement < 0.5 时建议人工复核。
6. **并发**：v1.2.0 已异步化（提交即返回，后台会审），多用户不再排队；SSE 订阅不存在的会话会进入 keep-alive 等待（最长约 5 分钟），前端不会触发此路径。

## 修改指南

- 换评委/加评委：改 `REVIEWERS` 列表（model 必须在 ModelHub 池内）
- 调分类口径：改 `REVIEW_SYSTEM` 提示词与 `CATEGORIES`
- 提前汇总阈值：`MAJORITY = 3`
- 健康度判罚阈值：`UNHEALTHY_STREAK = 2`
- 台账位置：`data/ledger.jsonl`；会话：`data/sessions.json`
