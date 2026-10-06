# PromptMaster 文档：验证方式与发布部署

> 2026-09-17 自 README 拆出。章节标题保留原编号，正文逐字未改；
> 本文覆盖原 README 的「七、验证方式与诚实边界」与「十三、发布与部署」。

## 七、验证方式与诚实边界

三层验证，各自能证明什么、不能证明什么，说清楚：

| 层次 | 命令 | 证明 | **不**证明 |
|---|---|---|---|
| 单元测试 | `pytest tests/ -q`（1485 项、`--collect-only` 计数 2026-10-02 原稿改进模式轮 +36、同日臂感知轮 +3、同日差分/成本轮 +46、同日 CI 门禁轮 +39、同日节点提示词契约轮 +22、同日 lint 范围轮 +3、同日 live 工作流轮 +10、同日 Environment 审批轮 +2、同日测试补强轮 +21、同日 CLI 分发轮 +18、同日 graph 补齐轮 +10、同日 MCP 补齐轮 +8、同日 library 补齐轮 +17、同日 calibrate 补齐轮 +29、同日薄弱点清单轮 +2、同日纯逻辑桩轮 +17、同日降级兜底桩轮 +18、同日 10-03 卡死机修复轮 +19、同日 10-03 锚点出处轮 +11（`tests/test_call_budget.py` / `tests/test_anchor_provenance.py`，活体 1246）、同日 10-04 工程质量轮 +5（`tests/test_complexity_ratchet.py` 1 条 + 脏 `test_runs` 让报告整份崩掉的回归 4 例，活体 1251）、同日 10-04 测试质量轮 +24（`tests/test_modelhub_contention_paths.py` 13 条争用/退避分支、CLI 入口拆细后补的 10 条、死夹具守卫 1 条，活体 1275）、同日 10-05 串行锁轮 +15（`tests/test_live_run_lock.py` 15 条真端点串行锁回归，活体 1294）、同日 10-05 串行锁轮再 +1（补 `holder_path()` 与排队退避轮询这两条死角，活体 1295）、同日 10-05 串行锁轮 +6（`tests/test_live_run_lock.py` 15 条真端点串行锁回归，活体 1301）、同日 10-05 端点实证因果化 +3（那两条实证改写不增减条数，涨的是 `tests/test_preflight.py` 的出口代理披露 3 条，活体 1304）、同日 10-05 封顶接线轮 +4（`tests/test_empty_cap_wiring.py`：聚合吃封顶 / state 带 empty_cap / 渲染披露激活 / 无封顶不误报——§十七·十 照出的三条对不上的复现与钉死，活体 1308）、同日 10-05 抖动出处/可估性/d2 换算轮 +87（§十八～§二十三：`test_jitter_provenance.py` 12 条、`test_noise_measurable.py` 10 条、`test_d2_scaling.py` 9 条、`test_arbiter_seat_jitter.py` 9 条、`test_early_stop_sentinel.py` 9 条、`test_env_isolation_meta.py` 10 条、`test_guard_mutation.py` 15 条、`test_ci_gate_matrix.py` 11 条、`test_precommit_gate_set.py` 4 条、`test_hooks_actually_block.py` 4 条，连同 `test_measurement.py` 3 条改口径，活体 1395）、同日 10-05 账本读者一致性轮 +10（§二十九：`test_ledger_reader_parity.py` 7 条 —— 两本读者对同一本账本必须给同一个答案，`test_guard_mutation.py` 加 3 条变异证明身份过滤能红，活体 1405）、同日 10-05 崩溃臂别被筛掉轮 +14（§三十·一·二：`test_failed_arm_not_dropped.py` 5 条 —— 崩掉的臂不许被“噪声不可估”静默删掉，`test_history_mock_filter.py` 9 条 —— 36 条 mock 臂曾全漏判混进 Δ 统计，活体 1419）、同日 10-05 C1∧C2 重算轮 +5（§三十·七：`test_history_mock_filter.py` 再加 5 条 —— 半崩臂的回推判据三易其稿，最终挂在 `min_score` 上，并钉住那条差点被误伤的真臂`a43e44adcc9f`，活体 1424）、同日 10-05 C1∧C2 分母侧核验轮 +5（§三十·八：`test_c1c2_arm_audit.py` —— 基线 4.98 判得站得住（编造指控能在输出里查到、被批的那句提示词确在基线臂）、`min/max` 对不上钉为旧症状（时间线早于封顶接线修复）、`passed` 五条件里只有 `ci_lower` 不过且新口径下会翻转、活体 1429）、同日 10-05 空产出封顶误伤轮 +8（§三十·九：`test_empty_cap_false_positive.py` —— 合规报告照任务标注「数据缺失」却被封到 4.0 的复现与钉死，实测误伤 50 条降到 0、命中 160 降到 1（那 1 条是真空壳）、活体 1437）、同日 10-05 归档口径重算轮 +13（§三十·十：`test_archive_vs_current_code.py` —— 归档 avg 与当前代码现算差值的时间线/同源/「C1∧C2 归零」直接反证，活体 1450）、同日 10-06 判别力量具轮 +29（§三十一：`tests/test_discrimination.py` 19 条 —— AUC/留一折/可估性门槛，含「聚合侧漏掉聚类口径」「analyze 入口断线」两个必须红才行的接线断言；`tests/test_harvest_coverage.py` 9 条 —— 按人工标签格子的补采覆盖度，含同 id 跨文件去重与 `--coverage` 零写入，活体 1479）、同日 10-06 标签保全轮 +6（`tests/test_harvest_label_safety.py` —— 补采工具的默认路径端到端 + 去重集覆盖全部锚点文件 + 已确认人工分不被静默覆盖；上一轮我把 `log_dir = Path(args.log_dir)` 改丢而 1479 条全绿，这条就是给那条路发的绿牌，活体 1485）；这条数由 `test_test_count_copies_match_the_live_collection` 对着活体收集钉，抄在三处就要三处同改） | 评分公式、短板拦截、**用例数不足不判达标**、JSON 解析、路由、降级重试、注入隔离与定界符越界、双评委合并/仲裁、**单评委结果不污染双评委缓存**、并发排序、缓存命中/淘汰与**配置指纹**、提示词质量门（含领域词不误杀 / 约束超载 / 定界符配平）、null 容错、演示模式隔离与产物落盘、**服务层限流**（滑动窗口 / 429+Retry-After / 赛马按任务数计费）、**两轮澄清提问语义**、**MockGen 场景覆盖校验（含注入用例）**、**用量台账**、**入口编码兜底**、**节点提示词结构契约**、**控制台渲染 XSS 防线与安全响应头**、**任务记录存储层**（内存/SQLite 行为一致 + 跨实例可见 + 并发写不丢账）、**REST API 七个路由与 404/422/401 分支**、**CLI 入参护栏与 `--fast` 快速档**、**LLM 限流退避/降级通道/记账分支**、**缓存落盘与淘汰异常分支**、**SqliteCache 多进程共享缓存**、**评委输出形状容错**（对象数组 issues / JSON 字符串 / 缺尾键 / null 数组 / **键名被按 description 同义改写**，都不该作废整条评估）、**取证先于打分的字段序**、**双向盲评与位置偏置计数**、**fixed–broken 逐条得失记账**、**对比切片与高方差用例优先**、**Δ 的基础设施有效性闸**（零有效样本报「不可采信」）、**注入存活按用例计数与代码派生校验码**、**评委同源检测**、**仲裁分数出处披露**（N/M 例出自仲裁者一人时报告必须说不，别让读者以为那是双评委共识）、**自报分与加权分相等时披露探测器失效**、**满分声明告警**（五维全 ≥9.5 却仍列 issue／或零 issue，只提醒不否决）、**评委复现性 `--repeat`**（绕缓存重复打；绕缓存不许整体替换 CallHook，否则自测会打真端点）、**仲裁失败回退仍记全出处账**（`conservative` 也要带 `judge_scores` 与分差）、**仲裁 prompt 的「给看分数」与「要求忽略」必须共生**、**出处字面量跨 judge/report 一致**（改了值不许让那行静默失效）、**评估缓存的 rubric 指纹**、**校准账本的口径指纹**、**净增量对退化基准不失真**、**`releases/*/tests` 里每一套都必须同时出现在 CI 两个 job 与下面的门禁块**、**重试层叠收敛与墙钟预算**（SDK 隐式重试被钉成 0、超时/5xx 按实测文本与状态码分类、`PM_CALL_BUDGET` 在每次真实发起前检查并把单发 timeout 夹进剩余预算；端点级实证：装死端点收到的请求数 == 我方可见重试数） | 任何与模型能力相关的结论 |
| 拓扑自检 | `python run.py --selftest` | 图能跑通、状态正确累加、迭代终止与兜底正确、双评委仲裁路径 | 优化效果（用的是假后端） |
| 真实 HTTP e2e | `bash examples/run_e2e_stub.sh` / `examples/run_e2e_stub.ps1` | 真实客户端 → HTTP → 响应解析 → 校验链路通畅；两条结构化输出通道均可用；**所有角色端点都被锁在桩上** | 优化效果（桩服务返回固定内容） |
| 提示词回归评测 | `python eval_prompts.py`（离线，零成本；CI 两个 job 都跑）/ `--live`（真实调用，**永不进 CI**） | **节点提示词自身的结构契约**：渲染后无占位符残留、`<安全约束>` 块齐全、各节点专项块存在（optimizer 的自检清单、evaluator 的先取证、reviser 的不得弱化）。判据是**绝对**的——"模板是否残破" | 提示词行为与效果（`--live` 才有行为校验，且只证结构不证质量）；也不拦"这次改动新引入了什么"（那是 gate 的事） |
| 真实节点评测（手动） | GitHub Actions → **「节点提示词真实评测（手动触发）」** → Run workflow（`.github/workflows/eval-prompts-live.yml`） | `--live` 用**确定性代码侧校验**检查提示词**行为**：clarifier 提问预算与 is_clear、optimizer/reviser 过质量门、mockgen 场景覆盖（main_path+boundary）、evaluator 劣质输出是否被压分 | 提示词**效果**（分数高低）；外部端点抖动会直接反映成失败，所以它的红**不等于**代码坏了。**不自动跑**：打真实端点、花真钱、结果依赖外部状态 |
| 提示词改动门禁 | `python run.py gate`（零调用；CI 两个 job 都跑，见 ci.yml） | **本次改动是否把已知事故模式带回来了**：与 git 基线比 `pm/prompts.py` 的模板，只拦「新引入」的规则失败模式（含"已命中规则上继续加重"这个 code 级看不出的通道） | 提示词是否**更好**；也**看不见结构缺失**（实测：删掉整个 `<安全约束>` 块时它 `introduced` 为空，由上面那条抓）。⚠️ 判据是**增量**不是绝对——17 个模板有 16 个天然命中规则，绝对判据会永久假红 |
| 评委校准 | `python run.py calibrate`（锚点样本 + 人工分），加 `--repeat N` 测复现性 | **两轴**：与人工分的偏差（MAE/偏松偏严/排序一致性）＋ **评委跟自己的一致性**（同输入绕缓存打 N 次的极差，越过 `PM_JUDGE_DISAGREEMENT` 即告警）| 样本 <5 条时仅方向性参考；都不提升评委能力，只量化偏差；复现性读数不在交付报告里 |

