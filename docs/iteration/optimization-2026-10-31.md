# 全面优化轮（2026-10-31）：基线、修复、验证

> 本轮的任务是"对整个仓库做一次全面优化"。做法：**先量基线，再按"会不会骗到人"排序修**，
> 每一项都带回归用例，最后跑项目自己的全部门禁。本文记录改了什么、为什么、验证结果，
> 以及**发现了但没修**的问题（那一节和修好的那节同样重要）。

## 一、基线（动手之前）

| 项 | 结果 |
|---|---|
| `pytest tests/ -q` | 1983 passed / 1 skipped / 0 failed |
| `ruff check` + `ruff format --check` | 全绿 |
| `mypy pm/ extras/ run.py run_server.py eval_prompts.py` | 全绿（101 files） |
| 覆盖率地板 | 92（`test_packaging` 同数钉住） |

结论：静态层没有可捡的东西（lint/format/类型全清）。所以优化空间在**语义层**：
规则判据的漏判与误杀、闭环里会骗人的分支、资源上界、以及文档与真实行为的不一致。

审计方式：四路只读审计（零调用层 / 闭环正确性 / 可靠性与成本 / 文档一致性），
每条结论都要求"实测复现或代码证据"，不接受猜测。

## 二、修好了什么（按"会不会骗到人"排序）

### 2.1 评估缓存键缺评委指纹 → 换端点/温度后复用旧评分（最严重）

`_judge_spec` 只拼 `build_config(j).model`，于是换 `PM_EVALUATOR_BASE_URL`（同名模型、
不同服务商）、改 `PM_EVALUATOR_TEMPERATURE`（0.1→0.9）、改 `max_tokens`（截断）
**都不会让缓存失效**。报告里的分与 Δ 会出自"已经不再生效的那个评委"，而报告只写"缓存命中"。
`pm/llm/config.py::_decorrelate_judge_b` 的注释早就断言"温度变化会进入 call_fingerprint，
旧评估缓存会一次性失效"——那句话在评估缓存上并不成立。

修：`_judge_spec` 改用 `call_fingerprint(j)`（provider/model/base_url/temperature/max_tokens/timeout）。
代价是存量评估缓存一次性失效（首轮多花评委调用）——这是刻意选择：对"用旧结论冒充新读数"的
容忍度低于对多花一轮钱的容忍度。守卫：`tests/test_judge_cache_fingerprint.py`（6 条，
含端到端 `key_for_eval` 变化）。

### 2.2 空产出封顶误杀短答案 + 只压主路 → Δ 反向

三个独立缺陷叠在一起：

1. `has_empty_deliverable_profile` 里 `len(stripped) < 8` 无条件判空壳 ⇒ `"42"`、`"YES"`、
   `"通过"`、`"3.5%"` 这些**正确且完整**的回答被封顶到 4.0（`execute.py` 注释里的真实用例
   正是"只回答一个数字"）。
2. 封顶按用例号取输出，同一用例的第二次采样覆盖第一次 ⇒ 8.9 分的完整报告会被另一条拒答压成 4.0，
   而真正该封的那条漏封（分数来自**代表采样**，`sample_index` 就在那条评估上）。
3. 封顶只改 `weighted_score`，`passed` 仍是评委原判 ⇒ 报告并排出现「通过用例 1」与「未达标」。
4. 封顶只接在 `judge.py`，基线臂（`baseline.py`）拿裸分 ⇒ 两臂都产出空壳时主路 4.0 / 基线 8.9，
   Δ≈−5，报告写"优化未带来优势"。

修：短答案只在**含空产出标记词或完全不含字母数字**时才判空壳；封顶按
`(test_case_index, sample_index)` 定位输出并同步 `passed`；基线臂走同一个实现。
封顶逻辑从 `evaluate_node` 里抽成 `cap_empty_deliverables()`（可单测）。
守卫：`tests/test_empty_cap_short_answers.py`（10 条）。

### 2.3 注入门禁粘滞 → 修好缺陷的那一轮仍被判未达标

`patch["injection_gate"]` 只在**拦截时**写，上一轮的 `{"blocked": True}` 会一直活着：
修订把注入缺陷修掉后 `status=passed`、`avg=9.0`，报告首行照样印
「⚠️ 未达标：…注入门禁拦截（1/1 条被劫持）」。`unresolved_questions` 同病（只追加不清理）。

修：抽出 `injection_gate_patch()`，每轮无条件覆写（含 `blocked=False`），并按前缀精确清除
本节点上一轮加的那条未解决问题（其它来源的不许被顺手抹掉）。
守卫：`tests/test_injection_gate_stickiness.py`（5 条）。

### 2.4 零调用层：约束预算闸对主流写法整类失效（本轮用户可见收益最大）

`count_constraints` 只认 `[约束]` 与 `## 约束` 两种段头，实测下列写法一律 `count = 0`：
`**约束**`、`__约束__`、`【约束】`、裸 `约束：`、`Rules:`、`## Rules`、`## 约束 ##`；
编号形态 `(1)` / `（1）` / `①` / `1．` 也一条都数不出来。
后果：一份 12 条硬约束的提示词拿到"未命中任何确定性规则"、**退出码 0**，
用户据此省钱跳过真评测。连带 `promptdiff` 的结构通道也一起归零。

同时补上误杀防线：`## 内容要求` / `## 交付要求` / `## 输出要求` / `## Output Format`
列的是"要写哪些部分"，此前被判 `constraint_overload` → 触发无谓重写
（这类误杀的代价本仓库有记录：均分 5.78→4.76、提前终止）。

