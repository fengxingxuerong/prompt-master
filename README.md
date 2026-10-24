# PromptMaster

**中文** · [English](README.en.md) · [Releases](https://github.com/fengxingxuerong/prompt-master/releases)

> 把「让 AI 分析销售数据」这种一句话需求，变成**被测过、被评过、被证伪过一轮**的提示词，
> 并如实说清哪里还不行。产出四件：最终提示词 / 交付报告 / 可选 SKILL.md / 机器可读 JSON。

**它不是提示词润色器。** 按花不花钱排，它只做三件事：

| 你想干的事 | 命令 | 成本 |
|---|---|---|
| 体检一版已有提示词 | `run.py check --prompt-file x.md` | 零调用、零出网，命中规则退出码 1 |
| CI 里只拦本次新引入的坑 | `run.py gate` | 零调用，判增量不判绝对值 |
| 跑完整闭环：生成 → 用例 → 评估 → 修订 | `run.py --task "…"` | 会花钱，先 `--preflight` |

**先选入口**——按你的处境挑一行即可，不必先读完这篇：

| 你的处境 | 用它 | 花钱吗 |
|---|---|---|
| 手上已有一版提示词，只想先看有没有硬伤 | `run.py check --prompt-file x.md` | **零调用、零出网** |
| 想在**自己仓库**的 CI 里只拦"本次新引入"的坑 | `run.py diff --base-ref origin/main x.md`（或 `run.py gate`） | **零调用** |
| 先知道"跑一轮大概多少次调用" | `run.py --dry-run --task "…"` | **零调用** |
| 第一次真跑，先确认端点活着 | `run.py --preflight` | 少量调用 |
| 人在终端，跑完整闭环 | `run.py --task "…"`（交互终端下会**先报价、要一句 y/N**） | 计费 |
| 长任务不想占终端 | `python run_server.py` + `run.py submit --task "…"`，再 `run.py wait <run_id>` | 计费 |
| 让别的 Agent 调它 | MCP：`python -m pm.mcp_server`（stdio） | 零调用与计费都有 |
| 不想碰命令行 | `run.py panel`（浏览器小窗）/ `run.py desktop`（原生窗，可选附件） | 生成计费 |

## 30 秒跑通

```bash
pip install -r requirements.txt          # Python ≥3.11
python run.py --selftest                 # 不配 Key：验代码能不能跑，不验效果
python run.py check --prompt-file 我的提示词.md   # 零调用，先免费体检再决定要不要花钱
```

真实运行（先 `cp .env.example .env` 填 `PM_API_KEY`，或 export 同名变量）：

```bash
python run.py --preflight                # 逐角色冒烟，花大钱之前必做
python run.py --task "让 AI 分析销售数据" --fast   # 第一次先用快速档：约 8-10 次调用 / 数分钟
python run.py --task "让 AI 分析销售数据" --target-model deepseek-v3   # 完整闭环：45-102 次调用
python run.py --task "…" --prompt-file 我的提示词.md   # 改已有那版，Δ = 比你自己好多少
```

**新手最少只填这 3 个字段**（复制 `.env.example` 为 `.env` 后，其余保留默认即可跑）：

| 字段 | 填什么 | 去哪拿 |
|---|---|---|
| `PM_PROVIDER` | `openai`（默认，兼容 DeepSeek / 通义 / vLLM 等）或 `anthropic` | 按你用的服务商定 |
| `PM_API_KEY` | 你的模型服务商 API Key（`sk-…`） | 各服务商控制台：OpenAI / DeepSeek / 通义 / OpenRouter… |
| `PM_BASE_URL` | 服务商接口地址，如 `https://api.deepseek.com/v1`、`https://api.openai.com/v1` | 服务商对应文档的 Base URL |

> 想"换模型/分角色/省钱档"再读 `.env.example` 的注释——它逐字段讲了"为什么"。
> 怕填错就先 `python run.py doctor`,它会逐项核对并指出缺哪样。

- 想**先看产出长什么样**：[`docs/sample-report.md`](docs/sample-report.md)（假后端产出的**形状示例**：分节结构真实、读数全是桩）。
- 想**先确认配置对不对**：`python run.py doctor`（零调用：Python / .env / Key / 角色 / 日志 / 端口 / 常驻服务 / MCP 一次说清）。
- 想**不花钱先跑一遍链路**：`PM_FAKE_BACKEND=progress python run.py --task "…" --fast`。
- 想**装成命令**（Agent 配置里不写绝对路径）：`pip install . && prompt-master --help`。
- 退出码是给 Agent 做分支的协议：`0` 达标 / `1` 未达标但已交付 / `2` 参数或配置错 / `3` 运行失败；
  加 `--json` 时 stdout 恰好一个 JSON 对象，进度全走 stderr。

## 它给的分，能信多少

**这把尺子还没对准 —— 而且当前没有能站住的一致性读数。** 零成本自查：

```bash
python run.py calibrate --aggregate     # 零调用，纯账本重算
```

一句话现状：活账本只有 **1 轮 / 11 条**，其中 **6 条来自仓库自带示例集（人工分是编的）**，
判定 2×2 里最薄一格只有 4 条（<10）⇒ **这张考卷答不了「达标一致性」**；历史 4 轮快照也
**无法从活账本重算**（输入轮已归档）。两组数都不构成「评委可信」的证据 ——
真结论只有一句：**缺所有者亲判的真锚点。**

由此推出三条使用边界：

1. **Δ 只在基线有效时才有意义**（零有效样本会报「不可采信」）。
2. **达标 ≠ 有效果**：分数是方向参考，不是验收依据。
3. **注入防御是模型相关的**：同一提示词在 A 端点免疫、B 端点可能被一句话劫持。

完整实验链、否证理由（含判定式协议 `PM_SCORING_MODE=checklist` 为何被关）、锚点造假自查：
见 [`docs/evaluation.md`](docs/evaluation.md) §三十四（原 README 本节原文，2026-10-19 迁入）。

## 什么时候别用它

1. **只想把一句话写漂亮** → 用 [prompt-optimizer](https://github.com/linshenkx/prompt-optimizer)，更轻、开箱即用。
2. **要 100+ 家模型厂商或红队扫描** → 用 [promptfoo](https://github.com/promptfoo/promptfoo)。
3. **需要统计学意义上的「确实变好了」** → 现在给不了：评委判别力的名义显著性（AUC 下界 >0.5）建立在含 6 条编造人工分的 11 条锚点上，不构成可信证据（见上一节与 `docs/evaluation.md` §三十四的 2026-10-23 裁决）。
4. **要 LangChain / LlamaIndex 生态集成** → 它只接 `openai` / `anthropic` 兼容端点。

## 能力一览

| 线 | 命令 | 说明 |
|---|---|---|
| 优化闭环 | `run.py --task`（或 `POST /api/optimize`） | 澄清 → 生成 → 用例 → 执行 → 评估 → 修订；带基线对照与成对盲评 |
| 原稿改进 | 加 `--prompt-file 我的提示词.md` | 只对**你已有的那一版**做最小改动（保留术语与字段名）；Δ 读作「比你自己的版本好多少」 |
| 静态体检 | `run.py check`（`--prompt-file` / `--cases-file`） | **零调用、零出网**：量一版提示词，或数一份用例集有几票真能否决 |
| 版本差分 | `run.py diff A B` / `--base-ref origin/main x.md` | **零调用**：改了什么、修好几条、新引入几条；适合放进你自己的 CI |
| 改动门禁 | `run.py gate` | **零调用**：只拦本次**新引入**的规则失败模式（判增量不判绝对值） |
| 事实断言 | `--cases-file cases.json --assert-mode rule` | 你给的 `expected` 有一票否决权；`custom:<名>` 可挂你自己的断言函数 |
| 资产与校准 | `run.py library` / `history` / `calibrate` | 达标成品检索与导出 · Δ 显著性 · 评委可信度 |
| 图形入口 | `run.py panel` / `run.py desktop` | 浏览器小窗 / 原生窗口（桌面端是**可选附件**，不随 `pip install .` 安装） |

MCP 接入（13 个工具，stdio）：`python -m pm.mcp_server`。计算类工具直接跑 CLI，长任务类转发本机 server。

`python run.py --version` 报出版本（与 REST / 面板自报同源）；`python run.py --help` 列出全部参数与**子命令**
（`submit/status/report/wait/history/calibrate/library/check/diff/gate/doctor/panel/desktop`）。
逐条参数面、Agent 调用协议、面板与桌面的接线细节见 [`docs/agent-cli-guide.md`](docs/agent-cli-guide.md)。

### 在**你自己的仓库**里给提示词上门禁

```bash
git fetch origin main --depth=1
python run.py diff --base-ref origin/main prompts/我的提示词.md
```

退出码 `1` = 这个 PR 相对 main **新引入**了规则问题（修好不判失败）；`0` = 没有变坏。
`check` 管单时点，`diff --base-ref` 管增量 —— 两者都零调用、可逐次提交跑。

<details>
<summary><b>已知边界（按会不会骗到你排序）—— 阅读前先知道这些</b></summary>

1. **仪表未收敛**（见「它给的分，能信多少」）——核心数字仍带 ±1~2 量级的自身抖动，
   而这个"量级"本身也逐轮摆动，不是固定精度。
2. **覆盖率有门禁，但它是地板不是目标**：`pm/` 全量的线（地板 92）、`pm/modelhub/*` 分项的线（地板 93）；
   实测值与逐轮漂移见 [`docs/operations.md`](docs/operations.md)（本 README 不再抄历史快照 —— 它会漂，抄它没意义）。
3. **多进程是显式前提，不是默认**：任务表默认进程内内存（`--workers > 1` 必须先设 `PM_TASK_DB`）、
   缓存默认每进程一份、限流是进程内滑窗。单进程才成立的东西别横向复制；跨进程台账的实测见 `docs/operations.md`。
4. **版本号只有一个事实源**：产品 `pm/__init__.py:__version__` 与网关 `GATEWAY_VERSION` 各自独立；
   git tag 那条 `v1.x` 是**套件**发布线（见 [Releases](https://github.com/fengxingxuerong/prompt-master/releases)），别拿它当产品版本。
5. **安全豁免 W7/W8/W9 未正式收回**（`releases/suite/security-waiver.html`）；公网暴露前必须处理。

</details>

## 仓库结构（节选）与去哪看更多

```
pm/                 产品实现（nodes/ 图节点、cli/ 命令、modelhub/ 网关、web/ 控制台、
                    scoring.py 评分协议、memory.py 资产检索、scheduler.py 任务调度）
run.py run_server.py  两个入口薄壳（真实逻辑在 pm/cli/、pm/server.py）
docs/               agent-cli-guide（CLI）· rest-api（HTTP）· operations（验证/部署/门禁）
                    evaluation（评分方法学与实测）· agent-skill · suite/（三服务）
tests/              pytest 用例（CI 门禁；1984 条，无 Key、禁止真实出网）
extras/             可选附件（桌面窗口 + 项目扫描/分析后端）：不随 `pip install .` 安装，不入主门禁
case_templates/     可直接喂 --cases-file 的领域用例集
releases/           三服务套件与历史发布快照（不是本产品）
```

完整结构（含 `judge_calibration/`、`tests_modelhub/`、`scripts/`、`logs/` 等）与工程口径见 `docs/operations.md`。

| 想看 | 去哪 |
|---|---|
| 全部 CLI 参数与子命令、Agent 调用协议、MCP 工具清单 | [`docs/agent-cli-guide.md`](docs/agent-cli-guide.md) |
| HTTP 接口 | [`docs/rest-api.md`](docs/rest-api.md) |
| **工程口径**：门禁、覆盖率地板与逐轮实测、发版惯例、部署、仓库完整结构 | [`docs/operations.md`](docs/operations.md) |
| 评测方法学（评分协议 / 锚点 / 判别力 / 全部实测与否证） | [`docs/evaluation.md`](docs/evaluation.md) |
| 逐轮迭代账本 | [`docs/iteration/iteration-ledger.md`](docs/iteration/iteration-ledger.md) |
| 一份交付报告长什么样 | [`docs/sample-report.md`](docs/sample-report.md) |
| 给别的 Agent 用的技能说明 | [`docs/agent-skill.md`](docs/agent-skill.md) |
| 本仓第二块东西：`releases/` 里的套件三服务 | [`docs/suite/README.md`](docs/suite/README.md) |
