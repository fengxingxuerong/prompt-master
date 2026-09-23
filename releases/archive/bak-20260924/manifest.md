# .bak 归档（2026-09-24 · M3 收口清理）

评分报告 v1.0 扣分项「.bak 双轨冗余（−4，D 维度）」的闭环动作：9 个被 git 跟踪的
.bak 备份全部 `git mv` 至本目录（历史保留、零删除）。此后**版本回滚只走 git tag**
（v1.4.5 / v1.4.6 已在位），不再维护服务目录内的双轨副本。

## 清单与原位置

| 文件 | 原位置 | 来源 |
|---|---|---|
| app_v1.0.py.bak ~ app_v1.3.1.py.bak（6 份） | releases/triage/ | 会审台 v1.0→v1.3.1 迭代备份 |
| index_v1.1.html.bak | releases/triage/static/ | 会审台前端 v1.1 备份 |
| modelhub.json.bak-v140 | config/ | ModelHub v1.4.0 配置备份 |
| usage_store.py.v140.bak | pm/modelhub/ | ModelHub v1.4.0 usage_store 改动备份 |

## 恢复方式

- **首选 git**：`git log --oneline -- <原路径>` 找到历史版本，`git show <commit>:<原路径>` 取回；
  或 `git checkout <tag> -- <原路径>`。
- **文件直取**：本目录内文件即原字节副本，`copy` 回原位置即可（文件名与清单「原位置」对应）。

## 引用关系说明

- `releases/triage/README.md` 版本表已同步指向本目录。
- 历史报告 `releases/suite/fix-report-v141.html`、`releases/suite/multi-llm-report.html`
  中的旧路径引用**保持原样不改写**（历史报告按当时口径存档），以本清单为准。
- 根目录 `.env.bak-*` 密钥备份文件未被 git 跟踪（.gitignore 覆盖），不在本清单范围；
  其内容已确认收敛于 `.env`（W9 处置时核验）。
