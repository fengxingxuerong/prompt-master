# CHANGELOG v1.4.0（2026-09-22）

主题：清遗留缺陷（W4/W12/W16/龙虾 pytest 化）+ 统一 README 索引。安全维度沿用 2026-09-21 书面豁免，不在本轮范围。

## 变更清单

### 1. 统一 README 总索引（协作维度）
- 新增 `README.md`（项目根）：三服务一览、启动/看门狗命令、报告台账索引、新成员 10 分钟上手、责任边界（安全豁免、单机存储、W9 桌面密钥）。

### 2. W12：ModelHub JSONL 台账轮转
- 新增 `releases/suite/rotate_ledger.py`：`logs/modelhub_ledger.jsonl` 超过 1MB 时轮转为 `.1`（保留 3 代，`.3` 淘汰）。
- 实测：`--force` 演练，旧账 138 条归档 `.1`；轮转后新调用写入新账（calls=1）正常，`/v1/ledger/stats` 从零累计符合预期。

### 3. 龙虾站 pytest 化
- 新增 `releases/lobster/tests/{conftest.py,test_lobster.py}` + `pytest.ini`：7 用例覆盖健康检查、下单成功、手机号 422、规格 422、列表 PII 脱敏（**** 掩码）、PATCH 状态流转、未知单号 404。
- **7/7 全过**（TestClient 进程内，零真实端口依赖）。
- 附带修复：`app.py` 订单备注 Latin-1 兼容层中 `asyncio.get_event_loop()` 在无 loop 线程（TestClient）抛 RuntimeError → 改为 try/except 回退空字节，生产路径行为不变。

### 4. W16：存量测试顺序依赖（根因修复）
- **现象**：`tests/test_measurement.py` 单跑 30/30 绿，与其它文件合跑时 `test_single_sample_keeps_pointwise_behaviour` 等 2 例红（judge_jitter=2.6 混入聚合）。
- **根因链**（逐层实证）：
  1. 宿主机 `.env` 含 `PM_JUDGE_JITTER=2.6`（2026-09-20 校准实测的合法生产配置）；
  2. `pm/llm.py` 模块级 `load_dotenv()` 在 import 时把该值灌进 `os.environ`；
  3. `pm/schemas.py` 在 import 时把 env 值冻结为模块常量 `JUDGE_JITTER`；
  4. 于是"谁先被 import"决定测量层用例结果——单跑 schemas 先加载（0.0，绿），先跑 test_api 则 llm 先灌 env（2.6，红）。
- **修复**：`tests/conftest.py` 顶部在任意测试模块加载前 `pop("PM_JUDGE_JITTER")` 并把 `schemas.JUDGE_JITTER` 钉回旧口径 0.0；需要测抖动的用例维持既有 `monkeypatch.setattr` 显式覆盖，互不影响。
- **附带**：`tests/test_quality.py::test_full_loop_with_meta_leak_optimizer` 改用 `fake_backend` 上下文（ContextVar 任务级隔离），不再在模块级改 `_SCENARIO`/清共享计数。

## 回归证据

| 项 | 结果 |
|---|---|
| 全量 pytest（主项目 tests/） | 688 收集，连续 3 次 exit 0（687 passed + 1 skipped），历史首次全绿 |
| 龙虾站 pytest | 7/7 passed |
| 三服务健康 | 8687 OK（12 models）/ 8791 OK / 8792 OK |
| W11 在位验证 | verify_w11.py：corrupt 留存 + ALERT 补丁 present=True |
| 安全豁免 | 沿用 2026-09-21 W7/W8/W9 书面豁免，本轮未涉安全改动 |

## 影响面

- 改动文件：`tests/conftest.py`、`tests/test_quality.py`、`tests/test_measurement.py`（撤探针）、`releases/lobster/app.py`（asyncio 守卫）、`releases/suite/rotate_ledger.py`（新增）、`README.md`（新增）、龙虾 `tests/`（新增）。
- 生产行为变更：无（`app.py` 守卫仅在无事件循环线程生效；轮转脚本为离线维护工具）。
- 回滚：单文件可逆——conftest 删除 W16 段、app.py 守卫还原为 get_event_loop 直调、轮转脚本直接删除。
