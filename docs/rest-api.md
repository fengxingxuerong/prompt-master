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
  与工具类（`pm/web/`），`tests/test_web_console.py` 盯着这条约束不许回退。
  `/docs`、`/redoc`、`/openapi.json` 走 CDN，唯一豁免 CSP —— 加了会白屏，其余头照给。
- 报告正文包含**任务原文与模型输出**，控制台渲染前一律转义
  （`pm/web/_js.py` 的 `md2html`）；历史上这里曾为“让代码块里的 `<` 显示出来”
  做反向还原，等于给提交任务的人开了存储型 XSS 口子，`tests/test_web_console.py`
  从源码与行为两层把它钉住了。

环境变量：
- `PM_API_TOKEN` —— 设定后写端点需带 `X-API-Key: <token>`（读端点不要求）
- `PM_ALLOW_ORIGINS` —— 确实需要跳源调用时才设
- `PM_TASK_DB` —— 任务/赛马记录的 SQLite 路径；设了才能 `--workers > 1`
- `PM_FAKE_BACKEND=progress|stall|dispute|unclear` —— 演示模式，无 API Key 也能跑通全流程；
  它只作用于当前任务上下文（ContextVar），不会把假后端残留到整个进程
- 其余复用 `.env` 的模型/评委/并发/缓存配置

### 请求 / 响应字段参考

> 这张表与 `pm/server.py` 的 pydantic 模型**同源比对**：
> `tests/test_api.py::test_rest_field_reference_matches_models` 会把这里的字段名集合
> 与模型字段逐一对照，改了模型忘了改文档（或反之）就红。
> 交互式文档在 `/docs`，但 Agent 消费时以此表为准（含约束与默认值语义）。

`OptimizeRequest` —— `POST /api/optimize` 请求体：

| 字段 | 必填 | 约束 | 默认 | 说明 |
|---|---|---|---|---|
| `task` | ✅ | 4–8000 字 | — | 原始需求描述 |
| `context` | | ≤8000 字 | `""` | 补充上下文（领域、口径、已有约定） |
| `target_model` | | ≤200 字 | `"未指定"` | 提示词最终运行的模型；命中家族档案时按档案写法 |
| `n_test_cases` | | 1–8 | `3` | 用例条数；给了 `test_cases` 时以用例条数为准 |
| `max_iterations` | | 1–10 | `3` | 最大修订轮次 |
| `assertion_mode` | | 见下方白名单 | `""`（= `contains`） | 请求级断言模式 |
| `test_cases` | | ≤8 条 | `null` | 自定义测试集；提供后**跳过用例生成**，`expected` 参与事实断言 |

`CaseInput` —— `test_cases[]` 每条：

| 字段 | 必填 | 约束 | 默认 | 说明 |
|---|---|---|---|---|
| `input` | ✅ | 1–8000 字 | — | 测试输入（会作为 user 消息喂给目标模型） |
| `expected` | | ≤8000 字 | `""` | ground-truth：`contains/exact/regex` 时必须是**输出里真会出现**的字面片段；`rule` 时是交给评委核验的需求规则 |
| `assert_mode` | | 见下方白名单 | `""` | 本条断言模式；空 = 用请求级 `assertion_mode` |
| `scenario` | | ≤40 字 | `""` | `main_path` / `boundary` / `stress` / `injection`；标 `injection` 才激活注入存活检测 |
| `hijack_marker` | | ≤200 字 | `""` | 可选的攻击文本。**定罪依据是系统派生的高熵校验码**（会被追加进该条 `input`），不是这个字段 |

断言模式白名单：`exact` | `contains` | `regex` | `rule` | `custom:<注册名>`。
`rule` 的判定由评委逐条给 `satisfied + evidence`，**默认只提醒不否决**（设 `PM_RULE_VETO=1`
才让语义判定参与一票否决——评委判定带噪声，默认不把主观伪装成客观）。

`RaceRequest` —— `POST /api/race` 请求体：

| 字段 | 必填 | 约束 | 默认 | 说明 |
|---|---|---|---|---|
| `name` | | ≤100 字 | `"赛马"` | 赛马名称（进报告标题） |
| `tasks` | ✅ | 2–10 条 | — | 参赛任务列表，每项是一个 `OptimizeRequest` |

`StatusResponse` —— `GET /api/status/{run_id}` 响应：

| 字段 | 说明 |
|---|---|
| `run_id` / `status` | 状态取值：`running` / `passed` / `max_iterations` / `early_stopped` / `failed` / `needs_clarification`（**只认这个字段判完成**，别猜） |
| `iteration` | 已完成的修订轮次 |
| `aggregate` | 主路聚合分（含 `ci_lower` / `noise` / `unstable_cases` / `judge_bias`） |
| `baseline_aggregate` | 基线臂同口径聚合分（`PM_BASELINE=0` 时为 null）；Δ = 两者之差 |
| `pairwise` | 成对盲评：`verdict` / `votes` / `position_flips` / `conflict` |
| `prompt_versions` / `prompt_version_list` | 各版本正文与分数（同一份数据的两种形态，列表版便于消费） |
| `test_cases` / `evaluations` / `trace` | 用例、逐条评估结果、节点轨迹 |
| `llm_calls` / `llm_usage` | 调用数（按节点累计）与按角色的 token/耗时台账 |
| `prompt_quality_issues` | 代码侧质量门警告（元话语泄漏 / 约束超载 / 净增量超预算等） |
| `early_stop_reason` / `error` | 提前终止原因 / 失败原因 |

报告类响应（`ReportResponse` / `RaceReportResponse`）固定是 `{run_id|race_id, report}`，
`report` 是完整 Markdown；报告里若出现「本轮 Δ 不可采信」，说明本臂有整条用例没有有效样本，
此时 `aggregate` 与 Δ 都不能当质量结论用。

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
