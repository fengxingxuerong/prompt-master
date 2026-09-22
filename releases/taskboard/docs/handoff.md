# 交接说明：如何使用任务分配系统

> 5 分钟上手 · 服务地址 http://127.0.0.1:8792 · 数据：data/tasks.json（JSON 落盘，重启不丢）

## 1. 启动 / 停止

```bash
cd <部署目录>\taskboard
python app.py --port 8792     # 依赖 fastapi/uvicorn（随 prompt-master venv）
# 停止：Ctrl+C 或结束进程
```

## 2. 新成员 5 分钟实操（建任务 → 更新 → 查台账）

1. 打开看板 `http://127.0.0.1:8792/`；
2. 「新建任务」：填标题、选负责人、填**来源**（必填）、优先级默认 P2 → 创建并指派；
3. 在「截止日排序」表找到刚才的任务 → 点「开工」（状态变进行中）→ 干活 → 点「提复核」；
4. 用户（或验收人）确认后点「通过」（done 终态）；
5. 全部记录在任务 history：谁、何时、做了什么、改了什么。

CLI 等价方式：
```bash
curl http://127.0.0.1:8792/api/tasks                       # 台账
curl -X POST http://127.0.0.1:8792/api/tasks -H "Content-Type: application/json; charset=utf-8" \
  -d '{"title":"示例","assignee":"Agent-AutoClaw","priority":"P2","source":"口头指示 2026-09-21"}'
curl -X PATCH http://127.0.0.1:8792/api/tasks/TASK-xxx -H "Content-Type: application/json; charset=utf-8" \
  -d '{"status":"in_progress","actor":"你的名字"}'
```

## 3. 日常维护

- 每天：看板负载总览（逾期标红、P1 数量）；
- 每周：`data/backups/` 手动备份一次台账（`copy data\tasks.json data\backups\tasks-<日期>.json`）；
- 每条 done 任务抽查一条 history 完整性（动作 + 时间 + actor）。

## 4. 修改与扩展

| 要改什么 | 改哪里 |
|---|---|
| 花名册（加真人） | app.py `ROSTER` 列表（同步改 VALID_ASSIGNEES 由其派生） |
| 优先级默认天数 | app.py `_default_due()` |
| 状态机 | app.py `STATUSES` + 看板 board.html 的 ops 映射 |
| 主题样式 | static/board.html 的 `:root` 变量（Fathom 风格变量已就位） |

## 5. 已知边界

- 无登录鉴权：本地/内网可信环境使用；公网前加令牌（参考 ModelHub PMH_GATEWAY_TOKEN 模式）；
- 台账无分页：任务超 500 条时建议加 limit（当前量级无影响）；
- 单机部署：多机共享需迁 SQLite/PostgreSQL（同龙虾站建议）。
