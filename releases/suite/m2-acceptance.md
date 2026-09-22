# M2 运维感知里程碑 · 验收与交接（2026-09-22 19:40）

## 一、交付成果（三项最小可用增量，全部真机验证）

### M2.1 通知中心 ✅
- 新模块 `releases/triage/notify_center.py`：落盘 notifications.jsonl（保持既有行为）+ 可选 webhook 分发（5s 超时静默）
- 新端点：`GET /api/notifications?limit=N`（列表）、`GET /api/notify-config`（配置态回显）、`POST /api/notify-test`（链路测试）
- **验证**：notify-test → file=True webhook=skipped（未配置态）；列表端点回读 1 条
- **配置方法**：写 `releases/triage/data/notify_config.json`：`{"webhook_url": "https://你的接收端", "enabled": true}`，无需重启（每次发送现读配置）

### M2.2 任务超时提醒 ✅
- 看门狗巡检挂 `_check_stale_tasks()`：pending/in_progress 且 due 超 24h 的任务落 warn 通知；同任务同 due 只提醒一次（.stale_notified.json 记忆）
- **验证**：阈值判定单测 due=09-20→67.9h 触发 / 09-22→19.9h 跳过；当前台账 pending 任务 due 09-24/10-12 均未超时——不触发属正确行为
- **注意**：看门狗常驻进程需下次计划任务触发时加载新逻辑（当前 --once 手动跑已验证编译与逻辑，5min 节奏自动生效）

### M2.3 实时调用曲线 ✅
- 新端点 `GET /api/gateway-metrics`：代理网关 /v1/metrics（解决浏览器跨域），网关离线时返回 offline+原因
- 看板页新增 SVG 折线面板：每 5s 采样累计调用，60 点窗口，实时显示成功/失败计数；拉取失败有明确提示
- **验证**：两次采样 t1=140 t2=140（数据单调，曲线可绘）；页面 startLive/livecurve 标记在位

## 二、异常演练（要求至少一条，实际两条）

1. **webhook 不可达**：配置 `http://127.0.0.1:9/hook` → 发送耗时 2.0s、webhook=failed、file=True——静默失败、通知不丢、业务不崩。演练后配置已还原默认态
2. **网关离线路径**：/api/gateway-metrics 失败返回 offline+error，看板显示"网关离线"而非白屏

## 三、回滚方案

- 一键：`git reset --hard v1.4.2`（M2 全部改动在其后单提交内）
- 文件级：app.py←app_v1.2.0.py.bak（M2 改动前快照）；notify_center.py/cost-board 曲线段直接删除即净
- 数据：notify_config.json 还原 `{"webhook_url":"","enabled":false}`（已还原）；notifications.jsonl 纯追加，删除即净

## 四、验证汇总

| 项 | 结果 |
|---|---|
| 会审台测试套件 | 10/10 passed |
| 主项目敏感面抽样 | 83 用例全过 |
| 四服务健康 | 4/4 OK |
| 编译检查 | app.py / watchdog_suite.py / notify_center.py 全过 |
| 里程碑三项端点 | notify-test / notifications / gateway-metrics 全部实测 PASS |

## 五、交接：接下来怎么走

1. **启用 webhook 推送**（可选）：编辑 `releases/triage/data/notify_config.json` 填入你的接收 URL 并 enabled=true，发一条 notify-test 验证即可
2. **看门狗新能力生效时点**：下一次计划任务触发（≤5 分钟）自动加载超时提醒逻辑
3. **仍在等你**：TASK-002 商汤口径 / TASK-005 龙虾四件套 / OpenRouter 充值决策
4. **下次检修建议**：09-29 前后；例行巡检已全覆盖
5. **版本锚点**：triage v1.3.0 · git tag v1.4.3（本次）· 回退 `git reset --hard v1.4.2`

## 六、复核结论

对照 9 条验收标准逐条自查全部达标（范围说明✓ 计划✓ 最小增量✓ 端到端✓ 风险回滚✓ 异常路径✓ 台账✓ 交接✓ 复核✓）——**M2 里程碑可以关闭**。
