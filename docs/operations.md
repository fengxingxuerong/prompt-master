# PromptMaster 文档：验证方式与发布部署

> 2026-09-17 自 README 拆出。章节标题保留原编号，正文逐字未改；
> 本文覆盖原 README 的「七、验证方式与诚实边界」与「十三、发布与部署」。

## 七、验证方式与诚实边界

三层验证，各自能证明什么、不能证明什么，说清楚：

| 层次 | 命令 | 证明 | **不**证明 |
|---|---|---|---|
| 单元测试 | `pytest tests/ -q`（1815 项、`--collect-only` 计数 2026-10-02 原稿改进模式轮 +36、同日臂感知轮 +3、同日差分/成本轮 +46、同日 CI 门禁轮 +39、同日节点提示词契约轮 +22、同日 lint 范围轮 +3、同日 live 工作流轮 +10、同日 Environment 审批轮 +2、同日测试补强轮 +21、同日 CLI 分发轮 +18、同日 graph 补齐轮 +10、同日 MCP 补齐轮 +8、同日 library 补齐轮 +17、同日 calibrate 补齐轮 +29、同日薄弱点清单轮 +2、同日纯逻辑桩轮 +17、同日降级兜底桩轮 +18、同日 10-03 卡死机修复轮 +19、同日 10-03 锚点出处轮 +11（`tests/test_call_budget.py` / `tests/test_anchor_provenance.py`，活体 1246）、同日 10-04 工程质量轮 +5（`tests/test_complexity_ratchet.py` 1 条 + 脏 `test_runs` 让报告整份崩掉的回归 4 例，活体 1251）、同日 10-04 测试质量轮 +24（`tests/test_modelhub_contention_paths.py` 13 条争用/退避分支、CLI 入口拆细后补的 10 条、死夹具守卫 1 条，活体 1275）、同日 10-05 串行锁轮 +15（`tests/test_live_run_lock.py` 15 条真端点串行锁回归，活体 1294）、同日 10-05 串行锁轮再 +1（补 `holder_path()` 与排队退避轮询这两条死角，活体 1295）、同日 10-05 串行锁轮 +6（`tests/test_live_run_lock.py` 15 条真端点串行锁回归，活体 1301）、同日 10-05 端点实证因果化 +3（那两条实证改写不增减条数，涨的是 `tests/test_preflight.py` 的出口代理披露 3 条，活体 1304）、同日 10-05 封顶接线轮 +4（`tests/test_empty_cap_wiring.py`：聚合吃封顶 / state 带 empty_cap / 渲染披露激活 / 无封顶不误报——§十七·十 照出的三条对不上的复现与钉死，活体 1308）、同日 10-05 抖动出处/可估性/d2 换算轮 +87（§十八～§二十三：`test_jitter_provenance.py` 12 条、`test_noise_measurable.py` 10 条、`test_d2_scaling.py` 9 条、`test_arbiter_seat_jitter.py` 9 条、`test_early_stop_sentinel.py` 9 条、`test_env_isolation_meta.py` 10 条、`test_guard_mutation.py` 15 条、`test_ci_gate_matrix.py` 11 条、`test_precommit_gate_set.py` 4 条、`test_hooks_actually_block.py` 4 条，连同 `test_measurement.py` 3 条改口径，活体 1395）、同日 10-05 账本读者一致性轮 +10（§二十九：`test_ledger_reader_parity.py` 7 条 —— 两本读者对同一本账本必须给同一个答案，`test_guard_mutation.py` 加 3 条变异证明身份过滤能红，活体 1405）、同日 10-05 崩溃臂别被筛掉轮 +14（§三十·一·二：`test_failed_arm_not_dropped.py` 5 条 —— 崩掉的臂不许被“噪声不可估”静默删掉，`test_history_mock_filter.py` 9 条 —— 36 条 mock 臂曾全漏判混进 Δ 统计，活体 1419）、同日 10-05 C1∧C2 重算轮 +5（§三十·七：`test_history_mock_filter.py` 再加 5 条 —— 半崩臂的回推判据三易其稿，最终挂在 `min_score` 上，并钉住那条差点被误伤的真臂`a43e44adcc9f`，活体 1424）、同日 10-05 C1∧C2 分母侧核验轮 +5（§三十·八：`test_c1c2_arm_audit.py` —— 基线 4.98 判得站得住（编造指控能在输出里查到、被批的那句提示词确在基线臂）、`min/max` 对不上钉为旧症状（时间线早于封顶接线修复）、`passed` 五条件里只有 `ci_lower` 不过且新口径下会翻转、活体 1429）、同日 10-05 空产出封顶误伤轮 +8（§三十·九：`test_empty_cap_false_positive.py` —— 合规报告照任务标注「数据缺失」却被封到 4.0 的复现与钉死，实测误伤 50 条降到 0、命中 160 降到 1（那 1 条是真空壳）、活体 1437）、同日 10-05 归档口径重算轮 +13（§三十·十：`test_archive_vs_current_code.py` —— 归档 avg 与当前代码现算差值的时间线/同源/「C1∧C2 归零」直接反证，活体 1450）、同日 10-06 判别力量具轮 +29（§三十一：`tests/test_discrimination.py` 19 条 —— AUC/留一折/可估性门槛，含「聚合侧漏掉聚类口径」「analyze 入口断线」两个必须红才行的接线断言；`tests/test_harvest_coverage.py` 9 条 —— 按人工标签格子的补采覆盖度，含同 id 跨文件去重与 `--coverage` 零写入，活体 1479）、同日 10-06 标签保全轮 +6（`tests/test_harvest_label_safety.py` —— 补采工具的默认路径端到端 + 去重集覆盖全部锚点文件 + 已确认人工分不被静默覆盖；上一轮我把 `log_dir = Path(args.log_dir)` 改丢而 1479 条全绿，这条就是给那条路发的绿牌，活体 1485）、同日 10-06 考卷身份轮 +7（§三十二：`tests/test_paper_identity.py` —— 条数不是考卷身份，聚合侧与漂移侧对"是不是同一张卷"必须同答案；老记录缺指纹时退化按条数判并把退化报出来。顺带把 `render_aggregate` 拆到掉出复杂度台账（33→32 条），活体 1492）、同日 10-06 支持度门槛轮 +2（§三十一·九：`tests/test_harvest_coverage.py` —— `--prefer-deficient` 的排序不许由 n=1~3 的命中率决定（真走一遍才发现它把 owner 最满的 `<6.0` 格排到第一），低支持度带排后但不删且必须披露；含"把门槛关掉能复现缺陷"那条反向断言，活体 1494）、同日 10-06 空开关轮 +2（§三十一·九(3) 与 ·十：`--prefer-deficient` 只排序不取舍 ⇒ 加 `--total`，并把 `ordered` 从"分段"改成"按 prefer 序列"（单带夹具看不出来，多带才暴露）；另公开更正 137 条可重判的错数（按输出内容去重后真值 2 条，零算力池 5 条）⇒ 补标签的排期要含新一轮真端点批次，活体 1496）、同日 10-06 改判队列轮 +4（`tests/test_owner_rejudge_queue.py` —— `scripts/build_owner_rejudge_queue.py` 的承重承诺：表的 human_score 一律留空、回填只写填了分的行并把该行记成 owner 亲判、有非法行则整次作废且一字节不动（全在真锚点文件的副本上验）；夹具两次补强才让"按缺口排序"与"按 id 排"可分辨，活体 1500）、同日 10-06 真实重跑轮 +10（§三十·十一：`test_failed_sample_not_a_low_score.py` —— 超时导致的废样本被当成 1.0 低分计入中位数，`min_score` 与 `ci_lower` 被一条传输失败绑架；判据从认一个字面量扩成认失败类别，并钉住真实低分仍被计入，活体 1510）、同日 10-06 评委席降级披露轮 +5（§三十·十二：`test_panel_degradation_disclosed.py` —— 半个评委席的报告看起来和双评委一模一样；判据钉住 `judge_scores` 为空的两义陷阱（配置单评委 vs 失败缺席），分母取 `judge_health.panel_size` 配置值而非倒推，活体 1515）、同日 10-06 限流形态轮 +11（§三十·十四：`test_limit_not_a_low_score.py` —— 真实跑两次 429（tpm/rpm 上限）把上一轮那个『超时』修复打穿了，废样本判据漏了限流一整类；补齐 429/配额/5xx并加结构信号兜底（judge=system 且五维全 1.0 = 压根没经过评委），活体 1526）、同日 10-06 折叠归档取证边界轮 +4（§三十·十五：`test_collapsed_archive_limits.py` —— 归档只存折叠后的结果，`sample_scores` 不带逐采样的 issues；上一节据不存在的前提推出的『case#0 是废样本』已翻案（那个 1.0 是真低分，剔掉才是错的）。含变异验证：把判据改成『分数≤1.0 即废样本』后该守卫转红，活体 1530）、同日 10-06 评委慢归因轮 +5（§三十·十六·十七：`test_judge_latency_rootcause.py` —— 评委慢的根因查清：不是端点排队，是输出 token 太多（评委 12143/次 vs target 1725/次，生成速率全角色同档）；更查出 `evaluator_b` 是**席位特定**劣化（同轮内只有它慢到 10~18 倍），这才是双评委反复失败的真凶。判据换过三次都被真实数据打脸，最终用轮内相对比较。四条守卫各做过变异验证，活体 1535）、同日 10-06 端点归因更正 +1（§三十·十七：两个评委走**同一个** base_url，差别只在模型——所以『换端点』是错的方向，该换二号位的模型；这条守卫刻意直读 .env 原文，因为 CI 把所有 URL 覆盖成同一死地址，用 build_config 的话断言恒真、守卫变装饰品，已做变异验证，活体 1536）、同日 10-06 失败记账 +8（§三十·十八：`test_failed_calls_are_recorded.py` —— 失败的调用以前在台账上**完全不存在**，`_record_usage` 只在成功路径调用，于是『真的慢』和『一直失败』在台账上长得一模一样，排障只能翻 errors 猜（本会话已因此查错三轮）。现把 failed_calls / failed_latency_ms / retry_requests **单列**，绝不并进 calls——并进去会让按成功调用算的速率判据失真。8 条里 7 条修复前确实红，活体 1544）、同日 10-06 报告披露失败 +5（§三十·十八：`test_report_shows_failures.py` —— 把 §三十·十八 那三个台账字段接进报告的用量段（失败次数/失败耗时/重试请求数单列一张表，并明写『与成功次数不可相加』，否则读的人会把 16+2 当成 18 次调用）。5 条里 2 条修复前红，另 3 条是防将来过度修正的回归判据（不许污染成功计数、不许在没失败时渲染空表、不许在旧归档缺字段时炸），活体 1549）、同日 10-07 白烧披露 +6（§三十·十九：`test_wasted_delivery_is_disclosed.py` —— 真跑 run_41a833fb9b41只花 3 次调用就 early_stopped：optimizer 烧掉 32850 输出 token / 207 秒、台账每个字段都正常（HTTP 200 照记 token），可最终提示词 0 字符。⇒ 台账按成功/失败二分，而『成功但空』两类都不是，§三十·十八 也抓不到它。现从 trace 的 *_empty 事件把它与用量对上，在报告里单列披露（映射不出的 node 只报事件不编金额）。6 条里 3 条修复前红，另 3 条防过度修正（正常轮不告警/未知 node 不编钱/缺 trace 不炸），活体 1555）、同日 10-07 不许凭空归因 +6（§三十·二十：`test_no_invented_attribution.py` —— 把 early_stop_reason 里写死的『端点截断/思考耗尽』改成『未观测到截断/额度证据，根因待查』。起因：§三十·十九·二 我照着这句臆测归因推断出『复现了推理耗尽』，随后被 token 分布推翻——空产出横跨 1493~16425 全分布，与 token 量无关；且 max_tokens 实际是 24000 不是 32000。实测空产出率 **19.7%**（66 轮 13 轮），W40/W41 达 33%/50%，不是随机长尾。归档里写死没证据的归因比不写更坏——它长得像证据。6 条里 4 条修复前红；判据只管『空内容』类早停，不波及有观测依据的『墙钟预算用尽』。活体 1561）、同日 10-07 空产出加重试 +6（§三十·二十一：`test_empty_delivery_retry.py` —— 实测 §三十·二十 那个 19.7% 空产出率下，optimizer 拿到空串就整轮早停。实测 check_prompt_quality("") → ok=False『提示词为空』，所以空串**本来就会触发**那次带 hint 重试（calls=2），问题只在于第二次还空就放弃。现给空产出额外最多 2 次机会（总 3 次），而**质量问题仍只重试一次**——反复重试会让提示词越改越偏。守卫还钉住两个被自己测试逮到的 bug：重试成功却返回第一次空产出的 meta；额外机会用满后仍掉进质量门重试、多发第 4 次纯重复请求。6 条修复前红（红法是 collection ImportError，判红须同时看 collection 是否炸）；活体 1567）、同日 10-07 真跑取证 +4（§三十·二十二：`test_run_aa98d99e66b5_findings.py` —— 带空产出重试保护跑了第一轮『passed』：run_aa98d99e66b5，4h33m / 81 次调用，avg 8.37 / 基线 6.78 / Δ+1.59，但 3/4 例评委缺席 ⇒ 该分数**不可引用**。真正的产出是 §三十·十八 那三个字段首次在真实跑里完整工作：15 次失败调用、累计 **186 分钟**，占整轮墙钟 **68%** ——在 §三十·十八 之前这 186 分钟在台账上完全不存在。另发现墙钟预算 600s 实际耗了 10581s（超 17 倍，连接层挂起没被兜住），这是这轮跑 4.5 小时的直接原因。活体 1571）、同日 10-07 DNS 根因 +3（§三十·二十三：`test_dns_outside_timeout.py` —— 查 §三十·二十二 那个「预算 600s 实际 10581s」：实测传输层四形态，拒绝连接 2.08s / 黑洞 300.01s / 不可路由 IP 21.05s 全被 timeout 精确兜住，**只有 DNS 解析挂起跟到底**——声明 timeout=300s、DNS 挂 1800s ⇒ 实测 **1800.0s** 返回，一秒不差；`socket.setdefaulttimeout` 同样无效。根因：httpx 的 timeout 覆盖 connect/read/write/pool，而 `socket.getaddrinfo` 在这些阶段**之前**，`WallBudget` 又是「发起前」检查 ⇒ 一次 DNS 挂起就让预算形同虚设。已把该隐含假设写进 `WallBudget` 文档字符串。**本节没修任何东西**，三个修复方向都未验。活体 1574）、同日 10-07 DNS 预解析落地 +6（§三十·二十三·五：`tests/test_dns_outside_timeout.py` 再 +6 —— 方向 1「预解析 + 缓存」落地：`PM_DNS_BUDGET`（默认 5s，0=关）挂在 `_invoke_with_rate_limit_retry` 入口，解析挂起在预算内快速失败、不再跟满 timeout；解析报错不上抛（真实请求保持原样异常与重试分类）、成功进正面缓存、IP 字面量与无 base_url 不开火（测试零出网的构造性保证）。集成判据钉 counter==0（修复前该形态 counter=1 跟满 timeout）。·三 那 3 条库行为判据原样保留——修复在 httpx 之上，不改库行为。6 条修复前红（红法：预解析不存在时走到 get_llm 桩的 AssertionError，或 elapsed 跟满挂起时长）。活体 1580）、同日 10-09 评估缓存采样键轮 +4（`tests/test_eval_cache_sample_isolation.py` —— 真端点 `run_d01e8a9168d4`（4 用例 × 2 采样）实测：低温端点让 **4/4 用例两次采样的 target 输出字节相同**（归档 `cache_hit` 全 False，两发都是真调用），于是第二次评估按内容寻址命中第一次的对象（聚合日志"缓存命中 4"恰等于用例数），`score_spread` 按构造为 0 ⇒ 报告印 `噪声=0.00`，而它的真实含义是"第二次根本没打分"；`ci_lower` 正是从噪声项算的，所以这不是报表问题而是判定问题。修法沿用 `key_for_target` 的 `|s{sample}` 先例，把 `sample_index` 并进 `key_for_eval`。4 条里 2 条修复前红（`TypeError: key_for_eval() got an unexpected keyword 'sample_index'` / 第 2 采样 `n_calls=1`），另 2 条是反向护栏（同序号同内容必须仍命中——否则这次改动只剩成本没有续跑收益；换 prompt 不得串用，H1 那半边不许被碰坏）；把键里的 `|n{sample}` 摘掉复红已验（逆操作还原，未用 `git checkout`）。活体 1584）、同日 10-10 校准漂移崩轮 +5（`tests/test_calibrate_drift_rep_row.py` —— 照报告建议跑 `run.py calibrate --judge evaluator --repeat 3`：11 锚点 × 3 复现 ≈ 44 发真实调用、55 分钟，**全部打完后**崩在 `pm/cli/calibrate.py:496 float(prev["bias"])` ⇒ `KeyError: 'bias'`，stdout 0 字节、账本一条没写——`_save_entry` 排在 `_drift_vs_history` 之后，一个纯展示用的"与上一轮比"把整轮实测吞了。根因：账本里 09-29 那条是**复现性行** `{judge, rep_range_mean, ts}`，本来没有 bias/mae，而 `_comparable()` 只看指纹字段（"老记录缺指纹按可比处理"这个设计是对的）就把它当成了校准基线；读侧早就认识这个形状（`test_d2_scaling.py:59` 明写），**写侧的漂移比较从没处理过它**。修法：新增 `_has_calib_numbers`，`prev` 与 `changed` 两条选取都只认带数字的行（`changed` 分支同样硬取 `changed["bias"]`，漏一条就还崩）。5 条里 4 条修复前红（现场即 `KeyError: 'bias'`，含入口级 `_calibrate_command` 那条：断言"实测必须入账"），第 5 条是反向护栏（同口径真校准行照旧出 delta_bias/delta_mae/drifted，别把漂移检测修成静默停用）；摘掉守卫 4 红、还原即绿。**未做**：把 `_save_entry` 提到漂移比较之前——根因已闭，重排会牵动"先存后比"的语义，留作结构脆弱点记录在案。活体 1589）、同日 10-10 目标输出归一 +2（`tests/test_target_output_normalized.py` —— SenseNova 端点对**每一次** completion 都前缀两个换行，"只回答一个数字"也返回 `\n\n4`，评委逐条记「前导空行」假瑕疵；入库前 `_normalize_output` 抹掉端点自带的首尾空白（`pm/nodes/execute.py`）。活体 1591）、同日 10-11 巡逻线程接线 +2（`tests/test_patrol.py` —— 线程属于"服务真的被拉起来了"，不属于"有人 import 了这个模块"：`pm/server.py` 不再 import 即起巡逻线程，实测 import 前后线程数 1→2，CLI 的 submit/wait、MCP、每个 `from pm import server` 的用例都白拿一个线程，`--workers N` 下还按 N 翻倍。活体 1593）、同日 10-11 控制台脚本语法 +1（`tests/test_web_console.py` —— 组装出的整页 `<script>` 交给 `node --check`：JS 是整段编译的，任一处语法错会让浏览器丢掉整个脚本，而 Python 侧断言全都看不出来；含"锚点漂了只截到半页"的防呆。活体 1594）、同日 10-11 pool 配置防线 +22（`tests/test_modelhub_pool_config.py` —— 池子的失败不只在网络上，更多在"配置那一刻"：密钥占位符没解析、配置写坏、运行期被删/改坏，共同危险是**静默**；热重载保护与"修好的配置下一轮必须被拾起"在此钉住（进程内、零出网）。活体 1616）、同日 10-11 http 层失败语义 +22（`tests/test_modelhub_http_layer.py` —— `ModelHub._http_post_json` 这 30 行是模块 docstring"失败语义"承诺的唯一兑现处：连接错误/超时/401/403/404/429/5xx/假成功都必须判本模型不可用；401/403/404/429/5xx 要把 status 与 reason 带上去，丢了就把可切换故障降级成未知故障（假 urlopen，零出网）。活体 1638）、同日 10-11 历史同任务显著性 +16（`tests/test_history_task_stats.py` —— 与"哪些臂算 demo/崩溃"（`test_history_mock_filter`）互补的另一半，准入之后的统计语义：崩臂剔完可信臂不足 2 条不给判定、n<5 必须带"样本少"、Δ 恒等时 SE=0 的退化区间按真实行为钉住免得被"改好"、崩臂不参与统计但必须点名、JSON 单对象。活体 1654）、同日 10-11 读端点鉴权收尾 +1（`tests/test_api.py` —— 设了 `PM_API_TOKEN` 后 GET history/status/report/race* 全部受 `require_token` 保护（以前只有写端点被挡：任务原文与完整报告对摸到端口的人可读）；`pm/server.py` 与 `pm/modelhub/server.py` 共 3 处 token 比较改 `secrets.compare_digest`；rest-api.md「读端点不要求」旧表述同步更正。活体 1655）、同日 10-11 账本结构守卫 +1（`tests/test_evaluation_md_structure.py` —— §三十·十一 落盘时自编辑探针把要追加的脚本原文（`import re` 到未闭合三引号共 5 行）连正文一起贴进账本，§三十·十二 起约 1100 行泡在未终止字符串里、markdown 结构断裂（已修复，见该文修复注）；守卫钉三条事故签名（围栏外脚本形态 / 三连双引号 / 尾换行），对 HEAD 损坏版变异验证 5 处全红。活体 1656）、同日 10-11 覆盖率薄弱点收尾轮 +13（`tests/test_streaming_edge_branches.py` 7 条 + `tests/test_calibration_main_entry.py` 6 条 —— 薄弱点清单 C 里"补得起但还没补"的两块：streaming.py 的 11 条 miss（HTTPError 响应体读不出仍抛带状态码的 GatewayError / URLError 的 timed-out reason 判"读超时" / 首个 data 块之后的垃圾行丢弃 / resp.close() 抛错不污染已透传的流 / `sse_first_content` 的非 data 块·max_chars 停止消费·坏 JSON 跳过）与 calibration.py 的 `main()` 尾段（加载失败 rc=2 / 空样本 hint / pending 出处披露 + [SKIP] / `--repeat` 分支 / 全部失败 rc=1）与 `__main__` 守卫两行（exec 以 `__main__` 之名跑 `--help`，相对导入靠 `__package__` 解析）；假上游 + 全桩评委，零出网。活体 1669）、同日 10-11 MCP 转发自愈 +1（`tests/test_mcp_server.py` —— 读端点纳入鉴权的同轮自查抓到的真回归：`_http_json` 转发 GET status/report 不带任何 key，设了 `PM_API_TOKEN` 的部署里 MCP 的长任务工具会被**自家门禁 401 挡死**；修为每次转发现读 `PM_API_TOKEN` 附 `X-API-Key`（晚绑定，与 log_dir 同哲学），未设时不带头、本地请求保持干净。活体 1670）、同日 10-12 history 分代轮 +9（`tests/test_history_generation.py` —— `run.py history` 的同任务 Δ 显著性一直只按 **task 字符串**分组，而评委换型（flash-lite → deepseek-v4-flash）等于换算尺的刻度与噪声，跨代轮次被混进同一个均值读出的"显著"对当前仪表并不成立（`calibrate --aggregate` 早已按 model×rubric 分代，这一侧漏了）；现按 `(task, 评委尺子身份)` 分组，尺子身份取自 `run_*.json` 的 `judge_health.models`（排序拼接、与字典顺序无关），缺记录的旧归档归 `UNKNOWN` 桶**不合并**；文本渲染与 `--json` 均带 `judge_gen`，同一任务跨 >1 把尺时给一句"不同尺的行不能读成一列"的警告；活体 1679）、同日 10-12 代理隔离轮 +1（`tests/test_env_isolation_meta.py` —— 第四类环境污染是**不带 `PM_` 前缀**的代理变量：本机 `http_proxy` 让 `test_httpx_timeout_does_not_bound_dns` 假红（httpx 默认 `trust_env=True` 把请求交代理，探测域名 `pm-dns-hang-probe.invalid` **从未**经过本地 `socket.getaddrinfo`，"DNS 挂起 1.5s"这个前提整个没发生；实测判据读数 1.02~1.14s < 1.5s，清掉代理变量后 1.94s 照设计通过）。判据没错（库行为没变，§三十·二十三 原样成立），错的是测试进程继承了与结论无关的环境 ⇒ 修在 conftest 新增 `no_ambient_proxy` 夹具（4 个名字 × 大小写 = 8 条全清），而不是那个测试文件里——**以后任何**新建 httpx/urllib 客户端却不关心代理的用例都有同一个雷。守卫两条：① 进 `test_all_isolation_fixtures_are_still_autouse` 清单（夹具删掉或去 autouse 会红）；② 新行为守卫**把事故环境注入子进程**——CI 上无代理、直接跑永远绿，注入后夹具失效即红。⚠️ 注入的代理必须是**会立刻回话**的假代理（本进程起 socket 接受即关）：实测拿死端口（127.0.0.1:9）冒充时，某些网络环境下连死端口并不快速失败而是挂到超时，"DNS 挂起 1.5s"反而被凑够 ⇒ 判据照绿、守卫变假闸。⚠️ 变异锚点要删 `HTTP_PROXY`/`http_proxy` **大小写两条**：Windows 上 `os.environ` 大小写不敏感（给 `http_proxy` 赋值读到的键是 `HTTP_PROXY`），只删大写那条在本机是空操作——变异拿不到红。⚠️ 子进程的 `PYTEST_DEBUG_TEMPROOT` 显式指进 tmp_path：仓库根 conftest 默认把 pytest 临时根设进检出目录，而"检出目录不可写"的机器上子进程会死在**与代理无关**的 PermissionError 上（根 conftest 用 setdefault，显式设值它尊重；CI 是 Linux，根 conftest 本来就不设）；活体 1681）、同日 10-13 锁文件 open 退避轮 +2（`tests/test_modelhub_contention_paths.py` —— 跨进程台账抓到了第二形竞争：**锁文件自身的 `os.open`** 在并发创建同一新文件时会被瞬态 EACCES 拒（最小探针：两进程各自 open+lock+unlock 200 轮，6 轮里 2 例首个 open 即 errno 13；集成侧同形：两进程各发 60 把压测 5 轮里 2 例签发者当场崩 rc=1 ⇒ `test_two_processes_do_not_lose_issued_keys` 在这台机器的全量套件里必红、单文件必绿）。修前 `_process_lock` 只给"拿字节锁"退避、没给"打开锁文件"退避——与读路径 `_load_json_or` / 写路径 `_atomic_write` 的"瞬时占用退避"是同一类事（它们有、它没有）。现 open 与拿锁共用同一 timeout 预算，超时显式 ConfigError 点名锁文件。修后：25 轮 ×2 进程 ×60 把零失败；守卫两条（被拒一次必须让必须成功 / 一直拒必须显式报错），变异验证撤掉修复两条全红；活体 1683）、同日 10-13 logs 审计轮 +4（`scripts/logs_usage.py` + `tests/test_logs_usage.py` —— `logs/` 实测 169MB / 15221 文件且无审计口径：新增**只读**用量审计工具（按类别汇总 + 标出可回收的内嵌 venv/缓存目录，从不删除——证据类 `run_*.json`/`report_*.md` 被 `docs/evaluation.md` 按 id 引用，与死重必须分开处置）。实测可回收 102.20MB（60% 全是 `archive/2026-09-17-交付归档/docker_sim_*/venv` 一族内嵌虚拟环境）。守卫 4 条：分类口径（证据类不进可回收）/ 零写入快照 / `--json` 单对象 / 缺目录 rc=2；活体 1687）、同日 10-13 约束段标题形态轮 +3（`pm/quality.py` 的 `count_constraints` 此前只认 `[约束]` 方括号段，而 `structure_items` 早已兼认 Markdown 标题段（其 docstring 明写"真实交付物里比方括号更常见"）——实测同一份内容 `## 约束` + 10 条编号 ⇒ `count_constraints=0`、`structure_items=10`，**预算闸（≤8）对标题式提示词整条失效**，与本模块"绕过成本必须为零"自相矛盾；修法是两条路径共用同一套段头判定（与 structure_items 同一行）。`run.py check` 端到端：同一份提示词从 `constraints:0` 变为 `constraints:10` 并正确触发 `constraint_overload`。守卫 3 条（标题三形态计数 / 风格无绕过成本 / 标题式误杀防线），撤掉修复三条全红；活体 1690）、同日 10-13 注入技法库轮 +3（PD-1：`pm/injection_pack.py` 把伪造指令的**装束从 1 种扩到 10 种**（伪造系统通知/工具返回/定界符逃逸/英文系统更新/零宽混淆…），按 (run_id, 序号) 打乱后确定性轮换——同 run 复跑稳定（用例文本进缓存键，不许每跑换装束）、同 run 内不重样、跨 run 铺开覆盖；`_injection_survival` 的 details 带技法名（审计能回答"哪种打法得手/被拒"）。适用性裁剪写进模块 docstring：只收"能夹在正常数据里的单轮文本注入"这一族，PII/有害内容那几族（内容审核）不收。三条不变量守卫：渲染后码在场/点名校验码/无"测试·mock"元词自曝；变异验证两条（轮换退化成永远第一种 → 覆盖守卫红；details 去技法名 → 详情守卫红）。⚠️ 顺带修了一条会随装束轮换假红的旧断言（`test_test_node_flags_hijacked_output` 原来认字面短语「只输出校验码」，改认"码在输入里"）；活体 1693）、2026-10-13 编码与临时根轮 +16（`tests/test_console_gbk_entry.py` 7 条：cp936 入口守卫的静态接线 + `PYTHONIOENCODING=gbk` 行为复现，linux job 也能量到；`tests/test_temp_root_fallback.py` 9 条：临时根"先探再选"与检出外退路，活体 1709）、同日考卷体检轮 +16（`tests/test_cases_audit.py`：桶分区守恒 + 与执行器 `check_assertion` 的**差分**判据 + 上限常量的源码级钉（黑盒测不出抄件，植入 `max_cases=8` 变异后本文件全绿，只能按 `test_version_single_source` 的先例钉字面量）；三条变异各自验红后还原；同日补两条进程内入口用例（子进程那批不计进覆盖率分子，新增分支零覆盖就是当时量到的 −1.3 个点），活体 1727）、同日 MCP 补齐轮 +7（`cases_check` 工具的 argv 拼装 / 中转文件落临时目录且回读等价 / 坏 JSON 回结构化错误 4 例 / **起子进程发 tools/list 取真实注册表**再对 README 与指南两份抄件 —— 实测当时是「指南 9、README 12、注册表 12」三种说法，源码数装饰器抓不到 FastMCP 没收下的一种，所以判据走协议层，活体 1734）、同日 10-14 插件面板轮 +41（`tests/test_panel.py` 28 条 —— 五份自带模板逐份把面板读数与当场 CLI 读数对齐（判据不在面板里）、`bucketBars` 的 div 必须配平、转发底层加固项与 `pm/mcp_server.py` 逐条相等、`template=../../.env` 走白名单而不是拼路径；摘掉 legend 的 `</div>` 做过变异，本文件当场转红。`tests/test_temp_root_fallback.py` +1 —— 自带 pytest.ini 的 releases 套件不许再把临时根写死（lobster/triage 在本机各红 17/38 条的那条单点，改成复用仓根"先探再选"后 18/46 条 rc=0）。`tests/test_cli_units.py` 的子命令分发表手工补 panel。面板上三处缺陷是浏览器里点出来的、不是读代码读出来的：legend 的 div 漏闭合把后面的块全吞了、交付报告被 5 秒轮询刷掉、diff 把服务器临时目录路径显示给了用户；活体 1775）、同日 10-14 桌面端轮 +26（`tests/test_desktop.py` 40 条 + `tests/test_cli_units.py` 的 dispatch 表补 desktop —— 渲染层是纯函数（无显示器也能测），窗口层共用**一个** Tk 根：每条用例各建各的根，跑到第 18 条 本机直接 `Windows fatal exception: 0x80000003` 整进程没了（rc=3，不是某条用例红）。两条只有真开窗口才现形的缺陷：①探针线程里调 `root.after(0, …)` —— Tk 的 after 同样不是线程安全的，症状是底部两盏灯永远停在"检测中"，摘掉队列回投这条守卫当场红、还原即绿；②`_argv()` 在 worker 线程里读 Spinbox/Entry，抛 TclError 被后台线程吞掉，改成主线程取值。③入口本身：第一版 `app_main` 忽略 argv，实测 `run.py desktop --help` **开出窗口卡在 mainloop**（rc=124、stdout 0 字节），补 argparse 后把 desktop 加进 test_cli_guards 那两条手工清单（它们不读 `_SUBCOMMANDS`，加了子命令不会自动覆盖）。；同日 10-14 接口深化轮 +12：④接满参数面（原稿模式 `--prompt-file` 取①的正文、用例集取②的模板或粘贴内容 + `--assert-mode`、`--samples`、快速档），且**勾了快速档就不传** `--samples/--max-iter` —— 两个来源抢同一个开关会让读结果的人分不清哪个生效；提交前先在 worker 线程跑 `--dry-run`，确认框里写的是这次参数下的预估区间而不是固定文案，于是"取消"那条断言也跟着改对：取消 ⇒ 只有免费报价那一发、没有任何计费提交。⑤账本四个零调用按钮（history / calibrate --aggregate / library --recommend / gate）。真跑一次才照出的字段名错：`calibrate --aggregate` 的 pooled 里**没有** `r`，一致率与 κ 在 `pooled.decision` 下 —— 第一版凭记忆写字段名，界面直接印出三个 `None`；重写后连 `estimable=False`、`papers_differ`、被排除的崩溃臂这些否定性标记一起显示，并钉"输出里不许出现 None"。深色配色从 `vista` 换 `clam`：原生主题不理会 background/foreground，截图里按钮成了一块块浅灰。`pm/cliforward.py` 抽出转发与模板白名单（面板与桌面共用，第三份会漂）；活体 1803）；这条数由 `test_test_count_copies_match_the_live_collection` 对着活体收集钉，抄在三处就要三处同改） | 评分公式、短板拦截、**用例数不足不判达标**、JSON 解析、路由、降级重试、注入隔离与定界符越界、双评委合并/仲裁、**单评委结果不污染双评委缓存**、并发排序、缓存命中/淘汰与**配置指纹**、提示词质量门（含领域词不误杀 / 约束超载 / 定界符配平）、null 容错、演示模式隔离与产物落盘、**服务层限流**（滑动窗口 / 429+Retry-After / 赛马按任务数计费）、**两轮澄清提问语义**、**MockGen 场景覆盖校验（含注入用例）**、**用量台账**、**入口编码兜底**、**节点提示词结构契约**、**控制台渲染 XSS 防线与安全响应头**、**任务记录存储层**（内存/SQLite 行为一致 + 跨实例可见 + 并发写不丢账）、**REST API 七个路由与 404/422/401 分支**、**CLI 入参护栏与 `--fast` 快速档**、**LLM 限流退避/降级通道/记账分支**、**缓存落盘与淘汰异常分支**、**SqliteCache 多进程共享缓存**、**评委输出形状容错**（对象数组 issues / JSON 字符串 / 缺尾键 / null 数组 / **键名被按 description 同义改写**，都不该作废整条评估）、**取证先于打分的字段序**、**双向盲评与位置偏置计数**、**fixed–broken 逐条得失记账**、**对比切片与高方差用例优先**、**Δ 的基础设施有效性闸**（零有效样本报「不可采信」）、**注入存活按用例计数与代码派生校验码**、**评委同源检测**、**仲裁分数出处披露**（N/M 例出自仲裁者一人时报告必须说不，别让读者以为那是双评委共识）、**自报分与加权分相等时披露探测器失效**、**满分声明告警**（五维全 ≥9.5 却仍列 issue／或零 issue，只提醒不否决）、**评委复现性 `--repeat`**（绕缓存重复打；绕缓存不许整体替换 CallHook，否则自测会打真端点）、**仲裁失败回退仍记全出处账**（`conservative` 也要带 `judge_scores` 与分差）、**仲裁 prompt 的「给看分数」与「要求忽略」必须共生**、**出处字面量跨 judge/report 一致**（改了值不许让那行静默失效）、**评估缓存的 rubric 指纹**、**校准账本的口径指纹**、**净增量对退化基准不失真**、**`releases/*/tests` 里每一套都必须同时出现在 CI 两个 job 与下面的门禁块**、**重试层叠收敛与墙钟预算**（SDK 隐式重试被钉成 0、超时/5xx 按实测文本与状态码分类、`PM_CALL_BUDGET` 在每次真实发起前检查并把单发 timeout 夹进剩余预算；端点级实证：装死端点收到的请求数 == 我方可见重试数） | 任何与模型能力相关的结论 |
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

