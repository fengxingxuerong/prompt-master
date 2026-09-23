# CHANGELOG v1.4.5（2026-09-23/24）

主题：M3 安全收口三连——会审台鉴权（M3.1）+ 龙虾订单存储脱敏（M3.2）+ 看门狗 .env 透传与鉴权开启姿势统一（M3.3），外加 W9 桌面明文密钥处置。对应评分报告扣分项：无鉴权多租户（−10，豁免期内先行机制化）、PII 存储明文（W8 存储侧）、stderr 告警静默。

## 变更清单

### 1. M3.1：会审台 TRIAGE_TOKEN 鉴权（323205c）
- `releases/triage/app.py` 新增环境变量门控鉴权中间件：默认未设 = 本地全开放（行为与历史零差异）；设 `TRIAGE_TOKEN` 后除 `/api/health` 与页面（`/`、`/static/*`）GET 外，**全部端点**（含写操作与 `/api/sessions*` 数据读）需 `X-API-Key`。
- 比 taskboard/lobster 收紧一档：数据读也凭据化——工单正文含客户内容（W8 教训：明文 PII 不应无凭据可读）。
- 测试：`releases/triage/tests/test_auth.py` 7 用例三态（未设全开放 / 设后豁免与 401 / 正确 key 放行），含空白 token 容错与 env 隔离（W16 教训）。
- 会审台测试 29→36 全绿；红队隔离实例实测 7/7 符合预期。

### 2. M3.2：龙虾订单存储脱敏（2a6264c）
- `releases/lobster/app.py`：新增幂等 `mask_phone()`（前 3 后 4 + `****`）；`create_order` **入岸即掩码**（PHONE_RE 校验在前，掩码串伪装输入 422 拒绝）；`list_orders` 兜底再掩一次。
- 存量迁移：`releases/lobster/mask_storage_phones.py` 一次性脚本（备份→迁移→复检，幂等可重跑，`--verify-only`）；44/44 条掩码完成，复检零明文。明文原件备份于 `logs/`（gitignored，不入库）。
- 测试：`releases/lobster/tests/test_storage_masking.py` 3 用例（tmp 隔离，不污染真实台账）；龙虾测试 7→10 全绿。
- 已知边界：`orders.json` 为 git 跟踪文件，历史提交含旧明文（本地私有仓；转公开前需 filter-repo）；`note` 自由文本客户自填号码属范围外。

### 3. M3.3：看门狗 .env 透传 + 鉴权姿势统一（3831e19）
- `releases/suite/watchdog_suite.py`：启动子进程前加载仓库根 `.env`（KEY=VALUE，剥引号，系统环境优先）并合并进 `env=` 传递——修复「schtasks 极简环境下 `.env` 里的 TOKEN 永远到不了服务进程」的结构性缺口。至此四 TOKEN（PMH_GATEWAY / TASKBOARD / LOBSTER / TRIAGE）的开启姿势统一为「.env 写一行」。
- **顺带修复潜伏 bug**：`_check_stale_tasks` 调用本地 `notify()` 不支持的 `level/base_dir` 参数，TypeError 被 except 静默吞掉——超时任务告警从未真正落盘。改走会审台通知中心（`/api/notifications` 可见），失败回退 G-06 本地日志。
- `.env.example` 收齐四 TOKEN 注释项；根 README §2 与 `docs/suite/api-reference.md` 头注同步口径。
- 测试：`tests/test_watchdog_env.py` 3 用例（引号剥离 / 系统环境优先 / 文件缺失返回空）。
- 红队复测：taskboard 4/4、lobster 3/3（隔离实例 8795/8796），triage 7/7（前轮）——三服务未鉴权写全部 401。

### 4. W9：桌面明文密钥处置（无代码提交）
- `D:\Desktop\新建 Text Document.txt`（AMD / SenseNova×3 / OpenRouter / StepFun / NVIDIA 共 7 组密钥明文）经逐一比对确认 **7/7 已在 `.env` 收敛**后删除，残留零。密钥面收敛至 `.env` 单点。

## 回归证据

| 项 | 结果 |
|---|---|
| 根目录全量 pytest | 691 收集全绿（688 存量 + 3 看门狗），0 失败 0 跳过，exit 0 |
| 龙虾站 pytest | 10/10（7 存量 + 3 存储脱敏） |
| 会审台 pytest | 36/36（29 存量 + 7 鉴权三态） |
| ruff | 新增/改动文件零告警 |
| 三服务健康 | 8687 / 8791（新代码）/ 8792 / 8793（新代码）实测 OK |
| 红队 | 三服务未鉴权写 401 共 14 探针全符合预期 |
| 数据迁移 | 龙虾 44 条掩码复检零明文；测试残留 6 条清除恢复基线 |

## 影响面

- 改动文件：`releases/triage/app.py`、`releases/lobster/app.py`、`releases/lobster/mask_storage_phones.py`（新增）、`releases/suite/watchdog_suite.py`、`releases/lobster/data/orders.json`（掩码后快照）、三份 README/交接文档、`.env.example`、测试 3 个新增文件。
- 生产行为变更：默认（TOKEN 未设）行为与历史完全一致；设 TOKEN 后受保护接口 401 属预期安全行为。
- 回滚：单文件可逆——triage/lobster 的中间件与 mask 逻辑可按提交逐块还原；看门狗还原为无 env 透传（`env=` 参数删除即可）；orders.json 可由 `logs/lobster_orders_plaintext_backup_*.json` 或 git 历史恢复。
