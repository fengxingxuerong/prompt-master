# 部署与操作手册（ModelHub v1）

> 版本 1.0 · 2026-09-21 · 项目：D:\projects\prompt-master（PromptMaster + ModelHub）

## 1. 系统组成

| 组件 | 位置 | 作用 |
|---|---|---|
| 模型池核心 | `pm/modelhub/pool.py` | 12 模型主备链、断路器、自动切换 |
| 台账 | `pm/modelhub/ledger.py` → `logs/modelhub_ledger.jsonl` | 每次调用/切换留痕，可检索 |
| 角色与智能体 | `pm/modelhub/agents.py` + `config/roles.json` | 角色固定、智能体注册 |
| OpenAI 兼容网关 | `pm/modelhub/server.py` + `run_modelhub.py` | 统一接入入口（默认 8687） |
| CLI | `modelhub_cli.py` | ping/status/ledger/chat/backup/rollback |
| 模型池配置 | `config/modelhub.json` | 12 模型、优先级、密钥引用（`${PM_API_KEY_*}`） |
| 密钥来源 | `.env`（不入库） | `PM_API_KEY_1`（商汤）、`PM_API_KEY_AMD` |

架构一句话：智能体 → (OpenAI 兼容) ModelHub 网关 → 按 priority 主备链逐个尝试
商汤 SenseNova / AMD Radeon 端点 → 成功即返回，失败自动切换下一个；全程写台账。

## 2. 启动 / 停止

```bash
cd D:\projects\prompt-master
# 启动网关（默认 127.0.0.1:8687）
.venv\Scripts\python.exe run_modelhub.py --port 8687
# 停止：Ctrl+C 或结束进程；健康检查：
curl http://127.0.0.1:8687/api/health
```

依赖已随项目 venv 安装（fastapi/uvicorn/pydantic/python-dotenv；无需新增依赖）。

## 3. 配置变更

- `config/modelhub.json`：改优先级（priority）、增删模型、改超时（`timeout_seconds`）、
  临时禁用（`enabled:false`）。**保存即生效**（网关按 mtime 热重载，无需重启）。
- `config/roles.json`：角色系统提示词。同样热重载；新角色保存后立即可用。
- `.env`：密钥与网关参数（`PMH_TIMEOUT` 单请求超时默认 90s、
  `PMH_BREAK_THRESHOLD` 连败熔断阈值默认 3、`PMH_COOLDOWN_SECONDS` 冷却默认 300s、
  `PMH_GATEWAY_TOKEN` 网关鉴权令牌）。改 `.env` 后需重启网关。

## 4. 日常运维（CLI 速查）

```bash
.venv\Scripts\python.exe modelhub_cli.py ping     # 12 模型逐个真实调用，连通性矩阵
.venv\Scripts\python.exe modelhub_cli.py status   # 断路器/冷却状态
.venv\Scripts\python.exe modelhub_cli.py chat "问题" --agent ops --role assistant
.venv\Scripts\python.exe modelhub_cli.py stats    # 台账汇总
.venv\Scripts\python.exe modelhub_cli.py ledger --success 0 --limit 50   # 查失败调用
.venv\Scripts\python.exe modelhub_cli.py ledger --type switch --limit 50 # 查切换事件
```

台账文件：`logs/modelhub_ledger.jsonl`（JSONL，一行一事件；可直接用文本工具/Excel 分析）。

## 5. 备份与回滚（已演练验证）

```bash
# 改配置前先备份（备份 config/modelhub.json、config/roles.json、.env）
.venv\Scripts\python.exe modelhub_cli.py backup
# 出问题一键回滚到最近一次备份（也可指定备份目录）
.venv\Scripts\python.exe modelhub_cli.py rollback
.venv\Scripts\python.exe modelhub_cli.py rollback D:\projects\prompt-master\backups\modelhub-20260921-114126
```

- 备份目录：`backups/modelhub-<时间戳>/`（含 manifest.json）；
- 2026-09-21 已实测"备份 → 破坏配置 → 回滚"：SHA256 前后一致，网关热重载自动恢复 12 模型；
- 更早的项目级快照（改动前原始 .env/llm.py 等）：`backups/pre-modelhub-20260921-105501/`。

## 6. 故障排查

| 症状 | 定位 | 处置 |
|---|---|---|
| `/v1/models` 503 | 配置缺失/损坏/密钥环境变量未设 | 看返回 detail（精确到文件与变量名）；修复或 rollback |
| 对话 502 全池失败 | 上游全不可用或配额耗尽 | `status` 看断路器；`ledger --success 0` 看最后错误；稍等冷却自愈或回滚 |
| 某模型一直不走 | 连败进入冷却 | `status` 看 `cooling_remaining_s`；恢复后自动回主备链 |
| 422 未知角色 | role 名不在 roles.json | `GET /v1/roles` 对照；或在 roles.json 固定新角色 |
| 400 stream | v1 不支持流式 | 去掉 stream 或等 roadmap |
| 401 | 设了 PMH_GATEWAY_TOKEN 但请求没带 | 带 `Authorization: Bearer <令牌>` |

## 7. 与 PromptMaster 原有流程的关系

- 原有优化闭环（`run.py` / `run_server.py` / MCP）**完全不受影响**：仍然走 `pm/llm.py`
  的角色配置（PM_* 变量），本次未改动其任何一行逻辑；
- ModelHub 是**新增的统一接入层**：新智能体一律接 8687 网关；PromptMaster 内部
  流程后续如需接入模型池，把 `PM_BASE_URL=http://127.0.0.1:8687/v1` 即可切换（可选，
  非本次改动）。

## 8. 安全须知

- `.env` 含真实密钥，已被 `.gitignore` 排除，绝不入库、绝不外发；
- `config/modelhub.json` 只存 `${ENV}` 占位符引用，可安全提交；
- 局域网/公网暴露时务必设 `PMH_GATEWAY_TOKEN`（并自行加反向代理 + HTTPS，属上线运维另列事项）。
