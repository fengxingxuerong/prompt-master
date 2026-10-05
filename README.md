# PromptMaster · 给 Agent 用的提示词质检与优化闭环

> 一句话：把一句需求变成**被测过、被评过、被证伪过一轮**的提示词，并如实说清哪里还不行。
> 产出是「最终提示词 + 交付报告 + 可选 SKILL.md + 机器可读 JSON」，不是"看起来更好"的一段话。

本仓库有两块东西，别走错门：

| 是什么 | 在哪 | 入口文档 |
|---|---|---|
| **PromptMaster 本产品**（提示词优化/评测/校准） | `pm/`、`run.py`、`run_server.py` | 本文 ↓ 与 `docs/agent-cli-guide.md` |
| **套件三服务**（ModelHub 网关 / 龙虾站 / 任务台账，随仓发布） | `releases/`、`config/` | `docs/suite/README.md` |

## 一、一分钟跑通

```bash
# 0) 装依赖（Python ≥3.11）
pip install -r requirements.txt

# 0b) 可选：装成命令（Agent 配置里就不用写 `python /绝对路径/run.py`，也不会换机器就断）
pip install . && prompt-master --help
#   ⚠️ 装包态下 `.env` 是按**当前工作目录**向上找的；在别的目录跑请直接用环境变量传 PM_API_KEY

# 1) 无 Key，先看代码能不能跑（不证明效果）
python run.py --selftest
PM_FAKE_BACKEND=progress python run.py --task "让 AI 分析销售数据" --fast   # 假后端跑完整流程

# 2) 真实运行：配 Key（cp .env.example .env 后填 PM_API_KEY，或 export）
python run.py --preflight                       # 花大钱之前先逐角色冒烟，必做
python run.py --task "让 AI 分析销售数据" --target-model deepseek-v3

# 2b) 已经有一版提示词了：先免费体检，再决定要不要花钱改
python run.py check --prompt-file 我的提示词.md          # 零调用，命中规则就退出码 1
python run.py --task "让 AI 分析销售数据" --prompt-file 我的提示词.md
#     ↑ 带 --prompt-file 时基线臂自动换成你的原稿，报告里的 Δ 即「比你自己的版本好多少」

# 3) 长任务别占终端：起常驻服务，用子命令
python run_server.py &                          # REST + Web 控制台 + MCP 同源，:8080
python run.py submit --task "……"                # 秒回 run_id
python run.py wait <run_id>                     # 轮询到终态，输出结果 JSON
```

`python run.py --help` 会列出全部参数与**子命令**（`submit/status/report/wait/history/calibrate/library/check/diff/gate`）。
退出码是给 Agent 做分支的协议：`0` 达标交付 / `1` 未达标但已交付 / `2` 参数或配置错误 / `3` 运行失败。
加 `--json` 时 stdout 恰好一个 JSON 对象，进度全部走 stderr。

## 二、五条能力线

| 线 | 命令 | 说明 |
|---|---|---|
| 优化闭环 | `run.py --task`（或 `POST /api/optimize`） | 澄清 → 生成 → 用例 → 执行 → 评估 → 修订，最多 N 轮；带基线对照与成对盲评 |
| 原稿改进 | 加 `--prompt-file 我的提示词.md` | 不从零生成：对**你已有的那一版**做最小改动（保留术语与字段名），基线臂自动换成原稿，Δ 读作「比你自己的版本好多少」 |
| 静态体检 | `run.py check --prompt-file x.md --json` | **零调用、零出网**：用优化闭环里那套代码侧规则当场量一份提示词（约束超载 / 抑制型条款 / 元话语泄漏 / 定界符…），退出码 0=无命中 1=有命中 |
| 版本差分 | `run.py diff A B` | **零调用**：两版提示词（或两个 run）之间改了什么 —— 规则得失 / 结构增删 / 逐行 diff；退出码 1=有新引入的规则问题 |
| 改动门禁 | `run.py gate`（CI 两个 job 都跑） | **零调用**：自动从 git 取基线比 `pm/prompts.py`，**只拦本次新引入的规则失败模式**。判据是增量不是绝对——17 个模板有 16 个天然命中规则，绝对判据会永久假红 |
| 事实断言 | `--cases-file cases.json --assert-mode rule` | 你给的 `expected` 有一票否决权；`rule` 模式交评委逐条核验，`custom:<名>` 可挂你自己的断言函数 |
| 资产与记忆 | `run.py library` / `run.py history` | 达标提示词可按任务相似度检索、导出；history 给 Δ 与显著性 |
| 评委可信度 | `run.py calibrate [--repeat N]` | 锚点人工分 vs 评委分（MAE/偏置/排序一致性）+ 评委自我复现性极差 |