修：段头判定收口成一个 `_section_header()`（`count_constraints` 与 `structure_items` 共用，
两处各写一遍必然漂——2026-10-13 已经漂过一次）；编号正则扩到五种形态；
非约束段名补 `内容|交付|验收|写作|质量` 与英文同义词。

**端到端实测**（同一份 12 条 `要求：` 的提示词）：
修复前 `约束条目 0 条` / exit 0；修复后 `约束条目 12 条（[要求]×12）` + `constraint_overload` / exit 1。
守卫：`tests/test_quality.py` 新增 4 组参数化用例（11 种段头 × 5 种编号 × 5 种非约束段名）。

### 2.5 抑制型规则误杀正确写法 + promptdiff 撇号跨行吞段

两件事：

- `check` 对原文直接 findall，而 `diff` 早就做了"剥引用"。于是模块 docstring 自己推荐的写法
  「不得停止处理，也必须继续按骨架输出」被判成 `suppressive_rule` 并强制重写；
  同一份文本 `check` 红、`diff` 说"没引入新命中项"。
- `promptdiff._QUOTED` 的 `'[^']*'` 不排除换行：正文里两个英文撇号（`Don't … user's`）
  会把中间整段"当作引用"抹掉，那里的 `停止处理/不输出结论` 随之消失 ⇒ `regressed=False`、
  CI 门禁**静默放行**。

修：抑制检测收口成 `pm.quality.suppressive_hits()`（剥引用 + 剥否定语境，`check`/`diff` 共用）；
单引号分支加 `\n` 限制。守卫：`tests/test_quality.py` 2 条 + 既有 promptdiff/gate 用例。

### 2.6 兜底式缺失：承诺的豁免是死码

`_PER_FIELD_RE`（"其余照常输出/已有数据保留"）定义后**从未被调用** ⇒ docstring 承诺的
"已区分两种缺失则豁免"不存在。否定词表也漏了 `不需要/不必/无需/无须`。

修：豁免接进判据，但**按行作用域**而不是全文（第一版写成"全文出现即放行"，当场被
`tests/test_panel.py` 推翻：一份提示词完全可以一边写"其余字段照常输出"、一边追加
"所有结论位置一律标注数据缺失"——**两者并存正是自相矛盾**）。另加"空输入分支"豁免
（`输入完全为空` 是模块推荐的第 ① 条分支，不是全局开关；事故原句是"输入**非空**"，不会被误放）。

### 2.7 定界符：散文重复引用被当成结构错误

`读取 <输入> 中的数据。若 <输入> 为空则输出无数据。` 被判「重复开标签」。
修：只对**处于行首或行尾**的标签做配平（真实事故 `<输入> 以下是…` 在行首，仍能抓到；
句子中间的两处引用不参与）。第一次尝试写成"整行只有标签"被 Run A 的反例推翻，已保留说明。

### 2.8 用例体检漏读条内 `assert_mode`

运行时的口径是 `mode or assert_mode or 缺省`，体检只读 `mode`。于是同一份 JSON 换个键名就两套结论：
`{"assert_mode":"custom:x"}` 被报"白占一个用例位"（运行时其实执行），
`{"assert_mode":"rule"}` 被算成 contains（judge_only 桶全数错）。

### 2.9 可靠性：三处无界增长 + 一处明文秘密 + 一处 500

| 缺陷 | 后果 | 修法 |
|---|---|---|
| 限流器 `_MAX_KEYS` 形同虚设（只删空桶，而桶只在同 key 再访问时才空） | 换 key 就能把字典撑到无界（实测 10200 → 10201） | 两段式剪枝：先删整桶过期，再按最久未活动淘汰到上限 |
| `TaskManager._futures` 只增不删（只有 `shutdown()` 清） | 长跑服务里无界增长 | `add_done_callback` 完成即摘 |
| SSE 泵线程 + 无界队列，客户端断流不收 | 每条断流留一个线程 + 一条 socket，内存随上游产出涨 | 队列有界（64）+ `closed` 旗标 + `finally` 里 `gen.close()`；泵抽成 `_open_frame_pump`/`_pump_frames`（复杂度棘轮不接受主函数涨到 31，顺带可测） |
| 限流日志把 `PM_API_TOKEN` 原文写进 `logs/` | 管理口令明文落盘 | `_mask_key()`：IP 原样、令牌打指纹 |
| `PMH_TIMEOUT=60s` 这类带单位写法在流式端点抛未捕获 ValueError | **只有流式**全 500，非流式正常 | 改用 pool 的容错解析器 |

守卫：`tests/test_resource_bounds.py`（7 条）、`tests/test_sse_disconnect_cleanup.py`（3 条）。

### 2.10 Windows 上会「假绿」的子进程解码

跑全量套件时读到一个警告而不是失败：`test_rejudge_form` 的两条 node 用例在读取线程里抛
`UnicodeDecodeError: 'gbk' codec can't decode byte 0xa8` —— 而它们是**绿着**的：
`subprocess.run(..., text=True)` 不给 `encoding` 时用 locale 解码（中文 Windows = cp936），
而 node 吐 UTF-8。解码在读取线程里失败 ⇒ `rc.stdout/stderr` 变空串 ⇒
`assert "冒烟全部通过" in rc.stdout` 成了永远为假的判据，失败信息也变成空白。
linux CI 的 locale 是 UTF-8，永远不会暴露它。

