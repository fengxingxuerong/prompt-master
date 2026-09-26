# CHANGELOG v1.4.7（2026-09-26）

主题：上云与 CI 首绿——套件首次发布 GitHub 公开仓库（fengxingxuerong/prompt-master），GitHub Actions 四路矩阵（Linux 3.11/3.12/3.13 + Windows）跑通全绿；同期收口 2026-09-25 全天质量加固轮（ModelHub 覆盖率大补课、vkeys 并发丢密钥四层修复、triage lost update、A/B 口径修正）。

## 变更清单

### 1. 2026-09-25 质量加固轮（37 提交）
- **ModelHub 覆盖率大补课**：streaming 7%→89%（55f19f0）、池/断路器/虚拟密钥进门禁（5c7f6c3）、切换链 61%→85%（6add6fd）、运维路由 194 条 0 覆盖→21 用例并当场抓到三处「文档说要说话、代码只会 500」（f1eeb47）、真点一遍控制台抓到 6 处界面缺陷 + 流式记账漏计（2ad89c9）；覆盖率地板按实测抬至 89/65 并钉住「三处抄件必须同数」（0cba864）。
- **vkeys 并发丢密钥四层修复**（188adca）：mtime 门（同刻写 78~84% 落同一格）/ 锁粒度（每实例一把 → 按绝对路径共享）/ tmp 撞名（固定名 → pid+线程 id）/ 跨进程锁（新增 `<file>.lock` OS 锁），四层各配专属回归测试与变异证据；附鉴权假阴性 17/300 → 0/300。
- **triage 会话行 lost update**（906f3f8）：反馈端点 SELECT 不在写事务里，能把工单 `status` 从 done 倒回 running（前端永远转不完）；修 `finish_session()` 合并写 + `BEGIN IMMEDIATE`，变异验证 6/30 红 → 0/30；跑测试写脏运营数据同轮收口（CI 加 `git diff --exit-code -- releases/*/data`）。
- **A/B 对照口径修正**（e5940bd）：准确性指标不再借用稳定性配对集（交集 8 条 → 单发配对 16 条），重算 MAE 0.59→1.83、bias +0.45→+1.83，判据结论不变但样本口径诚实；账本密封（493ce8f）+ 人工打分表通道（b70fde7）+ A/B 收口撤回错误归因（96eea59）。
- **门禁建设**：pre-commit 真装并用故意失败提交验拦截（5746c04）、钩子改离线 local 与 CI 对齐（c8b5281）、装包入口 `prompt-master` 命令（074993f）、根 README 拉进抄件同数（19b1e51）、PM_LOG_DIR 晚绑定收敛（23b5581）。

### 2. 上云发布（2026-09-26）
- GitHub 公开仓库首发；全历史 160 提交署名由 `dev@localhost` 重写为「飞行雪绒 <feixingxuerong@users.noreply.github.com>」（filter-branch env-filter，树内容 diff 为零）。
- 推送前安检：全历史敏感文件名 + HEAD 内容级密钥扫描零命中（`config/modelhub.json` 系列 7 份长值全部为注释文本与 base_url；`test_preflight.py` 命中为 `_mask_key` 测试占位符）。

### 3. CI 首跑消红——两处「本机残留掩盖 CI 缺口」
- **无 .env 池初始化炸**（1ecd88a）：CI 干净检出没有 `.env`，`/v1/usage` 触发池初始化解析 `${PM_API_KEY_*}` 抛 ConfigError，4 job 全红；本地全绿纯靠本机 `.env` 掩盖。修：`gateway` fixture 注入假值——涉事路由只读池状态与台账、零真实出网。
- **ps1 e2e 不回退 PATH**（93e8c6e）：`run_e2e_stub.ps1` 只认 `.venv\Scripts\python.exe`，CI Windows runner 无 `.venv` 必挂（Linux 靠 ci.yml 显式传 `PY=python` 早已绕过）。修：与 sh 版探测同口径，`.venv` 不存在时回退 PATH 上的 python。
- **验证方法**：干净 worktree（无 `.env`、无 `logs/`）先复现 CI 红、修复后全量 EXIT=0；隐藏 `.venv` + PATH 前置装好依赖的临时 venv，完整模拟 CI 场景跑通三通道（24/24×3、exit=0）。

## 回归证据

| 项 | 结果 |
|---|---|
| 根目录全量 pytest | 890 全绿（v1.4.6 时 691 → 加固轮 +199），0 失败 0 跳过，本地与 CI 双口径 |
| GitHub Actions | 4/4 job success（windows 11 步 + Linux×3 各 16 步），对 93e8c6e |
| ruff check / format / mypy | 零告警（本地与 CI 同钉版本口径） |
| pre-commit | 四条钩子（ruff check / format / mypy strict / pytest 全量）提交期实测走通 |
| 干净检出复现 | git worktree（无 .env / logs/）先复现两处 CI 红根因、修复后 EXIT=0 |
| e2e 三通道 | json_fallback / structured_output / function_calling 各命中 24 次、exit=0 |

## 影响面
- 版本口径不变：`pm/__init__.py:__version__ = 2.1.0`（产品）、`GATEWAY_VERSION = "1.1.0"`（网关）各自不动；本标签只是**套件发布线**的锚点。
- 安全豁免 W7/W8/W9 尚未正式收回（`releases/suite/security-waiver.html`）；公网暴露前必须处理。
- 已知尾巴：根目录 `tests_modelhub/` 为早期遗留目录（不在 CI 口径），待归档。
