# docs/archive · 过程性报告归档

这里放**带日期戳的过程记录**（QA 报告 / e2e 迭代日志 / 真机矩阵 / 部署验证 / 修复日志），
与"活的文档"分开：活文档（`../operations.md`、`../evaluation.md`、`../design-notes.md`、
`../agent-cli-guide.md`、`../rest-api.md`、`../release-notes.md`）随代码更新，这里的文件
写完即定格，只作为当时结论的出处被引用——它们被活文档和测试注释引用时，路径都指向
`docs/archive/…`，全文搜索仍可达。

归档 ≠ 无效：修复日志（fix-log）的 P 编号、矩阵报告的"发现 N"仍是现行测试与注释里的
出处引用；改活文档时不要顺手"同步更新"这里的数字——它们记录的是当时测到了什么。

## 清单（2026-09-26 归档，git mv 保历史）

| 文件 | 是什么 |
|---|---|
| `qa_report_2026-09-08.md` / `qa_report_2026-09-13.md` | 两轮全量 QA：缺陷清单与修后回归 |
| `e2e_report.md` / `e2e_report_v2.md` | 早期 e2e 报告（本地文件，被 `.gitignore` 的 `e2e_report*.md` 挡在库外，随目录一并归位） |
| `e2e_iteration_log_2026-09-13.md` | 迭代日志：每轮修订的评委读数与根因 |
| `deploy_verification_2026-09-13.md` | Docker 构建 + playwright 浏览器运行时验证 |
| `llm_e2e_matrix_2026-09-16.md` / `llm_e2e_matrix_2026-09-18.md` | 真机矩阵两轮（Δ 口径互不可比，见各自前提声明） |
| `fix-log.md` | 修复台账（P/A/C 编号），被测试注释引用 |
