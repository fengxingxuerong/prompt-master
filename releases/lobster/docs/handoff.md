# 澳洲龙虾发布包 · 交接文档（演示口径）

> 版本 1.0 · 2026-09-21 · 性质：**演示站（demo）**，不构成真实交易

## 1. 交付物清单与访问方式

| 交付物 | 位置 | 访问 |
|---|---|---|
| 产品落地页 | lobster/static/landing.html | `http://127.0.0.1:8791/` |
| 订单台账页 | lobster/static/ledger.html | `http://127.0.0.1:8791/ledger` |
| 宣传海报 | lobster/static/poster.html | `http://127.0.0.1:8791/poster` |
| 社媒图文文案 | lobster/docs/social-copy.md | 文件 |
| 异常处理规则 | lobster/docs/exception-rules.md | 文件（台账页同步展示） |
| 订单台账数据 | lobster/data/orders.json | JSON 落盘，接口读写 |
| 订购/台账 API | lobster/app.py | POST/GET/PATCH /api/orders |

## 2. 启动 / 停止

```bash
cd <部署目录>\lobster
# 依赖：fastapi、uvicorn、pydantic（随 prompt-master venv 已装）
python app.py --port 8791
# 健康检查
curl http://127.0.0.1:8791/api/health
# 停止：Ctrl+C 或结束对应 python 进程
```

公网暴露前必须：反向代理 + HTTPS + 管理口令（演示版无鉴权，属已知风险，见 §5）。

## 3. 日常操作

- **查订单**：台账页 `/ledger`，可按状态筛选；或 `GET /api/orders?status=pending`
- **状态流转**：台账页每行操作按钮（确认/发货/签收/缺货/退款/死虾赔付），或 `PATCH /api/orders/{id}`
- **异常演示**：台账页「异常分支演示」提示区；三分支规则见页面下方规则区
- **替换占位内容**：
  - 品牌名 → landing.html / poster.html / social-copy.md 全局搜索 "AUSSIE LOBSTER · 澳龙直送"
  - 价格 → app.py 的 `SPECS` 字典 + landing.html 规格表 + poster.html 价格条（三处同步改）
  - 产地/供应商口径 → 确认后更新落地页 01 区块与 social-copy.md 合规红线

## 4. 回滚步骤（已演练）

1. **数据回滚**：`copy data\backups\orders-<时间戳>.json data\orders.json`（覆盖即回滚；服务热读，无需重启）
2. **整站回滚**：部署目录为整份拷贝——恢复 = 用上一版目录替换，或 `git checkout` 上一标签
3. **演练记录**：见最终验收报告 §部署与回滚（backup → 破坏 → 恢复 → 数据一致校验，全通过）

## 5. 风险清单（真实运营前必须处理）

| 风险 | 等级 | 处理 |
|---|---|---|
| 演示版无任何鉴权，订单接口可被任意调用 | 高 | 接入管理口令 + 验证/限流后再公网开放（已支持 LOBSTER_TOKEN 环境变量门控） |
| 手机号 PII | 高→已降级 | **M3.2（2026-09-23）存储侧已收口**：入岸即掩码（前 3 后 4）+ 存量 44 条已迁移（明文原件备份于 logs/，不入库）；接口掩码维持不变。真实运营仍需隐私政策 + 合规评估 |
| 无支付通道，金额仅为参考价 | 中 | 接入支付服务商后启动（Goal Brief 范围外） |
| 进口/检疫/食品经营资质未办理 | 高 | Goal Brief 范围外，上线前自行确认合规 |
| 台账为单机 JSON 文件，无并发事务保证 | 中 | 上量前迁移 SQLite/PostgreSQL |

## 6. 责任分工建议（真实运营时）

- 商品与定价：业务负责人（占位替换后签字确认）
- 履约与冷链：供应商/物流方
- 台账日常操作：客服/运营
- 系统维护：技术（本交付包接手人）