MCP 接入（12 个工具，stdio）：`python -m pm.mcp_server`。计算类工具直接跑 CLI，长任务类转发本机 server。

## 三、这个产品对自己的结论有多硬（先读这节再用它给的分数）

- **评委这把尺子没有对准所有者的标准**（2026-09-27，23 条人工锚点全量校准，零失败）：
  与人工分 MAE 3.73、bias +3.73（系统性放水）；过线判定一致率 26.1%（κ=-0.089）——
  人工判不可用的输出评委全数放行。三家厂商对照（sensenova/glm-5.2/deepseek-v4-flash）
  等幅失守（4.1~4.4）⇒ 是评分协议缺了所有者的标准，不是某个模型的锅。
  已落地：空产出与安全可见性两类盲区有了提示词条款 + 代码硬封顶（校准口径不含封顶，
  量的是评委裸分）；完整实验链见 `docs/evaluation.md` §十四/§十五。
  ⚠️ 考卷已换过三张（9-27 首批 23 条 → candidates 48 条全确认 → 9-29 叠加 19 条
  A/B 考卷，聚合覆盖 64 条）：最近 4 轮 impression 聚合（2026-09-30 零调用重算）
  MAE 3.13［95% CI 2.42~3.86，宽 1.44］、bias +3.09、一致率 0.368、κ 0.074——
  放水方向未变；CI 宽 1.44 且轮间考卷不同，「评委变好/变坏」的精细比较与
  逐轮趋势对比均不成立，单轮读数仍是 ±极差量级的方向参考。
  量化工具：`run.py calibrate --aggregate`（零调用，口径见 §十四）。
  口径与逐轮记录见 `docs/evaluation.md`，账本在 `logs/judge_calibration_history.json`。
- **判定式评分协议（`PM_SCORING_MODE=checklist`）默认关闭，且已被实测否证**。它把五个整数的
  填数权从评委手里拿走一半（评委只答二值判定+证据，分数由代码算）。同一批锚点、评委预算
  抬到 24000 后重测：**17 条配对上 MAE 0.58 → 1.79、bias +1.79，且 Δ 一条负数都没有**；
  它的"极差小"也不是量具变好——17 条只落在 6 个分值上、**10 条同为 9.35**（不违规就没有
  下压项，评委选顶档则总分为常数）。稳定性判据同样未达成（降幅 0.225 < 噪声地板 0.25）。
  重开条件写在 `docs/evaluation.md` §十三：先给顶档一个举证门槛，让分布散开再测。
- **达标 ≠ 有效果**：报告里的 Δ 只在基线有效时才有意义（零有效样本会报"不可采信"）。
- **注入防御是模型相关的**：同一提示词在 A 端点免疫、在 B 端点可能被一句话劫持；
  注入门禁会在 `hijacked>0` 时拒发 SKILL.md，这是兜底不是保险。

## 四、仓库地图

```
pm/                 产品实现（nodes/ 图节点、cli/ 命令、modelhub/ 网关、web/ 控制台、
                    scoring.py 评分协议、memory.py 资产检索、scheduler.py 任务调度）
run.py run_server.py  两个入口薄壳（真实逻辑在 pm/cli/、pm/server.py）
docs/               agent-cli-guide（CLI）· rest-api（HTTP）· operations（验证/部署/门禁）
                    evaluation（评分方法学与实测）· agent-skill · suite/（三服务）
tests/              pytest 用例（CI 门禁；1424 条，无 Key、禁止真实出网）
tests_modelhub/     ⚠️ 验收**脚本**（要活网关），pytest 收集 0 条，不在门禁里 → 见该目录 README
judge_calibration/  评委校准锚点集（candidates.json 48 条全确认：23 人工 + 25 按所有者判例
                    AI 代判；ab.json 19 条 A/B 考卷 = samples.json 11 条核心 + disputed 8 条）
case_templates/     可直接喂 --cases-file 的 5 份领域用例集
releases/           三服务套件与历史发布快照（不是本产品）
logs/               运行产物（run_*.json / report_*.md / 缓存），gitignored，会持续膨胀
scripts/            一次性探针与 A/B 对照脚本，非产品代码
```

