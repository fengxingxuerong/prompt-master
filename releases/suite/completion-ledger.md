# 完成度台账（全量盘点 · 2026-09-21）

> 口径：盘点范围=本会话两条交付线（ModelHub / 龙虾站 / 任务台账）+ 套件级报告。
> 每条含：来源分类 / 状态 / 完成时间 / 证据位置。状态只认可核验证据。

## A. 交付物盘点（14/14 在位）

| # | 事项 | 分类 | 状态 | 证据 |
|---|---|---|---|---|
| A1 | ModelHub v1.1.0（12 模型池+自动切换+固定角色+统一接入+SSE 心跳+usage 持久化） | 在办→完成 | ✅ | releases/v1.1.0/MANIFEST.txt（32 文件 SHA256）；服务 :8687 健康 |
| A2 | 龙虾站发布包（落地页+台账 API+海报+文案+规则+交接） | 在办→完成 | ✅ | releases/lobster/（health ok）；最终验收报告 docs/ |
| A3 | 任务台账系统（鉴权中间件+分页+单查+undo+负载看板） | 在办→完成 | ✅ | releases/taskboard/（health ok）；docs/rules|exceptions|handoff |
| A4 | 竞品分析 + 优点映射 + 评分卡（60/60） | 待办→完成 | ✅ | docs/iteration/*.md + scoring-report.html |
| A5 | 套件级迭代报告 v1.2.0（基线→优化→复测→回归→回滚） | 待办→完成 | ✅ | releases/suite/iteration-report.html + CHANGELOG-v1.2.0.md |
| A6 | 能力核验报告（"能真实做事"6/6） | 待办→完成 | ✅ | releases/suite/capability-report.html |
| A7 | 诊断审计（13 项零问题）+ 缺点分析（17 条，高3中6低8） | 遗留→完成 | ✅ | releases/suite/audit-report.html + weakness-report.html(+md 归档) |
| A8 | 五维基线/复测对（性能/SEO/A11y/安全/代码质量） | 待办→完成 | ✅ | releases/suite/five_dim_baseline.json / five_dim_retest2.json |
| A9 | 监控巡检（health×3+数据口径，ALERT→OK 实测） | 待办→完成 | ✅ | releases/suite/monitor_suite.py + monitor_log.jsonl |
| A10 | requirements.lock（83 行）+ 龙虾 requirements.txt + start.cmd | 待办→完成 | ✅ | releases/requirements.lock、lobster/requirements.txt、start.cmd |

## B. 本轮发现的真实缺口与补齐记录

| # | 缺口 | 严重度 | 处理 | 复验结果 |
|---|---|---|---|---|
| B1 | **三服务实际已停**（后台会话随回合结束终止，无进程守护） | 高 | 干净重启三服务（杀残留→逐个启动） | HEALTH 3/3 OK（实测输出留存） |
| B2 | taskboard 带 TASKBOARD_TOKEN 运行（基线鉴权测试残留环境变量），GET /api/tasks 401 | 高 | 杀带令牌实例→无令牌模式重启 | LEDGER tasks=6 正常可查 |
| B3 | 龙虾 app.py 重复 if（编译失败 S2，服务跑旧代码未暴露） | 高 | 上轮已修（删重复行） | 本轮 py_compile 4/4 |

## C. 待用户决策项（非技术缺口，不属于"我该做的"）

| # | 事项 | 为什么等你 |
|---|---|---|
| C1 | 龙虾占位替换四件套（品牌/价格/供应商/产地）= TASK-005 P1 | 商业口径只有你能定 |
| C2 | 桌面明文密钥文件处置（W9） | 你的本地文件，删除/迁移需你授权 |
| C3 | 是否公网暴露（决定鉴权强度与部署形态） | 需你的部署决策 |
| C4 | 上游 stream 401 排查（TASK-002 排队中） | 依赖商汤账户侧信息 |

## D. 回滚与异常预案（指向既有留痕）

- 回滚：各服务交接文档 + CHANGELOG-v1.2.0.md §回滚步骤（备份→覆盖→SHA256 校验，已四次跨服务实测）；
- 异常实录：台账损坏 500 显式报错（非静默）、监控 ALERT→OK 实测、红队 PATCH 改写已还原留痕；
- 长期监控：`python releases/suite/monitor_suite.py --watch`（60s 巡检）或接入 AutoClaw 定时任务读 ALERT 字段。