**要验证提示词优化的实际效果，必须配置真实 API Key 运行。**前三层只能保证
"代码是对的"，不能保证"提示词变好了"——这两件事经常被混为一谈。

质量门禁（提交前）：
```bash
# 与 .github/workflows/ci.yml 逐条同口径（路径别删：漏一个文件就是"本地查、CI 不查"的第三种口径）
ruff check pm/ tests/ run.py run_server.py examples/ eval_prompts.py
ruff format --check pm/ tests/ run.py run_server.py examples/ eval_prompts.py
mypy pm/ run.py run_server.py eval_prompts.py
python -m pytest tests/ -q --cov=pm --cov-report=term-missing     # 覆盖率地板见 pyproject
# 复杂度也在这条 pytest 里：tests/test_complexity_ratchet.py 用 ruff 自己的 mccabe 计数
# 逐函数比对台账 tests/complexity_budget.json —— 台账必须与实测**逐字相等**：
# 新长出来的复杂函数要显式登记（并说明为什么先留着），拆小的收益必须当场回收进台账。
# 拆完函数后重新生成：python tests/test_complexity_ratchet.py --rewrite
# （只手工执行 —— 测试自己从不写被跟踪文件，否则下面那条 git diff 检查会把它自己抓红）
python -m coverage report --include="*/modelhub/*" --fail-under=82  # 网关那条分项线
python -m pytest releases/lobster/tests/ -q
python -m pytest releases/taskboard/tests/ -q
python -m pytest releases/triage/tests/ -q
# 上面三套跑完，被跟踪的运营数据必须还是干净的（三套全部由
# tests/test_packaging.py::test_ci_and_docs_run_every_release_suite 钉在 CI 与本文里）：
git diff --exit-code -- "releases/*/data/*"
python run.py --selftest          # CI 里额外清 PM_API_KEY 再跑一次（干净检出无 .env 也必须过）
bash examples/run_e2e_stub.sh     # 真实 HTTP 链路 + 三条结构化输出通道（桩端点，不出网）
python run.py gate                # 提示词改动回归门禁（零调用；自动探测基线，比较 pm/prompts.py）
python eval_prompts.py            # 节点提示词结构契约（零调用；判"模板是否残破"，与上面的增量判据互补）
```
覆盖率的两条线怎么读（地板值取实测下方留余量，不是质量目标）：

