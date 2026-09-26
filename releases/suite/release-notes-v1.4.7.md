## v1.4.7 · 上云与 CI 首绿

套件首次发布至 GitHub 公开仓库，GitHub Actions 四路矩阵（Linux 3.11 / 3.12 / 3.13 + Windows）全绿；同期收口 2026-09-25 全天质量加固轮。

### ✨ 亮点

- 🚀 **CI 首跑消红收官**：4/4 job success，从此 main 受四路矩阵守护
- 🧪 **测试基线 691 → 890**：ModelHub 覆盖率大补课（streaming 7%→89%、切换链 61%→85%、运维路由 0 覆盖→21 用例），补测过程真抓到并发丢密钥、lost update、三处「文档说要说话、代码只会 500」
- 🔐 **vkeys 并发丢密钥四层修复**：mtime 门（同刻写 78~84% 撞格）/ 锁粒度 / tmp 撞名 / 跨进程 OS 锁，逐层量化逐层修、各配专属回归测试；鉴权假阴性 17/300 → 0/300
- 🩺 **triage lost update 修复**：人工反馈能把工单从 `done` 倒回 `running`（前端永远转不完）的根因清除，`BEGIN IMMEDIATE` 变异验证 6/30 红 → 0/30
- 📊 **A/B 对照口径诚实化**：准确性指标不再借用被难度筛过的半数样本（8 → 16 条），重算 MAE 0.59→1.83；已知 flaky 尾巴（time.sleep＋TaskManager）正式收口

### 📋 变更清单与回归证据

完整清单见 [`releases/suite/CHANGELOG-v1.4.7.md`](releases/suite/CHANGELOG-v1.4.7.md)。

| 项 | 结果 |
|---|---|
| 根目录全量 pytest | 890 全绿，0 失败 0 跳过（本地 + CI 双口径） |
| GitHub Actions | windows 11 步 + Linux×3 各 16 步，全部 success |
| ruff / format / mypy | 零告警（本地与 CI 同钉版本） |
| e2e 三通道 | json_fallback / structured_output / function_calling 各 24 次命中 |

### ⚠️ 注意事项

- **版本口径**：git tag `v1.4.x` 是套件发布线锚点；产品版本 `pm/__init__.py:__version__ = 2.1.0`、网关 `GATEWAY_VERSION = "1.1.0"` 各自独立，未变动。
- **安全豁免 W7/W8/W9 尚未收回**（`releases/suite/security-waiver.html`）：公网暴露部署前必须处理。
