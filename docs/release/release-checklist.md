# ModelHub v1.0.0 发布检查清单

> 逐项复核留痕：✅ = 已确认（复核时间 2026-09-21，复核方式见备注）

## 1. 环境与配置

| 项 | 状态 | 备注 |
|---|---|---|
| 网关可启动（run_modelhub.py） | ✅ | 预发布 8687 端口运行中，/api/health 正常 |
| config/modelhub.json 合法且密钥解析成功 | ✅ | U1 用例：12 条目、${PM_API_KEY_*} 解析为 35 字符真实 Key |
| .env 密钥有效（3 商汤 + 1 AMD） | ✅ | 2026-09-21 实测全部可调用 |
| 角色文件合法（10 角色非空 system_prompt） | ✅ | 网关 /v1/roles 返回完整清单 |
| 端口/防火墙 | ✅ | 本地监听 127.0.0.1:8687；公网暴露须设 PMH_GATEWAY_TOKEN（文档已注明） |

## 2. 依赖与版本

| 项 | 状态 | 备注 |
|---|---|---|
| Python 3.12 venv 齐备 | ✅ | fastapi/uvicorn/pydantic/python-dotenv 全部随项目既有依赖，零新增 |
| pyproject packages 声明同步 | ✅ | 含 pm.modelhub；test_packaging 4/4 |
| 上游端点可达 | ✅ | 商汤 + AMD 当日连通性实测（11 模型中 10 可用） |

## 3. 数据与备份

| 项 | 状态 | 备注 |
|---|---|---|
| 改动前快照存在 | ✅ | backups/pre-modelhub-20260921-105501/（原 .env/llm.py） |
| 发布快照存在 | ✅ | releases/v1.0.0/（20 文件 + MANIFEST SHA256） |
| 回滚工具可用 | ✅ | CLI backup/rollback 已演练（SHA256 一致验证） |
| 台账文件可写 | ✅ | logs/modelhub_ledger.jsonl 88+ 事件持续追加 |

## 4. 权限与安全

| 项 | 状态 | 备注 |
|---|---|---|
| 密钥不明文入库 | ✅ | config 只存 ${ENV} 占位；.env 在 .gitignore |
| 网关鉴权可选开关 | ✅ | PMH_GATEWAY_TOKEN 机制就位（本地默认开放，文档警示公网必设） |
| 上游密钥权限最小化 | ✅ | 仅用 txt 既有 Key，未申请新权限 |

## 5. 质量门

| 项 | 状态 | 备注 |
|---|---|---|
| 端到端验收 8/8 | ✅ | tests_modelhub/e2e_results.json（最终回归重跑留档） |
| 异常注入 6/6 | ✅ | exception_test.py 输出 |
| 发布测试 U1-U3/C1/P2 | ✅ | release_results.json |
| 性能基线 | 见报告 | P1 直连参照受上游 401 突发影响，结论见测试报告 §5 的口径说明 |
| S1/S2 缺陷清零 | ✅ | defect-ledger.md：S2 全部修复/缓解并回归 |
| 冒烟测试 | ✅ | e2e_test.py 即冒烟（全链路：模型→角色→注册→对话→台账） |
| 回滚演练 | ✅ | 备份→破坏→恢复→哈希一致→网关热重载恢复 |

## 6. 交接材料

| 项 | 状态 |
|---|---|
| 发布说明（release-notes-v1.0.0.md） | ✅ |
| 已知问题清单（发布说明 §已知问题） | ✅ |
| 变更清单（changelog-v1.0.0.md） | ✅ |
| 缺陷台账（defect-ledger.md） | ✅ |
| 测试报告（release-test-report.html） | ✅ |
| 监控与维护方式（发布说明 §监控与维护） | ✅ |