| 范围 | 接入门禁前 | 补 modelhub 测试后 | 复测① | 复测②（补运维路由） | 复测③（补切换链） | 复测④（原稿模式轮） | 复测⑤（逐模块补到 100%） | 复测⑥（降级兜底桩） | 门禁地板 |
|---|---|---|---|---|---|---|---|---|---|
| `pm/` 全量 | 84% | 88.5% | 90.43% | 92.44% | 93.49% | 93.37% | 95.19% | **95.80%** | 92 |
| `pm/` 去掉 modelhub | 95.6% | ~96% | 94.9% | 94.9% | 95.6% | 94.91% | 96% | **96%** | —（被全量线覆盖） |
| `pm/modelhub/*` | 33.5% | 58.6% | 69.8% | 81.0% | 87.1% | 86.37% | 89% | **93%** | 82 |

> ⚠️ 每一列都是**各自时点的实测**（同一条命令、独占、全量 rc=0），不是同一个数被抄来抄去。
> 表格存在的意义就是让下一个人看见"上一轮 81%、这一轮 87.1%"是真涨了。报数前必须重测：
> 跑法就是上面那条 `python -m pytest tests/ -q --cov=pm --cov-report=term-missing`，
> 且必须先看它自己打印的汇总行与退出码（本机有间歇性假红，见上面两条注意）。
> 复测①→②：给运维路由补 21 条用例（`tests/test_modelhub_admin_routes.py`，194 条语句原本 0 覆盖）。
> 复测②→③：给 `pool.chat()` 的**切换/瞬时重试/断路器记分/台账记账**补 11 条用例
> （`tests/test_modelhub_pool_chat.py`，假上游、零出网）；pool 61% → 85%。
> 复测④→⑤：把 `graph.py` / `mcp_server.py` / `cli/library.py` / `cli/calibrate.py`
> 逐个补到 **100%**，并补 `modelhub/server.py` 的三组纯逻辑分支
> （鉴权与头解析 / 角色与参数拼装 / SSE 帧解析与心跳，33 行 → 0）；该文件 80% → 88%。
> 复测⑤→⑥：补 `modelhub/server.py` 的**降级兜底**（流式全失败 → 非流式 + 合成 SSE，
> 84 行）与对话路由的成功/错误路径；该文件 **88% → 100%**，modelhub 分项 89% → **93%**。
> 这一轮的桩全部复用既有范式（`_FakeHub` + 桩 `stream_upstream` / `gateway` + 桩 `_hub_instance`）。