修：三处补 `encoding="utf-8"`；新增 `tests/test_subprocess_encoding.py`（AST 扫全仓，
含阴性对照防止判据自己失效——这条闸当场又抓出一处此前手写正则漏掉的调用）。

### 2.11 文档与真实行为对齐（11 处）

| 文档说 | 实际 |
|---|---|
| `--task "..."` 可照抄 | 3 字 < **≥4 字** 下限 ⇒ exit 2（连 `--dry-run` 也拦） |
| `--fast` 约 8-10 次调用 | 实测 **12-22**（`--dry-run --fast` 与真跑各对一次） |
| 4 用例双采样 2 轮预估 45~83 | 实测 **59~109** |
| agent-skill「13 个工具」只列 10 个 | 缺三个**零调用**工具（`prompt_check`/`prompt_diff`/`cases_check`） |
| `.env.example`：evaluator 默认 8000 | **24000**（照注释填 8000 是往下砍） |
| 指向 `pm/llm.py` | 已拆成 `pm/llm/config.py`（2026-09-27） |
| operations.md：1979 条 / tests_extras 41 条 | 2047 / **56** |
| rest-api 端点表漏 `GET /api/history` | 存在且受 token 保护 |

另：代码会读但 `.env.example` 没写的 8 个开关（`PM_LIVE_LOCK*`、`PM_PREFLIGHT_*`、
`PM_RATE_LIMIT_*_WAIT`、`PM_TEMP_BOOST_ON_PARSE`、`PM_CONN_BASE_SLEEP`）已补一节。
所有 `--task "..."` 示例改成真实需求串，并把"占位符会退 2"写进三份文档。

### 2.12 顺带：`未找到` 不在占位符表里

`CheckVerdict.evidence` 的字段说明自己教评委"找不到依据时写『未找到』"，而
`_is_placeholder` 不认它 ⇒ 写 `未找到` 照样拿 9.5 顶档，顶档的举证门槛等于没有。
补 `未找到/找不到/不适用`。

## 三、验证

| 门禁 | 结果 |
|---|---|
| `pytest tests/ -q` | **2140 passed / 1 skipped / 0 failed**（基线 1983 → 新增 157 条） |
| `ruff check`（CI 同口径范围） | All checks passed |
| `ruff format --check` | 全绿 |
| `mypy pm/ extras/ run.py run_server.py eval_prompts.py` | Success（101 files） |
| 覆盖率 | **96.38%**（地板 92）；`pm/modelhub/*` 分项 **98%**（地板 93） |
| 复杂度棘轮 | 逐函数预算全绿；`_stream_response` 28→26、`_library_recommend` 掉出台账，条数 32→30 |
| 测试条数抄件 | 4 处同改（`test_packaging` 钉住） |
| 端到端 CLI | `--version` / `check` / `diff` / `gate` / `calibrate --aggregate` / `--dry-run` 全部按预期 |
| 一处真实快速档 | `run_d67fe934ad39`：**12 次调用**、avg 8.44、status passed（见第六节说明） |

## 四、第二轮（同日续）：成本与用量口径

第一轮把「成本台账只记成功调用 / `add_retry_request` 没有生产调用点 / 流式不进日聚合」
列为下一轮优先项——**这三条在本轮修掉了**，因为它们属于"报告写出来的数字与真实花费不一致"，
而本项目的立身之本就是如实说清哪里还不行。

### 4.1「重试请求数」不再恒为 0

`UsageLedger.add_retry_request` 自 §三十·十八 就存在，但**全仓零生产调用点** ——
报告里那列在每一轮真跑里恒为 0。那比"没有这一列"更坏：读者会把 0 读成"这轮没重试过"。

现在四个重试点各自登记（口径：同一次逻辑调用里，除首发以外的每一条真实 HTTP 请求）：

| 重试点 | 位置 |
|---|---|
| 瞬时连接故障重试 | `retry.py::_invoke_with_conn_retry` |
| 429 退避 / Key 轮换重试 | `retry.py::_invoke_with_rate_limit_retry` |
| 通道 A 失败 → B 首发的全价请求 | `calls.py::structured_call` |
| 通道 B 的解析重试 | `calls.py::structured_call` |

报告「失败/重试」表的渲染条件也从"只看 `failed_calls`"改成"两者任一非零" ——
否则"通道 A 传输层成功但解析失败、B 重发成功"这类轮次（`failed_calls=0`、
`retry_requests=1`）会把整段吞掉。

守卫：`tests/test_retry_requests_recorded.py`（4 条，含"两发 429 后成功 ⇒ 记 2 条"、
嵌套作用域并账、无台账时空操作）+ `tests/test_report_shows_failures.py` 新增 1 条。

### 4.2 成本段声明口径

`render_cost_section` 只在**有失败调用**时补一句：金额只按成功调用返回的 token 折算，
失败/超时在台账上拿不到 token 因而不计入，「合计」是**本方能看见的那部分，不是账单**
（上游是否已计费，我方无从得知）。没有失败时**不渲染**这句，免得噪声稀释"这轮有问题"的辨识度。
守卫：`tests/test_cost.py` 2 条（有失败/无失败各一）。

### 4.3 流式调用进 `/v1/usage` 日聚合

非流式路径在 `chat_completions` 里记 `record_call`，流式这条路只写台账事件 ——
于是日聚合**漏掉全部流式调用**：台账（事实源）里有、看板（日聚合）里没有。
这种"两个读数各说一套"比缺数据更难查：对着看板核对账单会平白少算一半。
现在流式 `finally` 里两处都记（事件 + 日聚合）。
守卫：`tests/test_sse_disconnect_cleanup.py` 新增 1 条，**做过变异验证**——
把 `record_call` 换成空吞的 lambda，该用例当场红。

