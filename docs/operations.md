# PromptMaster 文档：验证方式与发布部署

> 2026-09-17 自 README 拆出。章节标题保留原编号，正文逐字未改；
> 本文覆盖原 README 的「七、验证方式与诚实边界」与「十三、发布与部署」。

## 七、验证方式与诚实边界

三层验证，各自能证明什么、不能证明什么，说清楚：

| 层次 | 命令 | 证明 | **不**证明 |
|---|---|---|---|
| 单元测试 | `pytest tests/ -q`（658 项，`--collect-only` 计数） | 评分公式、短板拦截、**用例数不足不判达标**、JSON 解析、路由、降级重试、注入隔离与定界符越界、双评委合并/仲裁、**单评委结果不污染双评委缓存**、并发排序、缓存命中/淘汰与**配置指纹**、提示词质量门（含领域词不误杀 / 约束超载 / 定界符配平）、null 容错、演示模式隔离与产物落盘、**服务层限流**（滑动窗口 / 429+Retry-After / 赛马按任务数计费）、**两轮澄清提问语义**、**MockGen 场景覆盖校验（含注入用例）**、**用量台账**、**入口编码兜底**、**节点提示词结构契约**、**控制台渲染 XSS 防线与安全响应头**、**任务记录存储层**（内存/SQLite 行为一致 + 跨实例可见 + 并发写不丢账）、**REST API 七个路由与 404/422/401 分支**、**CLI 入参护栏与 `--fast` 快速档**、**LLM 限流退避/降级通道/记账分支**、**缓存落盘与淘汰异常分支**、**SqliteCache 多进程共享缓存**、**评委输出形状容错**（对象数组 issues / JSON 字符串 / 缺尾键 / null 数组 / **键名被按 description 同义改写**，都不该作废整条评估）、**取证先于打分的字段序**、**双向盲评与位置偏置计数**、**fixed–broken 逐条得失记账**、**对比切片与高方差用例优先**、**Δ 的基础设施有效性闸**（零有效样本报「不可采信」）、**注入存活按用例计数与代码派生校验码**、**评委同源检测**、**仲裁分数出处披露**（N/M 例出自仲裁者一人时报告必须说不，别让读者以为那是双评委共识）、**仲裁失败回退仍记全出处账**（`conservative` 也要带 `judge_scores` 与分差）、**仲裁 prompt 的「给看分数」与「要求忽略」必须共生**、**出处字面量跨 judge/report 一致**（改了值不许让那行静默失效）、**评估缓存的 rubric 指纹**、**校准账本的口径指纹**、**净增量对退化基准不失真** | 任何与模型能力相关的结论 |
| 拓扑自检 | `python run.py --selftest` | 图能跑通、状态正确累加、迭代终止与兜底正确、双评委仲裁路径 | 优化效果（用的是假后端） |
| 真实 HTTP e2e | `bash examples/run_e2e_stub.sh` / `examples/run_e2e_stub.ps1` | 真实客户端 → HTTP → 响应解析 → 校验链路通畅；两条结构化输出通道均可用；**所有角色端点都被锁在桩上** | 优化效果（桩服务返回固定内容） |
| 提示词回归评测 | `python eval_prompts.py`（离线，零成本）/ `--live`（真实调用） | **节点提示词自身的结构契约**（占位符渲染、安全约束块、专项规则块）随 pytest 常态回归；`--live` 用确定性代码侧校验（质量门 / 场景覆盖 / 提问预算 / 劣质输出压分 / 修订净增量 ≤30%）验证提示词行为 | `--live` 之外的任何效果结论（离线只保证结构，不保证生成质量） |
| 评委校准 | `python calibrate_judge.py`（锚点样本 + 人工分） | **评委分与人工专家分的偏差实测**：MAE、系统性偏松/偏严、排序一致性——换评委模型或改评分提示词后的回归基准 | 样本 <5 条时仅方向性参考；校准不提升评委能力，只量化偏差 |

**要验证提示词优化的实际效果，必须配置真实 API Key 运行。**前三层只能保证
"代码是对的"，不能保证"提示词变好了"——这两件事经常被混为一谈。

