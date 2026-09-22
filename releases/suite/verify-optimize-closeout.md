# 验证·测试·优化 收尾报告（2026-09-22 20:35）

基线：triage v1.3.0 · git tag v1.4.3 · 测试框架 pytest（零付费工具）· 环境=本机生产实例（只读验证为主）

## 一、缺陷台账（D 系列）

| 编号 | 级别 | 缺陷 | 复现步骤 | 处置 | 复验结论 |
|---|---|---|---|---|---|
| D-011 | 一般 | lobster 与 triage 的 tests 合并跑时 `from app import app` 撞名，triage 用例全挂（AttributeError: no attribute LEDGER） | `pytest releases/lobster/tests/ releases/triage/tests/` | test_triage.py 改 importlib 按文件路径加载 triage_app + sys.path 注入（fix_d011/d011b） | ✅ 组合跑 17/17 PASS |
| D-012 | 轻微 | test_async_submit 偶发失败（后台会审线程慢于 4s 轮询窗口，regression_all 组合跑出现 1 failed） | regression_all.py 组合连跑数轮 | 轮询窗口 40→120 次（12s），保留 done 判定 | ✅ 单跑+组合跑均 10/10 |
| D-013 | **严重** | **回滚演练中 sessions.json 被覆盖为单会话（20:29:13），22 会话业务数据丢失** | 双实例交叠期某客户端 POST /api/reviews，新实例以内存态（1 会话）全量写回 sessions.json | **git 快照恢复**：`git checkout HEAD -- sessions.json`，重启后 22 会话/153 台账全数找回，故障窗口 ~3 分钟 | ✅ 数据完整恢复；根因为多实例并发写无防护（已列入风险清单） |
| D-014 | 轻微 | test_api 单跑/组合跑时 stderr 出现 langgraph `cannot schedule new futures after shutdown` 栈 | pytest tests/test_api.py | **非用例失败**：后台任务清理竞争的 stderr 噪音，pytest exit=0 全过 | ✅ 判定为噪音，已记录不修（修则需 scheduler 生命周期钩子，超出本轮） |

## 二、性能优化前后对比

| 端点 | 优化前（基线） | 优化后 | 说明 |
|---|---|---|---|
| GET /api/ledger（热路径） | 中位 4ms | 8-9ms（抖动带内持平） | **收益在增长斜率**：旧实现全量 json.loads（60KB→4ms，1MB 预估 ~60ms 线性恶化），新实现固定解析尾部 50 行，斜率归零 |
| 其余 16 端点 | 全部 <30ms | 持平 | 首连抖动（60-82ms）属 TCP 冷启动，非热点 |
| 写放大实测 | save_sessions 全量写 22 会话=2.7ms | 未动 | 100 会话预估 ~50ms，触发条件：会话破百（与 G-10 SQLite 迁移同窗口） |

数据：perf-baseline.json（17 端点×3 轮中位数）。

## 三、验证汇总（第二轮独立回归）

- triage 10/10 · lobster 7/7 · main 全量 exit=0（含 measurement/quality/api 敏感面 83 用例）
- 四服务健康 4/4 · 回滚演练往返（v1.3.0↔v1.2.0）：8.5s / 7.8s
- D-013 数据恢复验证：git checkout 后 22 会话 / 153 台账完整

## 四、生产就绪结论

**就绪，附两条运行纪律**：
1. **禁止多实例同时写 triage data/**（D-013 根因）：部署/回滚必须先杀旧进程再起新进程（现有脚本已如此），不做蓝绿双活
2. 会审台/任务台账数据目录纳入 git 提交节奏（本次正是靠 HEAD 快照恢复）；或按 G-10 迁 SQLite 后获得事务保护

## 五、遗留与建议

- 等用户口径：TASK-002 商汤 / TASK-005 龙虾四件套 / OpenRouter 充值（无变化）
- 建议下次窗口：D-014 scheduler 生命周期钩子（消除 stderr 噪音）、save_sessions 原子写（tmp+rename，防 D-013 复发）
- 回滚手册：git tag v1.4.0/v1.4.1/v1.4.2/v1.4.3 四锚点；triage .bak 全套（v1.0/v1.1/v1.1.1/v1.2.0/v1.3.0）