## 五、第二轮：仍然没修，以及为什么

下面三条**不是"没时间"**，是"改它要动一个需要所有者决定的口径"：

1. **判定式（checklist）下违反全部约束仍可达标**：`_STEPS`（0→9.5 / 1→8.0 / 2→6.5 /
   3→5.0）是**对齐 rubric 区间语义**逐档拟合出来的，函数注释写明了它为什么这么摆；
   空桶给 9.5 还配了 `_MIN_PROMPT_ITEMS` 的兜底（清单太薄就退回印象式）。
   改成"按比例"或"空桶给中位分"会**推翻一份有出处的校准**，而这属于评分方法学的决定，
   不是 bug 修复。要做的话，正确姿势是先用锚点重跑一轮校准拿证据（docs/evaluation.md 的惯例）。
2. **成本闸用 `llm_calls` 而非真实请求数**：把闸改成按请求数计会把报告头的
   「LLM 调用次数」口径一并改掉（那是被多次引用的一个数），同样需要所有者拍板。
   本轮先把**请求数**变得可见（见 4.1/4.3），口径决定留到下一轮。
3. **`assertions` 的 advisory 降级吞掉否决权**：`looks_like_rule` 命中「必须/不得/要求」
   就把逐字断言降级成提醒，于是"该公司不得对外担保"这类**真片段**永远拿不到否决票。
   收紧它有误伤风险（会把真的需求规则当片段执行），需要一批真实用例的取舍证据。

## 五、第三轮（同日续）：前端渲染、统计口径、账本耐久

第二轮之后又开了两路只读审计（前端/渲染层；存储/记忆库/校准 CLI），下面九条是
**它们实测量出来的**，都不是"读代码猜的"。

### 5.1 历史面板的注入面（XSS）

`renderHistory` 里有 4 处裸拼 `innerHTML`，而 `runs[].task` 就是**任务原文**——同文件另外
30 处都走 `esc()`，只有这里没走。一条形如 `<b>✅ 显著 Δ+2.00</b>` 或
`</td></tr><tr><td>…` 的任务名就能伪造结论、插假行。CSP 的 `script-src` 只挡"执行脚本"，
挡不住"让页面说谎"。

修：外部字段一律先进局部量并 `esc()`，再插模板；数字统一 `Number().toFixed(2)`。
守卫 `tests/test_web_console_frontend.py`：源码级判据（剥掉 `esc(...)` 后不许再有裸字段）
+ 兜底判据（剥掉 `esc/Number` 后不许剩未转义插值）。

### 5.2 版本曲线遇 `avg_score=null` 抛错，被 `catch(_){}` 静默吞

`revise` 刚 append 的那一版还没有分数（`PromptVersion.avg_score` 默认 None），
旧版直接 `scores[i].toFixed(1)` ⇒ `TypeError`，而整个渲染包在 `poll` 的 catch 里：
症状是**图表空白 + 左栏停止刷新 + 零提示**，每轮评估窗口都会出现。

修：先滤掉非数值再画；并把"另有 N 版未评估"画在图上（否则 2 个点会被读成"只有 2 版"）。
守卫用 node 真跑 `drawCurve`（canvas 上下文用 Proxy 桩，不需要 DOM）。

### 5.3 `history` 的「✅ 显著」有一半是算出来的

三处，每一处都能造出**假显著**：

| 缺陷 | 实测 | 修法 |
|---|---|---|
| 小样本用 1.96（大样本分位） | n=3、Δ=[1,2,3] 判显著；t(2)=4.303 下 CI 是 [-0.48, 4.48] | 按 df 查 t 分位（表列到 30，更大用 1.96） |
| `var=0 ⇒ se=0 ⇒` 零宽区间 | [0.5, 0.5] ⇒ CI [0.5, 0.5] 且"显著" | n<5 判**不可判**（"抖动估不出来"，与报告里"噪声 0.0 = 没测到"同一条纪律）；n≥5 仍按显著 |
| 可信臂不足 2 条的组被 `continue` | 该组的崩溃臂连"被点名"的机会都没有，而注释写着"必须点名" | 输出 `estimable:false` + `delta_ci95:null` + 保留 `excluded_untrusted` |

渲染侧同步：不可判时印「❔ 不可判」而不是「⚠️ 不显著」——后者也是一个判定，
会让人以为"测过了，没效果"。

**真实账本上的影响**（`run.py history --json`，本机 50 条运行 / 8 组）：

```
翻转: …分析销售数据并给出趋势判断…  n=4  t=3.182  old_sig=True  new_sig=False  mean=0.98
翻转: …读取客服对话原文识别根因…      n=2  t=12.706 old_sig=True  new_sig=False  mean=1.57
翻转: …采集目标页返回的商品信息…      n=2  t=12.706 old_sig=True  new_sig=False  mean=-1.65
共 3 组翻转（/ 7 组）
```

也就是说，这台机器上过去有 **43% 的「✅ 显著」是小样本分位算出来的**。

### 5.4 推荐资产时硬推不相似的

`library --recommend` 的门槛写的是 `> 0.05`，而记忆注入路径是 `0.35`——同一个概念两套实现。
实测"写秋天的诗"能给"写季度销售报告"当结构参考（Dice 0.51），0.08/0.11 的噪声也全过。
另外 `library` 缺"Δ ≥ 0"过滤（注入路径一直有）：**通过了但比基线更差**的那一版当 few-shot
参考，等于教新任务怎么变坏。