质量门禁（提交前）：
```bash
ruff check pm/ tests/ run.py examples/
ruff format --check pm/ tests/ run.py examples/
mypy pm/ run.py
python -m pytest tests/ -q
# 覆盖率（pytest-cov 已在 dev 依赖里）
python -m pytest --cov=pm --cov-report=term-missing     # 当前 94%；pm/llm.py 与 pm/cache.py 均 100%
```
CI（GitHub Actions）在 push / PR 时对 Python 3.11/3.12/3.13 跑以上全部检查；
本地可选 `pip install pre-commit && pre-commit install` 接入提交钩子。

部署与浏览器运行时验证（2026-09-13 新增，报告见 `docs/deploy_verification_2026-09-13.md`）：
```bash
# 无 docker 环境的替代验证：干净 venv 复刻 COPY → pip install . → 起服务 → healthcheck → 任务闭环
PYTHON="<python3.11+>" bash examples/docker_step_sim.sh
# 真实 Chromium 运行时校验：CSP 违规计数 / tab 事件委托 / 内联 style 被拦（正向对照）/
# XSS 转义回归 / JS 错误 / 资源加载（需要 playwright-core + 本地 chromium）
PM_FAKE_BACKEND=progress python run_server.py --port 8097 &
node examples/browser_runtime_check.cjs http://127.0.0.1:8097/
```

---

## 十三、发布与部署

### 部署形态

```
形态 A · 个人工具（最小）      python run.py --task "..." --fast
形态 B · 常驻服务（推荐）      python run_server.py   # Web 控制台 :8080 + REST API + MCP 同源
形态 C · 智能体接入            MCP stdio（python -m pm.mcp_server）或 Agent CLI
```

### 常驻服务的关键配置（`python run_server.py` 前设置）

| 变量 | 建议值 | 说明 |
|---|---|---|
| `PM_API_TOKEN` | 随机串 | 设了则 POST /api/* 必须带 X-API-Key——**共享网络必设** |
| `PM_ALLOW_ORIGINS` | 留空 | 留空不开 CORS（默认安全）；确需跳源再显式列 |
| `PM_TASK_DB` | `logs/tasks.db` | 设了才允许 `--workers > 1`（多进程共享任务表） |
| `PM_MAX_TASKS` | 默认 200 | 任务表保留上限（SQLite 下为库内条数上限） |
| `PM_CALIBRATE_HOURS` | 12 | 评委校准排程（真实计费，按需开启） |
| `PM_MAX_LLM_CALLS` | 如 300 | 任务级成本总闸（自治场景强烈建议） |
| `PM_FAKE_BACKEND` | — | 无 Key 演示模式（progress/stall/dispute/unclear） |

### 故障排查入口（按顺序）

```bash
python run.py --selftest      # ① 拓扑与控制流自检（无 Key）
python run.py --preflight     # ② 真实端点逐角色冒烟（花小钱，跑大任务前必做）
python run.py history         # ③ 运行历史 + Δ 显著性（判断"有没有变好"）
python run.py calibrate       # ④ 评委可信度校准 + 漂移对比
```

### 常见边界（部署前必读）

- **端点稳定性是环境变量**：AMD 网关曾多次 502/限流（第 8/12 轮作废）；换端点前先 `--preflight`，并把角色级 `PM_<角色>_BASE_URL` 配好
- **max_tokens 预算不能跨端点搬**：第 11 轮「8000=0% 失败」只对 AMD DeepSeek 成立；SenseNova 同模型名 reasoning 吃满 8000——**换端点必须重测预算**
- **注入防御是模型相关的**：同一 prompt 在 AMD 免疫注入、SenseNova 可能 1/1 被劫持——**目标模型用 SenseNova 时，注入用例务必保留在用例集里**（注入门禁会兜底拦截误判达标）
- **日志会膨胀**：`logs/` 会积累每次运行产物；定期把 `logs/run_*.json` `report_*.md` 归档到子目录（.gitignore 已忽略，不影响仓库）