- **已知薄弱点清单（2026-10-02 实测并逐块核过，不是估的）**：
  下面每一条都写明「哪些行 / 属于哪一类 / 补它的前提是什么」。
  ⚠️ **别把整块笼统当成"要活上游"** —— 实测核过，其中一部分是纯逻辑、
  假上游就能覆盖；笼统归类会让下一个人直接放弃本可以补的部分。

  **A. `pm/modelhub/server.py`（100%）**
  > **2026-10-02 已全部补齐**：本轮把降级兜底（84 行）与对话路由的成功/错误路径一并补完，
  > 该文件 80% → **100%**。modelhub 分项 86% → **93%**、`pm/` 全量 → **95.80%**。
  > 补法都是**复用既有假上游范式**（`tests/test_modelhub_stream_contract.py` 的 `_FakeHub`
  > + 桩 `stream_upstream`；`tests/test_modelhub_admin_routes.py` 的 `gateway` 夹具
  > + 桩 `_hub_instance`），没另起一套。**这块已无缺口。**

  **B. `pm/modelhub/pool.py`（85%，45 条语句未覆盖）**
  - 缺口是 `331-367` 的**真 `urllib` 请求段**（实测这 37 行全部未覆盖）—— 要活 socket 才走得到；
    另有 `58-69` / `122-132` / `176-177` / `220-230` / `558` 等零散分支。
  - 其余（切换 / 瞬时重试 / 断路器记分 / 台账记账）已由 `tests/test_modelhub_pool_chat.py`
    用假上游覆盖（pool 61% → 85%）。

  **C. 其余 `pm/` 文件（2026-10-04 逐文件重测，上一版写的"无低于 90% 的文件"已过期）**
  - 低于 90% 的只剩 `pm/calibration.py` **89%**（56 条）与 `pm/modelhub/streaming.py` **89%**（11 条）。
    calibration 的大块是**文件末尾那段 CLI `__main__` 入口**（约四十行，只有 `python -m pm.calibration` 才走）
    与零散的 `except` 兜底；
    streaming 的是上游收尾的几条 `except OSError`。两类**都能用假上游/桩补**，
    不是"要活端点"那一类，别照上一版的结论放弃。
  - `cli/history.py` 90%、`cli/check.py` 93%、`modelhub/usage_store.py` 93%、`memory.py` 94%。
  - `pm/modelhub/vkeys.py` 88% → **98%**（2026-10-04，`tests/test_modelhub_contention_paths.py`）。
    剩 4 条是 `_lock_impl` / `_try_lock` / `_unlock` 里**只在 POSIX 上执行**的分支
    （Windows 走 `msvcrt`，Linux CI 走 `fcntl`）—— 这是平台分割，不是缺口；
    在任一台机器上"补到 100%"都是做不到的，别把它写进待办。
  - `pm/cli/live_lock.py`（真端点串行锁，2026-10-05 新增）**94%**，缺的 5 行
    （81-83 / 93 / 105）与上面 vkeys **同一性质**：都是 `os.name != "nt"` 那一支的 `fcntl`。
    本机（win32）跑不到，Linux 上必然走到 —— **这句是按分支条件推的，没在远端取数验证过**，
    要坐实就去看 ubuntu job 的覆盖率明细，别在这儿改成事实。

  > **`pm/llm.py` 的时序分支已收口（2026-10-05 实测，两遍 `--cov` 逐行一致）**：
  > 410 / 734 / 1286 / 1288 四条以前"只被真时序撞上才覆盖"，现在各有一条条件注入的用例
  > （`tests/test_call_budget.py` 末尾四条）。第 1287 行 **稳定不覆盖**，按**疑似死分支**记着：
  > 内层 `_invoke_with_conn_retry` 在 479/492 就已经抛 `budget.exceeded`，外层 1286-1287
  > 从该调用点走不到。别为了把它"补绿"去扭测试——真要动的是重试层的结构本身。
  >
  > ⚠️ 一条指标层面的教训（2026-10-04）：同一份代码连跑两遍，覆盖率曾是 95.69% / 95.72%，
  > 差值全部来自 `vkeys.py` 的退避分支**只被真竞态撞上才覆盖**。补成确定性用例之后
  > 两轮逐文件 miss 数完全一致（96.22%）。**拿覆盖率做前后对比之前，先确认它是可复现的**，
  > 否则你比较的是噪声。
  >
  > ⚠️ 同族第二次（2026-10-05，这次红的是**用例自己**）：`tests/test_call_budget.py` 那两条
  > 端点实证"装死端点收到几发请求"的计数发生在 handler 线程里，而客户端 timeout 只有 1s ⇒
  > **断言真假取决于谁先被调度**。带 `--cov`（coverage 要给每个新线程装 tracer）或满载全量时
  > 连红五遍，不带 `--cov` 单跑全绿；我一度把它写成"本机连不上 loopback，疑杀软"，那是错的归因
  > （撤回过程与差分见 `docs/iteration/iteration-ledger.md` Round 30）。修法是**把因果倒过来**：
  > 端点先记账、再亲手掐断连接，客户端的错误由那次掐断引起 ⇒ 与调度解耦。
  > 变异 `PM_SDK_RETRIES=2` 仍精确打出 9=(1+2)×(1+2) 与 3，检出力没缩水。
  > **凡"数本地端点收到几次"的用例，都要问一句：记账是不是排在失败之前由构造保证。**

  > **结论**：`pm/` 里真正需要"活 socket 或会按需失败的 socket 桩"才能补的仍只有
  > `pm/modelhub/pool.py` 的 45 条（主体是 331-367 的真 `urllib` 段），**不建议为覆盖率数字专门去做**；
  > 改到那段代码时顺手补即可。其余（calibration / streaming）是"补得起但还没补"。
  >
  > ⚠️ 一条指标层面的教训（2026-10-04）：同一份代码连跑两遍，覆盖率曾是 95.69% / 95.72%，
  > 差值全部来自 `vkeys.py` 的退避分支**只被真竞态撞上才覆盖**。补成确定性用例之后
  > 两轮逐文件 miss 数完全一致（96.22%）。**拿覆盖率做前后对比之前，先确认它是可复现的**，
  > 否则你比较的是噪声。