修：相似度实现与门槛收口到 `pm/memory.py`（唯一一份），CLI 侧 import；补 Δ≥0 过滤并回显
`delta`。准入条件抽成 `_recommend_candidate()`（顺带让 `_library_recommend` 掉出复杂度台账）。

### 5.5 校准账本会被"当空账本"吃掉

三件事叠在一起，最坏是"花完钱、历史清零、还没有输出"：

- 损坏 JSON ⇒ `return []`，而 `_save_entry` 随后**整文件重写**：实测截断的 2 轮账本，
  跑完一轮（真实计费）后只剩新写的 1 条，全程零提示；
- 合法 JSON 但顶层是对象 ⇒ `.append` 抛 `AttributeError`，而那正好发生在付费跑完之后、渲染之前；
- `write_text` 非原子：写到一半崩掉留下语法坏掉的账本，下一个人再读出"空"。

修：坏账本**改名保留**为 `.corrupt-<ts>` 并告警（账本是校准结论的唯一出处，坏文件也是证据）；
非数组一律按坏处理；写入改 `tmp + replace`；数组里混进非对象时跳过并说明。
守卫 `tests/test_calibrate_ledger_durability.py`（5 条，含"保留下来的内容逐字未变"）。

### 5.6 带 BOM 的文件不归一化换行

BOM 分支直接 `raw.decode(...)`，只有无 BOM 分支做了 `\r\n → \n`。于是**同一份内容**
存成 UTF-8 是 LF、存成 UTF-8-BOM / UTF-16 是 CRLF ⇒ `run.py diff` 把整篇报成"每一行都改了"，
送进模型的正文字里还带着 `\r`。修：归一化抽成 `_normalize_newlines()`，**每条返回路径都过它**。

### 5.7 报告印的阈值与判定用的阈值不是同一个

`passed` 在计算时用当轮 `PM_PASS_THRESHOLD` 定死，而报告此前在**渲染时**再读一次环境变量。
实测 `run_0792cd9acf75`（当时阈值 8.5、avg 8.25 ⇒ passed=False）按后来的默认 8.0 渲染：
总览每道闸都过、首行却写"未达标"，报告解释不了自己的判定；反过来会印出
「✅ 达标：均分 7.8 ≥ 阈值 8.0」这种假命题。

修：`AggregateScore` 增加 `threshold` 字段，`from_evaluations` 一并冻结；渲染只读它。
老归档没有该字段时照旧渲染，但**明说**"此处取自当前配置"。控制台的"阈值 ≥8.0"
与曲线上的阈值线同样改成读归档值；噪声为 0 且 `noise_measurable=false` 时印"不可估"。

### 5.8 判定式下"一条都没满足"仍能过线

台阶表按**绝对条数**取档，于是清单越短越掉不下来：3 条约束全不满足 → 维度 5.0（勉强可用），
乘 0.25 权重后总分 **8.38 仍通过**；2 条清单更低到 6.5 → 8.75。而 `_STEPS` 上面的注释
写的是「4 条及以上 1-4（需重写）」——"4 条"是台阶表的实现细节，不是语义。

修：**用例级**封顶（`apply_caps`，与"编造关键事实"同一个执行点）——清单 ≥2 条且
**每条都不满足**时总分封到 6.0。故意不动维度台阶：逐桶改会顺手把"1 条清单违反 1 条"
也压死，而 rubric 对"个别轻微问题"给的就是 8 档。

### 5.9 第四轮补完：上一轮明确留下的四条

这四条是 §5.9 原样留下的，本轮全部落地。

**(1) 存储两后端不同口径**（`pm/store.py`）。同一个配置在两个后端给出不同结果，是最难查的
一类问题：换 `PM_TASK_DB` 就变行为，而症状（任务凭空 404）看起来像调度器坏了。

| 场景 | 内存后端（旧） | sqlite 后端（旧） | 现在 |
|---|---|---|---|
| `evict(0)` | 清空 ⇒ 提交后立刻 404 | 夹到 1 ⇒ 留 1 条 | 两个后端都 = **不淘汰** |
| `evict(-1)` | `next(iter({}))` 抛 `StopIteration` | 夹到 1 照跑 | 都不抛，语义同上 |
| `update_task` 并发写两个字段 | 一把锁，正常 | 读/写各取一次锁 ⇒ **丢一个字段** | sqlite 改成单锁读-改-写 |
| `RecordView("race")` | `len`=1 但 `list()` 为空 | 同 | 补 `list_race_ids()`，能数也能逛 |

`evict(0)` 归一成"不淘汰"的理由：用户写 0 通常意思是"别设上限"，而旧行为是"立刻把任务删掉"。
守卫 `tests/test_task_store_parity.py`（12 条，两后端参数化跑同一批断言）。

**(2) `--json` 的错误出口不是 JSON**。README 的契约是"stdout 恰好一个 JSON 对象"，
而 `calibrate`（样本加载失败 / 空样本 / 锚点全失败）与 `library --export`（找不到运行）
只往 stderr 打一句、stdout 为空。调用方 MCP 的 `_run_cli_json` **无条件** `json.loads(stdout)`，
拿到的是 `Expecting value: line 1 column 1` —— 一个与真因无关的报错，真因被 `capture_output`
收在 stderr 里没人看。现在统一走 `_error_out` / `_calib_error`：`{"error": ..., "exit_code": ...}`。
守卫 `tests/test_cli_error_json.py` 从**进程级入口**真跑（要钉的正是 stdout 契约）。

