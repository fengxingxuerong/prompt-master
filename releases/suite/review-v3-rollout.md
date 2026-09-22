# REVIEW_SYSTEM_V3 落地报告（2026-09-22 23:05）

## 改进内容（采纳提示词评价报告定稿建议）

1. **REVIEW_SYSTEM 升级 V3**（releases/triage/app.py）：在 V1 基础上追加 severity 三档口径（P1=资金损失/生产故障/舆情正在发生）、action 可执行性约束（含时限或对象，禁止空话）、confidence 锚点（0.9+/0.7~0.8/≤0.6）——全部保留在 system 段。
2. **弱信号规则移到 user 段**：正文 <30 字时自动追加"category=other、confidence ≤0.5、action 写澄清问题"——只在弱信号场景注入，避免 V2 的约束过载跑飞。
3. **附带修复**：FastAPI version 元数据 1.1.0→1.3.0（历次补丁只改了 health 串的展示性遗漏）。

## 三方对比（同 4 任务，deepseek-v4-flash，temperature=0.2）

| 任务 | V1 现行版 | V2 全锚点版 | **V3 混合版（本轮）** |
|---|---|---|---|
| T1 混合 | billing/P2/0.95 ✅ | billing/P2/0.92 ✅ | **billing/P2/0.9 ✅ action 含 1 个工作日时限** |
| T2 已扣款无订单 | billing/**P2**/0.85 ❌ | billing/**P1**/0.9 ✅ | **billing/P1/0.9 ✅ action 含 1 小时补单/退款时限** |
| T3 弱信号 | other/P3/0.6（过度自信） | **JSON 跑飞** ❌ | **other/P3/0.4 ✅ action=澄清问题（符合弱信号规则）** |
| T4 生产故障 | technical/P1/0.95 ✅ | technical/P1/0.95 ✅ | **technical/P1/0.95 ✅ action=DBA 备份恢复+通知客户** |

**V3 终测：JSON raw-ok 4/4 + category+severity 双命中 4/4——三项口径全满分**（V1 双命中 3/4，V2 合规 3/4）。

## 质量跃迁细节

- T2 action 从"核查支付回调并补单，尽快确认"（V1，无时限）→"1 小时内补单或原路退款"（V3，可执行可考核）
- T3 从过度自信 0.6 → 0.4 + 澄清式 action（诚实降级）
- T4 action 从"立即联系运维" →"启用备份恢复+同步通知客户"（完整处置链）

## 回滚

- 单文件：`copy releases\triage\app_v1.3.0.py.bak releases\triage\app.py` + 重启（回到 V1 提示词）
- git：`git reset --hard v1.4.3`

## 验证

triage 套件 29/29 全过 · 四服务健康 4/4 · 真机会审含弱信号注入全链路实测（pe-v3-results.json）