- 为什么给 modelhub 单独立一条：全量线会把结构问题抹平，而恰恰是这条网关出现过
  "流式通道没有 return、`stream=true` 返回 None、全套测试全绿"的事故（见
  `tests/test_modelhub_stream_contract.py`）。全局线守不住的地方要分项钉。
- ⚠️ 只量子集时会踩到全局地板（`--cov=pm.modelhub` 量出来的数不够 pyproject 的 `fail_under`，直接判红）。
  量局部请补 `--cov-fail-under=0`，那不是门禁坏了。
- 这个数字只统计主进程：`test_cli_guards.py` 那批 subprocess 打真实入口的用例不计入分子，
  所以 `pm/cli/main.py` 的读数偏保守。
- 旧文档里"覆盖率 94%"是手抄快照、且从来没被任何门禁守过（实测 84%）；现在地板值进 CI 了，
  改数字要连着改 `pyproject.toml` 的 `fail_under`、ci.yml 的分项线、上面的门禁块与表格、
  以及**根 README §五** —— 别只改文档。这几处抄件由
  `tests/test_packaging.py::test_coverage_floors_agree_across_all_copies` 钉成同数。
  这条测试是因为实测漂过才加的：CI 早已是 `--fail-under=56`，文档门禁块还写着 `32`，
  照文档跑的人会拿到一条比 CI 松 24 个点、还打印绿色的"门禁"。
  **2026-09-25 深夜又漂了一次，这次漂的是断言没管到的那个文件**：README §五 还写着"地板 87 /
  分项线 56"，而真闸是 92 / 82 —— 访客第一眼照抄的那份反而是唯一自由的，所以把它纳入了断言。
  同一条规则也管住了"多少条用例"这句话（README / 本文 / `.pre-commit-config.yaml` 三处抄件，
  由 `test_test_count_copies_match_the_live_collection` 拿 `--collect-only` 的活体读数对）：
  加用例就要顺手改那三处，否则红的是这条断言而不是文档。
