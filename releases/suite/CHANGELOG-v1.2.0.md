# Suite v1.2.0 变更日志（全方位优化轮）

> 日期 2026-09-21 · 每项变更：内容/原因/影响范围/回滚步骤

| # | 变更 | 原因（基线问题） | 影响范围 | 回滚步骤 |
|---|---|---|---|---|
| C1 | 龙虾 app.py 删除重复 `if s.status not in STATUSES:`（146 行） | 基线体检发现 S2 编译失败（py_compile 3/4） | 龙虾 PATCH 接口 | 还原本文件至备份（backups/lobster-app-prereopt.py 建议补存） |
| C2 | 三服务 HTML 补 meta description ×5（lobster 三页含转义修复、taskboard board、modelhub console） | 基线 SEO 5/6 | 页面 head | 删除对应 meta 行 |
| C3 | taskboard + lobster 加可选令牌中间件（TASKBOARD_TOKEN/LOBSTER_TOKEN，默认不设=本地开放） | 基线安全：写接口无强制鉴权（W7） | 全 API 写路径 | 删除 require_token 中间件；或不设环境变量即关闭 |
| C4 | 龙虾 GET /api/orders 手机号脱敏返回（136****6003） | 基线安全：PII 明文（W8） | 台账读接口 | 还原 list_orders 返回段 |
| C5 | 监控巡检 monitor_suite.py（health×3 + 数据口径，ALERT 字段可接通知） | 验收标准"上线后有监控告警" | 新增文件 | 删除文件即可 |
| C6 | 003/001/004/006 任务台账状态闭环更新 | 机制自我应用 | 台账 history | undo 或备份恢复 |

复测证据：five_dim_baseline.json vs five_dim_retest2.json（同口径五维）
