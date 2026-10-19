# PromptMaster

**中文** · [English](README.en.md)

> 把「让 AI 分析销售数据」这种一句话需求，变成**被测过、被评过、被证伪过一轮**的提示词，
> 并如实说清哪里还不行。产出四件：最终提示词 / 交付报告 / 可选 SKILL.md / 机器可读 JSON。

**它不是提示词润色器。** 按花不花钱排，它只做三件事：

| 你想干的事 | 命令 | 成本 |
|---|---|---|
| 体检一版已有提示词 | `run.py check --prompt-file x.md` | 零调用、零出网，命中规则退出码 1 |
| CI 里只拦本次新引入的坑 | `run.py gate` | 零调用，判增量不判绝对值 |
| 跑完整闭环：生成 → 用例 → 评估 → 修订 | `run.py --task "…"` | 会花钱，先 `--preflight` |

## 30 秒跑通

```bash
pip install -r requirements.txt          # Python ≥3.11
python run.py --selftest                 # 不配 Key：验代码能不能跑，不验效果
python run.py check --prompt-file 我的提示词.md   # 零调用，先免费体检再决定要不要花钱
```

真实运行（先 `cp .env.example .env` 填 `PM_API_KEY`，或 export 同名变量）：

```bash
python run.py --preflight                # 逐角色冒烟，花大钱之前必做
python run.py --task "让 AI 分析销售数据" --target-model deepseek-v3
python run.py --task "…" --prompt-file 我的提示词.md   # 改已有那版，Δ = 比你自己好多少
```

退出码是给 Agent 做分支的协议：`0` 达标 / `1` 未达标但已交付 / `2` 参数或配置错 / `3` 运行失败。
加 `--json` 时 stdout 恰好一个 JSON 对象，进度全走 stderr。

## 什么时候别用它

