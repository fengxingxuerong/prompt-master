# M2 里程碑执行计划

| 阶段 | 内容 | 依赖 | 完成判据 | 状态 |
|---|---|---|---|---|
| M2.0 | 基线留存（health/测试/git 读数） | 无 | maintenance-log 新增段 | ✅ |
| M2.1a | 会审台新增 notify 模块：通知落盘+webhook 分发（5s 超时静默） | 无 | 单元触发验证：假 webhook 不可达不崩，notifications 仍落盘 | 待办 |
| M2.1b | 通知查看页 /static/notifications.html + /api/notifications 端点 | M2.1a | 页面 200 + 数据非空 | 待办 |
| M2.2 | 看门狗巡检挂任务超时检查（pending>24h → notify） | M2.1 通知通道 | 构造一条过期任务实测落告警 | 待办 |
| M2.3 | 看板实时曲线：/api/gateway-metrics 代理 + 前端 5s 轮询 SVG 折线 | 无 | 页面出现曲线且数据随真实调用增长 | 待办 |
| V | 端到端验证 + 异常演练（webhook 不可达/服务重启） | 全部 | 全部 PASS + 回滚演练 | 待办 |
| H | 台账 TASK-015 + 验收报告 + git tag v1.4.3 | V | 报告归档 + 工作区干净 | 待办 |

时间窗口：单会话 ~40 分钟。责任：Agent-AutoClaw 全程执行，关键决策（配置格式、阈值 24h）已按合理默认执行并在交付时标注可调位置。
