# CHANGELOG v1.4.6（2026-09-24）

主题：M3.4 存储地基——三服务单机 JSON 落盘整体迁移 SQLite（WAL）。收口评分报告两项技术扣分：并发写无防护（−5，W3）、会话写放大（−2）。taskboard（a）/ lobster（b）/ triage sessions（c）三步落地。

## 变更清单

### 1. M3.4a：taskboard SQLite 化（51fc501）
- `_load/_save` 换 SQLite 内核、签名与返回结构不变——**全部路由代码零改动**。
- WAL + `busy_timeout=30000` + 单事务全量替换：跨进程/多 worker 并发写被串行化，不再有半截文件（W3 收口；threading.Lock 只护进程内的时代结束）。
- `seeded` 旗标一次性导入 `tasks.json` 快照（22 任务 + seq=22 续号），之后 DB 是唯一真值源、快照转只读种子。
- health 暴露 `storage`/`tasks` 口径（健康端点永不 500，存储异常如实暴露）。
- 测试：`releases/taskboard/tests/test_storage.py` 5 用例（快照导入 seq 续号 / seeded 幂等 DB 为真 / 空库启动 / 损坏快照 500 / create→patch→undo 闭环）。**自包含无 conftest**——避免与 lobster 的 conftest.py 单进程合跑撞 pytest import mismatch（D-011 近亲）。

### 2. M3.4b：lobster SQLite 化（51fc501）
- 同款模式：`orders` 表 + 快照一次性导入（44 订单）+ health 暴露口径。
- **测试密封化**：conftest 夹具把 DB 与快照路径全部指向 tmp——测试从此不再向真实台账写测试单（历史上台账 18→44 条增长的根源消除）；`test_list_orders_masks_phone` 改为自播种。

### 3. M3.4c：会审台 sessions SQLite 化（3f71bd0）
- `save_session` 改**单行 upsert**：面板每次会审起止只写一行，不再全量重写所有会话。
- `give_feedback` 改**单行读改写**：人工反馈只动目标会话一行——**写放大清零**（−2 收口）。
- `load_sessions` 坏 DB → 返回空 dict（D-013 语义等价迁移：坏存储不崩）；mtime 缓存随文件存储退役。
- JSON 快照一次性导入（31 会话）；坏快照不阻塞建库。`ledger.jsonl` 是纯追加台账、无写放大问题，维持 JSONL 不动。
- 测试：夹具密封化（防真实快照误导入）+ 2 个存量存储用例迁 DB 口径 + 新增「导入一次 DB 为真值源」「反馈单行写不碰邻行」；会审台 36→38 全绿。

### 4. 配套
- `.gitignore` 补 `*.db` / `*.db-wal` / `*.db-shm`（真值源不入库，快照 JSON 仍跟踪）。
- ruff 清理 taskboard 存量未用 import（os/uuid/datetime 保留实际使用者）。

## 回归证据

| 项 | 结果 |
|---|---|
| 三服务测试单进程合跑 | 54/54（taskboard 5 + lobster 11 + triage 38），exit 0 |
| 根目录全量 pytest | 691 全绿，0 失败 0 跳过 |
| ruff | 全部改动文件零告警 |
| 真实迁移 | taskboard 22 任务 / lobster 44 订单 / triage 31 会话，与快照基线逐条一致 |
| 线上抽检 | lobster 接口 44 条明文 0；triage 会话列表/详情 API 读路径完整 |
| 三服务健康 | 8687 / 8791 / 8792 / 8793 全部 `sqlite(wal)` 口径实测 OK |

## 影响面

- **数据面**：三个 `.db` 文件为运行时真值源（gitignored）；三个 JSON 快照冻结为种子（仍跟踪在库、不再更新）。回滚 = 删 DB 文件后重启（从快照重新种子）或 git 还原代码。
- **行为面**：API 契约零变化（路由层未动）；唯一语义差异是存储层并发安全性提升。
- **已知边界**：taskboard `seq` 自增在真·多进程同时创建任务时仍可能撞号（与 JSON 时代同等语义，未引入回归；单 worker 部署下无影响）；`note` 自由文本客户自填号码仍在脱敏范围外（v1.4.5 已声明）。

## 回滚

- 单服务可逆：删对应 `data/*.db`（含 -wal/-shm）→ `git checkout` 上一版 `app.py` → 重启（watchdog 5 分钟内自动拉起），服务从 JSON 快照回到文件存储时代。
- 全量回滚：`git checkout v1.4.5` 三服务目录 + 删 DB 文件。