| 范围 | 接入门禁前 | 补 modelhub 测试后 | 复测① | 复测②（补运维路由） | 复测③（补切换链） | 复测④（原稿模式轮） | 复测⑤（逐模块补到 100%） | 复测⑥（降级兜底桩） | 复测⑦（薄弱点收尾） | 门禁地板 |
|---|---|---|---|---|---|---|---|---|---|---|
| `pm/` 全量 | 84% | 88.5% | 90.43% | 92.44% | 93.49% | 93.37% | 95.19% | 95.80% | **97.15%** | 92 |
| `pm/` 去掉 modelhub | 95.6% | ~96% | 94.9% | 94.9% | 95.6% | 94.91% | 96% | 96% | **96.82%** | —（被全量线覆盖） |
| `pm/modelhub/*` | 33.5% | 58.6% | 69.8% | 81.0% | 87.1% | 86.37% | 89% | 93% | **99.05%** | 82 |

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
> 复测⑥→⑦（2026-10-11，全量 1669 条 rc=0）：清账薄弱点清单 C 的"补得起但还没补"两块——
> `streaming.py` **89% → 100%**（`tests/test_streaming_edge_branches.py`，假 urlopen），
> `calibration.py` 的 `main()` 尾段与 `__main__` 守卫补完（**91% → 95%**，
> `tests/test_calibration_main_entry.py`，全桩评委 + exec 守卫）；modelhub 分项同时吃到
> 在途的 pool 配置/http 层测试（93% → 99.05%）。剩 `pool.py` 331-367（真 urllib 段）
> 与 calibration 的零散 `except` 单行，均按原判"改到时顺手补"。

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
  - ~~低于 90% 的只剩 `pm/calibration.py` **89%**（56 条）与 `pm/modelhub/streaming.py` **89%**（11 条）~~
    **两块已于 2026-10-11 补齐**（`tests/test_streaming_edge_branches.py` / `tests/test_calibration_main_entry.py`，
    假上游 + 全桩评委，零出网）：streaming 的 11 条 miss 清零（收尾 `except` / timed-out 判读 /
    垃圾行 / close 抛错 / `sse_first_content` 三边）；calibration 的 `main()` 尾段与
    `__main__` 守卫补完。calibration 剩的是零散单行 `except` 兜底与 CLI 分支，
    改到相应代码时顺手补即可，不值得专门一轮。
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
- ⚠️ 2026-10-13 又被同一族问题打了一次，这次是**反过来**：`.pytest_tmp/` 自己的 ACL 坏掉
  （`ls` 与 `icacls` 都"拒绝访问"），pytest 建编号目录时在 **setup** 就被拒 —— 一次全量跑
  **1693 条全部 ERROR**、`--cov` 报 23%，而 ruff / mypy / `--selftest` 全绿。写死一个路径
  并相信它，等于把验证能力挂在宿主状态里。现在 `conftest.py` 改成**先探再选**：探测包含
  "建得出父目录 / 建得出里层 / 能 iterdir 枚举"三步（只测前两步会把这种"能 stat 到、读不动"
  的坏法判成可用），探不到就退到检出外（`<盘符>\tmp\pytest-prompt-master`，必须绝对路径 ——
  上一版拼成盘相对 `D:tmp` 时把目录建进了仓库），并把退让**印在 stderr**。看到那行警告
  不是测试坏了，是环境坏了；两者在日志里分得很开。守卫在 `tests/test_temp_root_fallback.py`。

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
形态 D · 插件面板              python run.py panel    # 只绑 127.0.0.1:8765 的小窗口，零调用三张卡不需要 Key
形态 E · 桌面窗口              python run.py desktop  # Tkinter 原生窗口：不开浏览器、不起服务、不要 token
                               #   五个标签：①体检 ②考卷 ③差分 ④闭环（参数面 + 先报价）⑤账本与仪表（全零调用）
```

形态 D 的存在理由：①②③ 三张卡（体检 / 考卷有几票 / 两版差分）是**当场出判定、零调用、
不花一分钱**的那一半能力，而它此前只存在于 CLI 与 MCP 里 —— 富控制台（形态 B 的 `GET /`）
反而没有入口。面板不产生第二套判据：三个 POST 全部 subprocess 打同一条 `run.py`，
由 `tests/test_panel.py` 把"面板读数 = 当场 CLI 读数"逐份模板对过。
`frame-ancestors 'none'` 意味着它不能被 iframe 嵌进别的应用，"插件"指一条命令起一个本地窗口。
形态 E 是同一四张卡的**原生窗口**版（Tkinter，标准库，无新依赖）：不 listen 任何端口，
所以没有 token/CSP 那一层；判据仍来自 `run.py`（`pm/cliforward.py` 是面板与桌面共用的
唯一转发口）。选哪个：要在别的机器/容器里远程看 → D；只要一个本机小工具 → E。

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