**(3) 控制台不显示任务失败原因**。卡片只画 status/iteration/llm_calls/均分/早停，
`StatusResponse.error` 一个字都不读——无 Key / 配额打满 / 端点点挂时只有一块红 badge。
修：`_progress_of` 补 `errors`（节点级明细，截 20 条）、`StatusResponse` 补字段、控制台渲染
`error` 与 `errors`；`docs/rest-api.md` 的字段表同步（这条被 `test_api.py` 的
"文档字段 = 模型字段"差分判据当场钉住了）。

**(4) 事实断言表按采样逐发渲染**。`test_runs` 是逐**采样**的，`PM_SAMPLES_PER_CASE=2` 时
同一用例出两行。实测 `run_13f9a99096e9` 的 case#0/#1 各两行共 4 行，而
`aggregate.n_assertions` 是 2。现在按用例去重，且**保留失败的那条**（与
`_collect_assertions` 的保守口径一致：任一样本失败即视为该用例失败——留下"通过"等于掩盖证据）。

### 5.10 第五轮：`literal` —— 合规片段终于拿得到否决权

第三轮就记着的第一条：`looks_like_rule` 命中「必须/不得/要求」就把逐字断言降级成提醒，
于是「本公司不得为关联方提供担保」这类**真片段**永远拿不到否决票。

先取证（扫了 `case_templates/*` 与 `judge_calibration/*` 里全部 20 条带 `expected` 的用例）：

```
判成"规则"（会降级）9 条，全部是 25~38 字的多分句 → 判得对
判成"字面片段" 11 条，全部 ≤ 11 字
```

也就是说启发式在**真实语料上没错**。问题出在语料没覆盖的那一格：**短句 + 规则词**。
「必须包含3条结论」（8 字）与「本公司不得对外担保」（9 字）长度几乎一样、语义相反 ——
任何按长度/句式的第二层启发式都会在其中一边打错人，而打错人的代价是一票否决。

而且这条比审计说的更严重：这类用例**根本没法表达**。预检（`--cases-file` / REST）
会直接把整份文件拦下（退 2），用户连跑都跑不了。

所以给的是**显式开关**，不做第二次猜：

```json
{"input": "甲为乙担保", "expected": "本公司不得为关联方提供担保", "literal": true}
```

- 预检放行（`rule_like_expected_detail` 跳过它，拦截话术里也把这条路写出来了）；
- 运行时照旧**逐字比对**、照旧**计入一票否决**（不再降级成"仅提醒"）；
- `check --cases-file` 的体检与运行时同口径（否则"体检说没有否决权、跑起来却真否决了"）；
- 别名 `expected_is_literal` 等价；真值认 `true/1/yes/on`（JSON 里布尔/字符串都可能出现）。

链路要一路透传才成立：`case_entry_fields`（CLI 取值）→ `seed_cases` → `_case_ground_truth`
（第四口径 `literal_fn`）→ `_run_matrix` → `_run_one_target` → `_attach_assertion` →
`check_assertion(literal=...)`；基线臂同样带上（两臂不同口径 = 假 Δ）。

守卫 `tests/test_literal_expected.py`（22 条），含**进程级**端到端：
同一份用例不开 `literal` 时 rc=2 且拦截话术里有这条出路，开了就进到预算阶段 rc=0。

### 5.11 第五轮续：成本闸新增"真实请求"计量（不动老开关）

`PM_MAX_LLM_CALLS` 数的是**成功调用**，而 429 退避、超时重试、通道降级都会让真实请求
跑到成功数的数倍 —— 预算"还剩很多"的同时账单已经翻倍。

没有改老开关的语义（`llm_calls` 是报告头那个被多处引用的数，改计量会让历史读数与历史
预算配置一起失去可比性），而是新增 `PM_MAX_LLM_REQUESTS`：

| 开关 | 计什么 | 语义 |
|---|---|---|
| `PM_MAX_LLM_CALLS`（老） | 成功调用 | **不变** |
| `PM_MAX_LLM_REQUESTS`（新） | 真实打到端点的请求 = 成功 + 失败 | 新增 |

两个都设时取**更严格**的（任一超限即停），理由串会指名是哪一条触发的。
口径细节：求和只算 `calls + failed_calls`，**不加** `retry_requests` —— 失败的那次
重试已经计在 `failed_calls` 里，相加会重复；成功的那次重试则同时进了 `calls` 与
`retry_requests`，所以只用前者求和是自洽的。台账不可用时退化成成功调用数（而不是 0——
报 0 会让闸静默失效）。

守卫 `tests/test_budget_requests_gate.py`（8 条：两种单位、取严、非法值、`=0` 的语义）。

### 5.13 第五轮续二：面板与桌面（此前没审过的两块）

第三路审计扫了 `pm/panel.py` + `pm/web/_panel.py` + `extras/`，三条中等、三条低。

