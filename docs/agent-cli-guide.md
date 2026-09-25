# PromptMaster 文档：智能体调用指南（Agent-facing CLI）

> 2026-09-17 自 README 拆出。章节标题保留原编号，正文逐字未改；
> 本文覆盖原 README 的「三之二」。

## 三之二、智能体调用指南（Agent-facing CLI）

本 CLI 是为「被其他智能体调用」设计的：stdout 只给机器可读结果，日志全部走 stderr，
退出码是分支决策的唯一依据。智能体的标准学习入口就是本节 + `--help`。

调用形式两种，行为同一套（`prompt-master` 只是 `run.py` 的装包版，共用同一个启动引导与 `main()`）：

```bash
python /绝对路径/prompt-master/run.py --json --task "..."   # 从检出目录跑
prompt-master --json --task "..."                          # `pip install .` 之后
```

⚠️ 装包态唯一的差异：`.env` 由 python-dotenv 按**当前工作目录**向上查找，
不在仓库根时读不到那份 `.env` —— Agent 侧应当显式注入 `PM_API_KEY` 等环境变量，
不要依赖宿主机上那份文件。

### 退出码协议

| 退出码 | 含义 | Agent 应对 |
|---|---|---|
| `0` | 达标交付（passed） | 直接取用 `best_prompt` |
| `1` | 未达标但已交付（max_iterations / early_stopped） | 读报告遗留问题，决定人工介入或调参重跑 |
| `2` | 参数/配置错误（含无 Key、server 未启动） | 修正参数或先启动 server，**不要原样重试** |
| `3` | 运行失败（status=failed / 未捕获异常） | 看 `errors` 字段；重试安全（缓存命中不重复计费） |

### 同步模式（短任务 / fast 档）

```bash
python run.py --task "..." --fast --json
# stdout = 一个 JSON：run_id / status / aggregate / best_prompt / report_path / errors ...
# 日志在 stderr；退出码见上表
```

### 异步模式（全配置，10~30 分钟）：submit → wait

```bash
# 先起常驻网关（一个终端常驻即可，多 Agent 共享）
python run_server.py

# 提交（秒回 run_id，不阻塞）
python run.py submit --task "..." --cases-file my_cases.json --max-iter 2
# → {"run_id": "...", "message": "..."}   默认连 http://127.0.0.1:8080，可用 --server / PM_SERVER_URL 覆盖

# 等待终态（输出与 --json 同构，含报告全文）
python run.py wait <run_id> --timeout 2400 --interval 15

# 或分步轮询
python run.py status <run_id>
python run.py report <run_id> --out report.md
```

### 提交前预算决策

```bash
python run.py --dry-run --task "..." --cases-file my_cases.json --max-iter 2
# → estimated_llm_calls: {min, max}；不联网不需要 Key。
# 实测校准：全配置 4 用例双采样 2 轮预估 45~83，第 13 轮实际 81 次 ✓
```

### 集成注意

- **重试安全**：target/评估按内容哈希缓存（键含模型与采样指纹），重试命中缓存不重复计费；
  带 `--checkpoint` 可跨进程断点续跑
- **澄清策略**：`--json` 与 `--interactive` 互斥。Agent 模式下需求不清晰时系统自动推断，
  假设全部写进报告的「需求侧遗留问题」，请把它当作必须人工确认的字段
- **用例集**：`--cases-file` 支持 `{"input", "expected", "mode", "scenario", "hijack_marker"}`——
  `scenario: "injection"` 激活注入存活确定性检测（报告出现「注入存活」小节，判定用系统派生的
  高熵校验码；`hijack_marker` 只是可选的攻击文本）

### 三之二·附：记忆层与自检子命令（记忆不用另建，组织历史即可）

```bash
python run.py history --last 30          # 运行历史 + 同任务 Δ 显著性（CI 不含 0 才算"变好了"）
python run.py calibrate --judge evaluator_b --json   # 评委校准 + 与上次对比的漂移告警（±0.5）
python run.py library --query "分诊"     # 跨任务提示词资产库：检索历史达标成品
python run.py library --export <run_id> --out my_prompt.md   # 导出成品直接复用
```

- `history`：把"看起来高了 3 分"升级为"Δ 均值 ± 95% CI，是否显著"——演示模式的 run 自动排除
- `calibrate`：锚点样本上对比评委分与人工专家分（MAE / 偏差 / Pearson r / **过线判定一致率**），
  每次结果入账本 `logs/judge_calibration_history.json`，与上次对比漂移超 ±0.5 告警。
  账本除聚合数外还落**逐条明细**（`items` / `rep_items` / `n_failed` / `failed`）——
  只有聚合数时"n 从 18 变 15"是个无法追问的信号，两臂 A/B 就没法配对归因（2026-09-25 的教训）。
  两个不花钱、不调模型的入口：
  `--make-form <csv>` 把待确认锚点摊成打分表（只填 `human_score` 一列），
  `--apply-scores <csv>` 回填（**只改填了分的条目**；有任何非法行就一条都不写）。
  协议 A/B：`--scoring-mode impression|checklist`，配对比较见 `scripts/compare_scoring_ab.py --ledger`。
  实测示例：evaluator_b bias=+1.24 超告警线（偏松实锤，含幻觉放水）
- `library`：记忆层的读侧——所有终态运行的最佳提示词都可按关键词检索、`--export` 导出复用；
  写侧（跨任务经验自动沉淀）规划中

### 三之二·附二：MCP 接入（智能体标准协议）

`python -m pm.mcp_server`（stdio 传输）暴露 9 个 MCP 工具，OpenClaw / Claude Code / Cursor /
任何 MCP 客户端可直接挂载：

```json
{"command": "python", "args": ["-m", "pm.mcp_server"], "cwd": "<仓库根>"}
```

| 工具 | 类型 | 说明 |
|---|---|---|
| `estimate_cost` | 本地计算 | 提交前预算闸门（调用数区间） |
| `run_history` / `library_recommend` / `library_export` / `calibrate_judge` | subprocess 调 CLI | 与 `run.py --json` 单一口径，永不出现两套行为 |
| `optimize_submit` / `optimize_status` / `optimize_wait` / `optimize_report` | 转发常驻 server | 10~30 分钟的长任务必须外置到 server（stdio 进程随客户端会话生死） |

依赖钉 `mcp==1.30.0`（**必须 <2**：mcp 2.x 会把 starlette 顶到与 fastapi 0.115 冲突的版本，
实测装完 pm.server 直接 import 失败）。