## 五、已知边界（按会不会骗到你排序）

1. **仪表未收敛**（见 §三）——产品输出的核心数字仍带 ±1~2 量级的自身抖动，
   而这个"量级"本身也是逐轮摆动的读数，不是这把尺子的固定精度。
2. **覆盖率有门禁了，但它是地板不是目标**（2026-10-04 独占复测，全量 1275 条 rc=0）：
   `pm/` 全量实测 **96.2%**（地板 92），拆开看 `pm/` 去掉网关 **96.5%**、
   `pm/modelhub/*` **94.6%**（地板 82）。这块从 33.5% 补上来：`agents.py` 0→100%、
   `pool.py` 61%→85%、`vkeys.py` 21%→98%、`streaming.py` 7%→89%。
   剩下的缺口集中在 `pool.py` 331-367（真 urllib 那段，要活上游才走得到）、
   `calibration.py` 89% 与 `streaming.py` 89% 的收尾分支；`server.py` 已于 2026-10-02 补到 100%
   （上一版这里还写着"缺口集中在 server.py 361-444"，那句已经过期，本轮按实测改掉）。
   薄弱的那块恰好是出过"流式通道整条失效而测试全绿"事故的那块。
   ⚠️ 同日更早两轮实测是 95.69% 与 95.72%，差值全部落在 `pm/modelhub/vkeys.py` 的
   27↔30 条 miss 上 —— 那是"退避分支只被真竞态撞上"造成的**指标不可复现**。
   已补成不依赖竞态的确定性用例（`tests/test_modelhub_contention_paths.py`），
   现在连跑两遍**逐文件 miss 数完全一致**（96.22% 两轮同值）。
   逐轮实测表与口径见 `docs/operations.md`；
   **上面两个"地板"是配置抄件，与 `pyproject.toml` / `ci.yml` / `docs/operations.md` 的同数关系由
   `tests/test_packaging.py::test_coverage_floors_agree_across_all_copies` 钉住** —— 改地板要四处一起改。
   （实测值本身仍不在闸门里：它要跑完整覆盖率才有，代价是一次套件。上面那条差异说明
   它也会漂，所以引用覆盖率数字前先重跑一次那条命令，别抄 README。）
3. **多进程是显式前提，不是默认**：任务表默认进程内内存（`--workers > 1` 必须先设 `PM_TASK_DB`）、
   缓存默认 JSON 每进程一份、限流是进程内滑窗。单进程才成立的东西别横向复制。
   文件型的两份台账（`data/vkeys.json`、`data/agents.json`）自 2026-09-25 起有跨进程写锁
   （`pm/modelhub/vkeys.py` 的 `.lock` 句柄）—— 修前实测两个进程各发 150 把密钥，盘上只剩
   126~151 把，3 轮里 2 轮有一路直接崩在"文件不可读"上；现在跨进程各发 60 把是 120/120（3/3 轮）。
4. **版本号只有一个事实源**（2026-09-25 收口）：`pm/__init__.py:__version__` = 2.1.0，
   `pyproject.toml` 走 `dynamic` 读它，REST API 的自报版本也从它取；
   ModelHub 网关是**另一条发布线**（`GATEWAY_VERSION = "1.1.0"`，三处引用同一常量）。
   原先 1.1.0 / 2.0.0 / 2.1.0 / tag v1.4.6 四个数并存且互不相认。写死字面量会被
   `tests/test_version_single_source.py` 拦（已用植入探针验过它真的会红）。
   git tag 那条 `v1.4.x` 是**套件**发布线，别拿它当产品版本。
5. **安全豁免 W7/W8/W9 未正式收回**（`releases/suite/security-waiver.html`）；公网暴露前必须处理。