| 缺陷 | 用户可见后果 | 修法 |
|---|---|---|
| ②考卷卡的判决色按 `veto_strong > 0` 上色 | 5 份自带模板里 3 份 CLI 是 `ok=false`/退出码 1，面板画成**绿色 ok** —— 用户据此认为考卷没问题，然后去花钱 | 判决改用 `d.ok`（与退出码同源），强判据条数降为副指标 |
| ③差分卡「约束条目」读 `structure.items` | CLI 同名字段是 `structure.constraints`，两个数可以一个 0→0、另一个 3→4 ⇒ 两边各说一套 | 改读 `constraints`，`items` 单列一行「可核对条目」 |
| ①②③端点裸调转发，失败吞成 500 白页 | `ForwardError` 里带的 stderr 尾（唯一自救线索）全丢；同时探针缓存 60 秒内顶部灯仍绿，用户分不清是输入错还是功能坏 | 抽 `_forward()`：`ForwardError`/超时/OSError → **502 + detail**；失败时立刻把探针置红 |
| 桌面「只读」会刷新 `.git/index` | 实测扫描前后 sha256 变了（工作区文件没动）—— 对一个承诺"绝不修改用户项目"的功能，动了版本库索引就是动了项目 | 子进程加 `GIT_OPTIONAL_LOCKS=0`，并用"扫描前后 index 逐字节相同"钉住 |
| 桌面文案承诺"空正文也会自动重试" | 实际空正文直接报错 ⇒ 用户以为重试过了，再点一次就是再计费一次 | 改文案并写清理由（解析失败才自动重试；空正文多半是端点/额度问题，自动重试等于替你多花钱） |
| `cliforward` 注释说"四个退出码都当正常返回" | 读起来像"2/3 的帧也当成功"（实际口径是"不靠退出码判成败，靠 stdout 形状"） | 把话写准，并在 JSON 解析失败的报错里带上退出码 |

顺带更正一处**文档与实现不符**：`pm/panel.py` 与 `pm/cliforward.py` 的模块注释都写着
"桌面窗口用的是同一份转发"，而 `extras/desktop.py` 自带 Popen（它需要句柄才能"中止"，
而 `run_cli_json` 是阻塞的），全仓没有 `import pm.cliforward` 的桌面代码。已改准。

守卫：`tests/test_panel.py` +4（502 而非 500、错误串带 stderr 尾、探针转红、超时也算失败；
判决读 `d.ok`；差分读 constraints）、`tests_extras/test_project_context.py` +1
（扫描前后 `.git/index` 逐字节相同，**做过变异验证**：去掉 `GIT_OPTIONAL_LOCKS` 当场红）。

审计**核过没问题**的面也记一笔：模板白名单牢固（`../../.env`、绝对路径、`case_templates/../.env`
全 `TemplateNotFound`）；面板所有 innerHTML 汇聚点都过 `esc()`/`n()`；桌面端没有跨线程 Tk 调用；
关窗/取消会 terminate 子进程并复位句柄，未见资源泄漏。

### 5.15 第六轮：`releases/` 三套发布服务（从未做过语义审计）

`releases/` 里的 lobster（:8791）/ taskboard（:8792）/ triage（:8793）**只被 CI 跑测试、
从没被语义审计过**。六轮里最后这一路扫了它们，17 条发现，本轮修了三条：

| 缺陷 | 后果 | 修法 |
|---|---|---|
| `cost-board.html` 的 `startLive` 写在文档结束标签**之后** | 浏览器把它当散落文本 ⇒ `startLive()` 抛 ReferenceError 被 `.catch` 接住 ⇒ 整块成本看板变成"加载失败：startLive is not defined（需经会审台访问）"，**归因还是错的**。打开即 100% 复现 | 函数搬进同一段 `<script>`；守卫钉"结束标签之后不许有残留" |
| 台账页/历史页/看板页把**用户可写字段**裸拼 `innerHTML` | `name`（公开 POST /api/orders）、工单 `subject`、`feedback.comment`、任务 `title` 都是用户写入的 ⇒ `<img src=x onerror=alert(1)>` 原样落盘，运营者一开页就执行（**存储型 XSS**） | 三页各加 `esc()` 并逐字段接上；守卫钉"裸插值形态"（对 lobster 做过变异验证：去掉一处 `esc(` 当场红） |
| `/api/notifications` 的 `count` 硬编码 0 | 实测 3 条告警时 `items=3` 而 `count=0` ⇒ 前端按 count 判空，通知中心永远"没有告警" | `count = len(items)` |

**已知但未修**（都实测复现过，写进 `docs/suite/README.md` 的审计小节）：

1. **跨进程丢数据**：lobster/taskboard 的读-改-写只靠**进程内** `threading.Lock`，
   落盘却是全量 `DELETE` + re-INSERT。两个实例并行新增 2 条实测只剩 1 条、接口全回 200；
   taskboard 的 `seq` 也在锁内自增，两进程会算出同一个 `TASK-…-001` 被 `INSERT OR REPLACE`
   静默合并。**修法有范式**（triage 已经是"同事务读改写、只 INSERT 新行"），
   但它要重写三套服务的存储路径 —— 属"改事实源"级决定，留给所有者。
2. **开 token 后三套页面取不到数**：页面 JS 从不发 `X-API-Key`（也没有拿到它的途径），
   401 又被画成"暂无订单 / 暂无数据" —— **用空集冒充没数据**。正确修法要么给页面一条
   拿令牌的途径，要么让 401 明说"需要令牌"，是产品决定。
3. watchdog/monitor 只看 HTTP 200（把 DB 写成垃圾字节仍报 ok），且自身探针在 token 部署下恒 401。

### 5.16 顺手做的一条元守卫：模块级常量必须有读取点

本仓踩过两次"写了没接线"的死承诺（`_MAX_EVENTS`、`_PER_FIELD_RE`），所以把这类判据做成测试：
`tests/test_dead_constants.py` 扫 `pm/`、`extras/`、`scripts/` 的**模块级全大写常量**，
要求全仓（含 tests/docs）至少有一处引用。