1. **只想把一句话写漂亮** → 用 [prompt-optimizer](https://github.com/linshenkx/prompt-optimizer)，更轻、开箱即用。
2. **要 100+ 家模型厂商或红队扫描** → 用 [promptfoo](https://github.com/promptfoo/promptfoo)。
3. **需要统计学意义上的「确实变好了」** → 现在给不了：评委判别力尚无统计证据（[§三](#三先读这一节再用它给的分数)）。
4. **要 LangChain / LlamaIndex 生态集成** → 它只接 `openai` / `anthropic` 兼容端点。

## 一、更多入口

```bash
# 装成命令：Agent 配置里不用写 `python /绝对路径/run.py`，换机器也不会断
pip install . && prompt-master --help
#   ⚠️ 装包态下 `.env` 是按**当前工作目录**向上找的；在别的目录跑请直接用环境变量传 PM_API_KEY

# 不花钱，先用假后端把整条流水线跑一遍
PM_FAKE_BACKEND=progress python run.py --task "让 AI 分析销售数据" --fast

# 长任务别占终端：常驻服务（REST + Web 控制台 + MCP 同源，:8080）
python run_server.py &
python run.py submit --task "……"     # 秒回 run_id
python run.py wait <run_id>          # 轮询到终态，输出结果 JSON

# 能点的小窗口（零调用的三张卡不需要 Key）
python run.py panel                  # 浏览器小窗，只绑 127.0.0.1:8765
python run.py desktop                # 原生 Tkinter 窗口：不开浏览器、不起服务
#   左侧导航切五张卡（Alt+1..5 同效）、Ctrl+Enter 跑当前卡；底栏 复制 / 另存 / 中止（④在跑时可掐真子进程）
#   体检 / 考卷有几票 / 两版差分 = 当场出判定；④没 Key 时两个花钱按钮置灰，卡里写明为什么灰
```

`python run.py --help` 会列出全部参数与**子命令**（`submit/status/report/wait/history/calibrate/library/check/diff/gate/panel/desktop`）。

> 本仓还有第二块东西：`releases/` 里的套件三服务（ModelHub 网关 / 龙虾站 / 任务台账），
> 与本产品无关，入口见 `docs/suite/README.md`。

## 二、能力线

| 线 | 命令 | 说明 |
|---|---|---|
| 优化闭环 | `run.py --task`（或 `POST /api/optimize`） | 澄清 → 生成 → 用例 → 执行 → 评估 → 修订，最多 N 轮；带基线对照与成对盲评 |
| 原稿改进 | 加 `--prompt-file 我的提示词.md` | 不从零生成：对**你已有的那一版**做最小改动（保留术语与字段名），基线臂自动换成原稿，Δ 读作「比你自己的版本好多少」 |
| 静态体检 | `run.py check --prompt-file x.md --json` / `--cases-file y.json` | **零调用、零出网**：用优化闭环里那套代码侧规则当场量一份提示词（约束超载 / 抑制型条款 / 元话语泄漏 / 定界符…）；给 `--cases-file` 时改量一份**用例集**——数清它有几票能否决（`rule` 走评委、空 expected 不执行、expected 就在 input 里的只挡得住丢内容），退出码 0=无命中 1=有命中 |
| 版本差分 | `run.py diff A B` | **零调用**：两版提示词（或两个 run）之间改了什么 —— 规则得失 / 结构增删 / 逐行 diff；退出码 1=有新引入的规则问题 |
| 改动门禁 | `run.py gate`（CI 两个 job 都跑） | **零调用**：自动从 git 取基线比 `pm/prompts.py`，**只拦本次新引入的规则失败模式**。判据是增量不是绝对——17 个模板有 16 个天然命中规则，绝对判据会永久假红 |
| 事实断言 | `--cases-file cases.json --assert-mode rule` | 你给的 `expected` 有一票否决权；`rule` 模式交评委逐条核验，`custom:<名>` 可挂你自己的断言函数 |
| 资产与记忆 | `run.py library` / `run.py history` | 达标提示词可按任务相似度检索、导出；history 给 Δ 与显著性 |
| 评委可信度 | `run.py calibrate [--repeat N]` | 锚点人工分 vs 评委分（MAE/偏置/排序一致性）+ 评委自我复现性极差 |
| 插件面板 / 桌面窗口 | `run.py desktop`（Tkinter 原生窗口）或 `run.py panel`（浏览器小窗口，只绑 127.0.0.1:8765） | 把零调用能力做成能点开就用的形状，⑤ 还接上**账本与仪表**（历史 Δ / 评委聚合读数 / 资产库检索 / 改动门禁，四个按钮全零调用）。④闭环接满参数面：原稿模式、用例集（可选②的模板或粘贴内容）、断言模式、采样、快速档，**提交前先跑 `--dry-run` 把预估区间写进确认框**；判据仍由窗口 subprocess 打同一条 CLI 产生。界面这一层按热门竞品补齐的是**到达速度**（全部零调用、不新增判据）：③ 两版**并排着色**、⑤ 历史**过滤** + run_id 一键跳回③、① 顶部列 `{{占位符}}`（**只列不判**）、导出全部读数为 Markdown、深/浅主题、Alt+1..5 与 Ctrl+Enter/Ctrl+S/Ctrl+L/F1，底栏常驻版本与仓库路径 |

MCP 接入（13 个工具，stdio）：`python -m pm.mcp_server`。计算类工具直接跑 CLI，长任务类转发本机 server。

## 三、先读这一节，再用它给的分数

**这把尺子还没对准 —— 而且当前没有能站住的一致性读数。** 跑一次就能看到现状：

```bash
python run.py calibrate --aggregate     # 零调用，纯账本重算
```

活账本现在只有 **1 轮 / 11 条**：MAE 0.882、bias +0.291、κ 0.814、AUC 0.875（95% CI 0.6~1.0）。
数字看着不错，但 **6/11 条锚点来自仓库自带示例集，人工分是编的**；且判定 2×2 里最薄一格只有
4 条（<10）⇒ **这张考卷答不了「达标一致性」**。2026-10-19 起出处按桶拆开（所有者亲判 5 条 /
仓库示例 6 条），示例不再冒充 owner —— 严格人工那一半只有 5 条，n=5 的读数只能当方向。

历史 4 轮快照（MAE 3.13、bias +3.09、κ 0.074、AUC 0.623，CI 覆盖 0.5）只能当方向参考，
且**无法从活账本重算**（输入轮已归档）。两组数都不构成「评委可信」的证据 ——
真结论只有一句：**缺所有者亲判的真锚点。**

由此推出的三条使用边界：

1. **Δ 只在基线有效时才有意义**（零有效样本会报「不可采信」）。
2. **达标 ≠ 有效果**：分数是方向参考，不是验收依据。
3. **注入防御是模型相关的**：同一提示词在 A 端点免疫、B 端点可能被一句话劫持。

判定式评分协议（`PM_SCORING_MODE=checklist`）已实测否证并默认关闭 —— 它稳定了分布，
但把 17 条里的 10 条挤到同一个 9.35 上，且偏松一分没动。
**完整实验链、否证理由、锚点造假自查全部在
[`docs/evaluation.md`](docs/evaluation.md) §三十四**（原 README 本节原文，2026-10-19 迁入）。

## 四、仓库地图

```
pm/                 产品实现（nodes/ 图节点、cli/ 命令、modelhub/ 网关、web/ 控制台、
                    scoring.py 评分协议、memory.py 资产检索、scheduler.py 任务调度）
run.py run_server.py  两个入口薄壳（真实逻辑在 pm/cli/、pm/server.py）
docs/               agent-cli-guide（CLI）· rest-api（HTTP）· operations（验证/部署/门禁）
                    evaluation（评分方法学与实测）· agent-skill · suite/（三服务）
tests/              pytest 用例（CI 门禁；1923 条，无 Key、禁止真实出网）
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
2. **覆盖率有门禁了，但它是地板不是目标**（2026-10-11 独占复测，全量 1669 条 **rc=0**，
   已按准入口径入表为复测⑦）：
   `pm/` 全量实测 **97.15%**（地板 92），拆开看 `pm/` 去掉网关 **96.82%**、
   `pm/modelhub/*` **99.05%**（地板 82）。本轮清了薄弱点清单 C 的两块账：
   `streaming.py` **89% → 100%**、`calibration.py` 的 `main()` 尾段与 `__main__`
   守卫补完（**91% → 95%**，剩零散 `except` 单行）；网关大涨的另一半来自在途的
   pool 配置/http 层测试（见 `docs/operations.md` 账本）。
   `pool.py` 331-367（真 urllib 段）仍按原判不建议为数字专门补。这块从 33.5% 补上来：`agents.py` 0→100%、
   `pool.py` 61%→85%、`vkeys.py` 21%→98%、`streaming.py` 7%→89%。
   剩下的缺口集中在 `pool.py` 331-367（真 urllib 那段，要活上游才走得到）与
   `calibration.py` 的零散 `except` 单行；`streaming.py` 已于 2026-10-11 补到 100%、
   `calibration.py` 的 `main()` 尾段与 `__main__` 守卫同日补完（薄弱点清单 C 的
   "补得起但还没补"两块至此清账）；`server.py` 已于 2026-10-02 补到 100%
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
   2026-10-13 又抓到同族的第二形：**锁文件自身的 `os.open`** 在并发创建时会被瞬态
   `EACCES` 拒（压测 5 轮里 2 例签发者当场崩）——`_process_lock` 的 open 补上与读/写路径
   同款的退避后，25 轮 ×2 进程 ×60 把零失败。
4. **版本号只有一个事实源**（2026-09-25 收口）：`pm/__init__.py:__version__` = 2.1.0，
   `pyproject.toml` 走 `dynamic` 读它，REST API 的自报版本也从它取；
   ModelHub 网关是**另一条发布线**（`GATEWAY_VERSION = "1.1.0"`，三处引用同一常量）。
   原先 1.1.0 / 2.0.0 / 2.1.0 / tag v1.4.6 四个数并存且互不相认。写死字面量会被
   `tests/test_version_single_source.py` 拦（已用植入探针验过它真的会红）。
   git tag 那条 `v1.4.x` 是**套件**发布线，别拿它当产品版本。
5. **安全豁免 W7/W8/W9 未正式收回**（`releases/suite/security-waiver.html`）；公网暴露前必须处理。
