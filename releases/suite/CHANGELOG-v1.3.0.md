# Suite v1.3.0 变更日志（全维度打磨轮）

> 日期 2026-09-22 · 每项变更：内容/原因/影响范围/回滚步骤 · 安全维度按用户指令豁免（不纳入评分与验收）

| # | 变更 | 原因（竞品对标缺口） | 影响范围 | 回滚步骤 |
|---|---|---|---|---|
| C1 | 任务台账 PATCH 支持 title/desc 编辑（竞品 One API/Linear 的"任务可编辑"能力对齐） | 竞品优点落地清单 G1 | taskboard API + history 留痕 | 还原 app.py（编辑功能为增量字段，回滚不损数据） |
| C2 | 看板前端加"编辑"按钮（prompt 双字段） | 同上 G1 前端侧 | board.html | 还原 board.html |
| C3 | 实测通过：desc 更新入库 + history 第 5 条留痕 | — | — | — |
| C4 | API 接口文档（三服务 33 路由全量，错误码实测口径） | Goal Brief 交付物 | docs/suite/api-reference.md | 文档可独立删除 |
| C5 | 设计规范（Design Tokens：三服务色板/字体/间距/四态/断点） | Goal Brief 交付物 | docs/suite/design-tokens.md | 文档可独立删除 |
| C6 | 运维实录：服务停运→重启→复测恢复（无进程守护缺点的实证与操作 SOP） | 五维复测 0/12 异常诊断 | 运行态 | — |

安全豁免口径（沿用用户 2026-09-21 指令）：W7/W8/W9 不纳入本轮验收，已登记 TASK-20260922-007。

## 本轮复测口径与结果

- five_dim_v13b：性能 12/12 · SEO 5/6（modelhub 根为 JSON 豁免）· A11y 1（JSON lang 豁免）· 安全 3 项（豁免口径）· 编译 4/4
- 编辑接口契约：PATCH title/desc → changes="描述更新" → GET 复读 desc 已更新 → history 5 条