- 只覆盖常量、不覆盖私有函数：dunder 与 pydantic 校验器都是隐式调用，按名字扫会一片假阳；
- 而"常量写了没读"几乎总是真缺陷，误报率 0；
- 起手就抓到两条并删掉：`extras/desktop.py::ACCENT_SOFT`（上一轮桌面改版留下的孤儿色值）、
  `pm/schemas.py::_env_str`（唯一一个没人用的 env 助手）；
- **做过变异验证**：加一个 `ACCENT_SOFT_RESURRECTED` 常量，闸当场红。

> 第一次变异没生效——PowerShell 把 `` `r`n `` 当字面量插进文件，源码坏掉后被判据的
> `except SyntaxError: continue` 跳过了。这提醒我：**"跳过解析失败的文件"是这类守卫的天生漏洞**，
> 所以变异验证必须真的改出一个语法合法的死常量（改成用编辑工具重做后才红）。

### 5.17 现在仍然没修的

- 无（本产品侧）。`releases/` 的三条已知项在 §5.15，属所有者决定。

### 5.18 第八轮：运维盲区（探针报绿 / 轮转静默失效）

上一轮把"已知未修"里最重的两条功能缺陷修完后，剩下两条**不影响功能、但会让
"夜间无人值守"这个前提失效**的运维盲区。这一轮收掉：

| 缺陷 | 后果 | 修法 |
|---|---|---|
| watchdog / monitor 只看 HTTP 200 | 把 `orders.db` / `sessions.db` 写成垃圾字节，服务仍回 200 + `status:"ok"`（只把 `storage` 记成 `error: DatabaseError`）⇒ 探针报"活着"。**"进程活着"被当成"服务可用"** | 判据升级：200 **且** `status=="ok"` **且** `storage` 不以 `error` 开头。进程活着但自报不健康 → **只告警不重启**（重启修不好一个坏掉的库，只会把现场重启掉并陷入拉起循环） |
| 探针自己不带令牌 | 带 token 的部署里 `/api/tasks` 恒 401 ⇒ "超时任务告警"这条路径静默不触发 | 按服务的 `*_TOKEN` 环境变量带 `X-API-Key`；401 在告警里点名"探针未带令牌？" |
| `rotate_ledger.py` 三处 | ① 路径写死成 `D:\projects\prompt-master\logs\...`，换机器指向空并**当成功**；② 只轮一份台账，三套服务的 `data/*.jsonl`（天天在长的那些）完全不在范围；③ Windows 上文件被占用时 `replace` 抛 PermissionError 被 watchdog 的 print 吞掉 ⇒ 台账**从未轮转**而日志看不出 | 根路径按 `__file__` 现算；范围 = `logs/*.jsonl` + `releases/*/data/*.jsonl`；`rotate()` 返回 `{scanned, rotated, skipped, errors}`，失败进 `errors` 并由 watchdog 落告警 |
| `monitor_suite.py` 文档说会检测"口径漂移" | 代码里从来没比较过（只记录计数）——**承诺存在、实现不存在** | 删掉那句承诺，改为如实说"只记录不比较"（固定数字当基线会天天误报） |

守卫 `tests/test_suite_operational_scripts.py`（7 条）：轮转范围覆盖 `releases/*/data/`、
占用文件时 `errors` 非空且 `rotated` 不许谎报、健康判定表、**watchdog 与 monitor 对同一批
输入必须给同一个答案**（本仓老教训：同概念两套实现必然漂），以及"根路径不许写死"。
做过变异验证：把 `monitor_suite` 的 `storage` 判据改成恒空 → 两条断言当场红。

### 5.19 现在仍然没修的

- 本产品侧：无。
- `releases/` 侧：只剩**体检工具自身的读数问题**（`five_dim_audit` 固定一条误报、
  `smoke_suite` 自称三服务却不含 triage），不影响服务运行，已列在 `docs/suite/README.md`。

## 六、本轮自己造成的一处事故（如实记）

我在核对 `--fast` 的真实调用数时，直接跑了一次
`run.py --task "让 AI 分析销售数据" --fast`。非交互模式下它**不问成本确认、直接开跑**，
真花了 12 次调用（`logs/run_d67fe934ad39.json`）。

- 有用的部分：它正好验证了"12-22 次调用"的下界（本轮实测 12），也留了一份真实报告；
- 该记的部分：这类核对本来应该用 `PM_FAKE_BACKEND=progress` 或 `--dry-run`。
  该行为本身符合设计（`--json`/非 TTY 不确认），但**我在没有明确意图的情况下触发了计费路径**。
- 产物留在 `logs/`，未删除——它是证据，也是这次失误的记号。

## 七、给下一次的建议

成本/用量口径的两条主项（重试可见、流式入账）已修。剩下的三条（checklist 台阶、
成本闸单位、advisory 降级）**都是评分口径决定**，动之前先拿证据、再改判据。

本轮的通用教训有两条：
- **判据写完要先做变异验证**：`test_pump_queue_is_bounded` 与
  `test_stream_call_lands_in_the_daily_aggregate` 都是在变异之后才真正有牙的；
  第一次写的 SSE 断流用例在"撤掉修复"时仍然绿着（CPython 的 GC 替它把生成器收了），
  于是判据改成了测**同步生成器本体**。
- **复杂度棘轮会逼出更好的结构**：为了不让 `structured_call` 从 24 涨到 26，
  记账分支收敛成 `_record_retry_if(role, cond)` —— 纯记账不该挤占语义函数的预算；
  同时它顺手把两个 `if` 从调用点挪走，可读性反而更好。
