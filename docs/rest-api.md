# PromptMaster 文档：REST API 服务

> 2026-09-17 自 README 拆出。章节标题保留原编号，正文逐字未改；
> 本文覆盖原 README 的「四」（含容器化部署）。

## 四、REST API 服务（P2 新增）

`pm/server.py` + `pm/scheduler.py` 将优化闭环包装为异步 REST API，支持**批量赛马**。

| 端点 | 说明 |
|---|---|
| `GET /` | **Web 控制台**（单 HTML 页面：提交任务 / 实时状态 / 查看报告 / 批量赛马） |
| `POST /api/optimize` | 提交单个优化任务（异步执行），返回 `run_id` |
| `GET /api/status/{run_id}` | 查询任务状态（实时聚合分 / 迭代轮次 / LLM 调用数） |
| `GET /api/report/{run_id}` | 获取交付报告（Markdown） |
| `POST /api/race` | 批量赛马：一次提交 2-10 个任务并发执行 |
| `GET /api/race/{race_id}` | 赛马进度（完成数 / 各任务状态） |
| `GET /api/race/{race_id}/report` | 赛马横向对比报告（评分总览表 + 各任务完整报告） |
| `GET /api/health` | 健康检查 |

启动：`python run_server.py [--host 127.0.0.1] [--port 8080]`，交互式文档在 `http://localhost:8080/docs`。

安全与部署约束（默认值已按“最小暴露”设定，不要随手改宽）：
- 默认只监听 `127.0.0.1`；要放到局域网请显式 `--host 0.0.0.0`，并**先**设 `PM_API_TOKEN`。
  本服务会烧 API 余额，旧版的 `CORS:*` + 无鉴权 + `0.0.0.0` 组合等于让任意网页能跳板提交任务。
- `--workers > 1` 需要**共享任务记录**：设 `PM_TASK_DB=<sqlite 路径>` 后各 worker
  读写同一份记录，可放心多开；不设则任务记录在进程内，多 worker 时
  `status`/`report` 会随机 404 —— 这种情况脚本会强制回退为 1 个 worker 并告警。
  （线程池仍是进程内的：**谁接到 POST 谁执行**，查询可以打给任意 worker。）
  注意本地结果缓存在多 worker 下各持一份，命中率会下降，但不会给出错误结论。
- `POST /api/optimize` 与 `POST /api/race` 会同时把 `logs/report_<run_id>.md` 与
  `logs/run_<run_id>.json` 落盘，所以**服务重启后报告仍可取回**（旧版只存内存）。
- 所有响应带 `X-Content-Type-Options: nosniff` / `X-Frame-Options: DENY` /
  `Referrer-Policy: no-referrer` / `CORP: same-origin`；控制台页额外带
  `Content-Security-Policy: default-src 'self'`，脚本与样式用**逐响应 nonce**
  （不给 `'unsafe-inline'`）。控制台是零外部依赖的单页，所以这条策略能收到全效：
  报告里被塞进来的外联资源、内联脚本、内联事件属性、`style` 属性全都会被浏览器拦掉。
  代价是控制台自身也不能用内联事件属性与 style 属性 —— 已改成 data-* + 事件委托
  与工具类（`pm/web.py`），`tests/test_web_console.py` 盯着这条约束不许回退。
  `/docs`、`/redoc`、`/openapi.json` 走 CDN，唯一豁免 CSP —— 加了会白屏，其余头照给。
- 报告正文包含**任务原文与模型输出**，控制台渲染前一律转义
  （`pm/web.py` 的 `md2html`）；历史上这里曾为“让代码块里的 `<` 显示出来”
  做反向还原，等于给提交任务的人开了存储型 XSS 口子，`tests/test_web_console.py`
  从源码与行为两层把它钉住了。

环境变量：
- `PM_API_TOKEN` —— 设定后写端点需带 `X-API-Key: <token>`（读端点不要求）
- `PM_ALLOW_ORIGINS` —— 确实需要跳源调用时才设
- `PM_TASK_DB` —— 任务/赛马记录的 SQLite 路径；设了才能 `--workers > 1`
- `PM_FAKE_BACKEND=progress|stall|dispute|unclear` —— 演示模式，无 API Key 也能跑通全流程；
  它只作用于当前任务上下文（ContextVar），不会把假后端残留到整个进程
- 其余复用 `.env` 的模型/评委/并发/缓存配置

### 容器化部署

```bash
docker build -t prompt-master .
docker run --rm -p 8080:8080 -v "$PWD/data:/data" \
  -e PM_API_KEY=sk-xxx -e PM_API_TOKEN=change-me prompt-master

# 多 worker：镜像里 PM_TASK_DB 已指向 /data/tasks.db，各 worker 共享同一份记录
docker run --rm -p 8080:8080 -v "$PWD/data:/data" \
  -e PM_API_KEY=sk-xxx -e PM_API_TOKEN=change-me \
  prompt-master python run_server.py --host 0.0.0.0 --workers 4
```

镜像只装运行时依赖（走 `pip install .`，不会把 pytest/ruff/mypy 搬进去），
以非 root 用户运行，`/data` 是记录 + 报告 + 缓存的挂载点，健康检查打 `/api/health`（零成本）。
**映射到宿主机时务必设 `PM_API_TOKEN`** —— 这个服务能烧 API 余额。

---