- ⚠️ 已知间歇性红：`tests/test_modelhub_streaming.py` 偶发客户端 `WinError 10053`
  （本机观测约 2/40 次运行，红的那几条签名完全相同）。它红在连接层，不是
  `pm/modelhub/streaming.py` 坏了 —— 先看签名，再决定要不要动产品代码。
Windows 本机注意：pytest 的临时目录根落在 `%TEMP%\pytest-of-<用户>\`，若其中的
`pytest-current` 软链坏掉（本机实测 stat 都抛 WinError 5），pytest 退出期的清理函数会
自己崩 ⇒ **用例全过也返回 rc=1**。仓根 `conftest.py` 与 `releases/*/pytest.ini` 已把临时根
挪进检出目录；若你绕过它们直接跑 pytest，先 `set PYTEST_DEBUG_TEMPROOT=<一个 ASCII 路径>`。
"门禁只认退出码"在这台机器上依赖这条前提，别忽略 rc。

CI（GitHub Actions）在 push / PR 时对 Python 3.11/3.12/3.13 跑以上全部检查；
提交钩子（pre-commit）**本机已装并验过**（2026-09-25：`pip install pre-commit` +
`pre-commit install`，四条钩子对全仓库 rc=0；再故意 stage 一个未使用的 `import os` 提交，
被 ruff-check 当场拦下、commit rc=1、HEAD 未动）。换机器 clone 后要重装一次——
钩子在 `.git/hooks/` 里，不随仓库走。
（代价实测：`pre-commit run --all-files` **109 秒**，大头是全量 pytest。
赶时间可以 `SKIP=pytest-quick git commit ...` 只跑静态三条，但**那一趟不算门禁过了**；
`--no-verify` 是关掉门禁，不是"门禁慢"的解药。钩子的三条命令路径与 CI 逐条相等，
由 `test_precommit_hooks_cover_the_same_scope_as_ci` 守着。）

部署与浏览器运行时验证（2026-09-13 新增，报告见 `docs/archive/deploy_verification_2026-09-13.md`）：
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

### 手动跑真实节点评测（GitHub Actions）

`.github/workflows/eval-prompts-live.yml` 是**手动触发**的工作流（Actions → 「节点提示词真实评测（手动触发）」→ Run workflow）。
它跑 `eval_prompts.py --live`，用确定性代码侧校验检查节点提示词的**行为**，
补上 CI 里那条离线结构契约够不到的那一半。

**为什么它不自动跑**：`--live` 打真实端点、花真钱，而且绿不绿取决于端点当时的状态
（限流、抖动、模型版本漂移）。放进每次提交的 CI 会有两个后果：端点抖一下就红、
红久了就没人看；以及每次 push 都在烧额度。所以结构契约（离线、零成本）进 CI，
行为评测留给人主动点。

**首次使用前要配的 Secrets / Variables**（Settings → Secrets and variables → Actions）：

| 类型 | 名称 | 说明 |
|---|---|---|
| Secret | `PM_API_KEY` | 必填 |
| Variable | `PM_BASE_URL` | 必填，如 `https://your-endpoint/v1` |
| Variable | `PM_MODEL` | 必填，如 `your-model-name` |
| Secret/Variable | `PM_EVALUATOR_B_*` / `PM_ARBITER_*` / `PM_COMPARATOR_*` | 可选；未配则回退全局三件套 |

⚠️ **角色级覆盖是"静默生效"的**：`pm/llm.py` 读的是 `PM_<ROLE>_BASE_URL` 这类键名，
写错了（比如多一个 `_B`）不会报错 —— 它会回退到全局模型，于是"冒烟过了、评测也过了"，
但用的根本不是你以为的那个模型。`tests/test_prompt_eval.py` 里有两条护栏钉这件事
（三处 env 必须逐字一致 + 角色名必须是 `pm.llm` 真的认识的那些）。

**工作流的输入**：
- `node`：`all`（默认）或单个节点 —— 选项由测试对着 `eval_prompts.TEMPLATES` 钉住，不会漂
- `dry_run`：只做前置检查（密钥/端点/依赖），**不调用模型**。首次点这个工作流时先用它

**它证明了什么 / 没证明什么**：
- 证明：节点提示词**行为**符合契约（提问预算、场景覆盖、劣质输出压分、修订净增量…）
- 没证明：提示词**效果**好不好。它不信任 LLM 自评，用的是代码侧确定性判据
- 红 ≠ 代码坏了：外部端点抖动会直接反映成失败，先看是否整批同时红

**建议的跑法**：改过 `pm/prompts.py` 里任一模板后，手动跑一次对应节点（`node` 选那个节点）
比跑 `all` 更快也更省。`run.py gate` 与离线结构契约会在每次提交时先替你挡掉明显问题。

#### 启用人工审批（仓库管理员做一次）

工作流里已经声明 `environment: live-eval`，但**这一行本身不产生任何审批** ——
审批来自该 Environment 上的 required reviewers 配置：

1. 打开仓库 `Settings` → 左侧 `Environments` → `New environment`
2. 名称填 **`live-eval`**（**必须逐字一致**，包括连字符）
3. 勾选 **`Required reviewers`**，选 1~5 个人或团队
4. （可选）勾 `Wait timer` 做延迟、`Deployment branches` 限制只能在 `main` 上触发
5. `Save protection rules`

配好后的行为：每次 Run workflow 会先停在 **"Waiting for review"**，
被选中的人点 Approve 才真正调用端点；Reject 则整次运行取消。

⚠️ **名字拼错会静默失效**：GitHub 会为拼错的名字**新建一个 Environment**（默认没有审批人），
工作流照样跑通 —— 只是你以为存在的审批闸从未存在。
`tests/test_prompt_eval.py::test_live_workflow_uses_the_documented_environment_name`
钉住"工作流里的名字 == 本文写的名字"，改一边就会红。

