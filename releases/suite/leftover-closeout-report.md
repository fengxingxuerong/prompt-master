# 遗留问题收尾报告（2026-09-22 18:40）

处置人：Agent-AutoClaw · 来源：检修遗留（maintenance-log）+ 任务台账（taskboard）+ 缺口盘点（gap-review）
回退基线：git tag v1.4.2（commit 1266cce）· 本轮改动在其后单提交内，`git reset --hard v1.4.2` 一键回退

## 一、遗留问题清单与处置

| # | 问题 | 来源 | 等级 | 处置 | 状态 | 验证 |
|---|---|---|---|---|---|---|
| L-01 | 会审台前端未接 SSE（仍盲等轮询） | 检修遗留 18:05 | 一般 | index.html 改异步提交+EventSource 实时逐票+done 补全渲染+150s 超时回退轮询+SSE 断线兜底 | ✅ 已解决 | 页面 11286 字符含 EventSource/listenEvents；服务 v1.2.0 在跑；备份 index_v1.1.html.bak |
| L-02 | TASK-006 任务分配机制复盘已到期 | 任务台账 | 一般 | 复盘完成：12 任务 0 事故 0 越权，判定可用维持现状；3 条改进项挂下次功能窗口 | ✅ 已解决 | task-allocation-review.md 归档；TASK-006 → done |
| L-03 | 成本无可视化（G-14） | 缺口盘点 | 一般 | 新增 /api/usage-board 数据端点 + /static/cost-board.html 看板（按日/按模型聚合，零依赖） | ✅ 已解决 | API 实测 days=1 total_calls=13 成功率 100%；页面 4934 字符 200 OK |
| L-04 | TASK-002 ModelHub P1 转发开销补测（stream 401 根因） | 任务台账 | 高 | **需用户决策**：需商汤账户侧信息（token plan 限额/并发策略）才能定位 | ⏸ 需用户 | 卡点：密钥策略属用户私有信息 |
| L-05 | TASK-005 龙虾站四件套占位 | 任务台账 | 一般 | **需用户决策**：品牌名/价格/供应商/产地真实口径 | ⏸ 需用户 | 卡点：商业口径只有用户能定 |
| L-06 | OpenRouter 通道欠费 | 缺口盘点 | 低 | **需用户决策**：充值后 enabled=true 即恢复；或放弃该源 | ⏸ 需用户 | 卡点：涉及付费 |
| L-07 | 会审台 UI 逐评委实时进度（G-12 前端半） | 缺口盘点 | 一般 | 随 L-01 一并解决（SSE 逐票实时点亮） | ✅ 已解决（合并） | 同 L-01 |
| L-08 | SQLite 迁移（G-10）/ 鉴权（G-15）/ 通知推送（G-06 后续） | 缺口盘点 | 低-中 | **暂缓**：按既定原则（数据量/真实多角色/安全豁免解除时触发） | ⏸ 暂缓 | 触发条件已写在 gap-review |
| L-09 | pending 超 24h 任务提醒（复盘改进项②） | task-allocation-review | 低 | **暂缓**：挂下次会审台功能窗口，看门狗加一行检查 | ⏸ 暂缓 | 无紧迫性 |

## 二、本轮改动清单与回滚

| 文件 | 改动 | 回滚方式 |
|---|---|---|
| releases/triage/static/index.html | 前端 SSE 改造 | copy index_v1.1.html.bak 回原名 |
| releases/triage/static/cost-board.html | 新增看板页 | 直接删除 |
| releases/triage/app.py | +/api/usage-board +/static/{name} 路由 | copy app_v1.2.0.py.bak（本轮改动前快照）回原名，或 git reset --hard v1.4.2 |
| releases/suite/task-allocation-review.md | 新增复盘文档 | 直接删除 |
| TASK-006 状态 → done | 台账业务数据 | 任务台账有 undo 端点可撤销 |

## 三、验证汇总

- 会审台测试套件 10/10 passed（异步/SSE/兼容/共识/容错/边界全覆盖）
- 主项目敏感面抽样（measurement+quality）83 用例全过
- 四服务健康 4/4（8687/8791/8792/8793）
- 成本看板 API 实测：13 调用 / 100% 成功 / 5 模型分布正常

## 四、交接说明（下次会话直接接手）

1. **可立即开工**：无——可独立解决的遗留项本轮已清零。
2. **等你一句话**：002（商汤口径）→ 005（龙虾四件套：品牌/价格/供应商/产地）→ 006 充值与否；给哪个就动哪个。
3. **下次检修建议窗口**：09-29 前后，内容 = pending 提醒（L-09）+ 届时积累的其他改进项；例行健康巡检由看门狗持续覆盖，无需人工。
4. **当前版本锚点**：服务 triage v1.2.0；git 最新提交即收尾报告提交；回退一律 `git reset --hard v1.4.2`。
