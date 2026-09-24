# 归档：根目录的覆盖率/测试"证据文件"（2026-09-25 收拢）

这 8 个文件原先躺在**仓库根目录**且被 git 跟踪，容易被当成交付凭据引用；
它们的实际内容与"看起来像什么"之间有出入，所以统一 `git mv` 到这里（历史保留，未删除）。

| 文件 | 实际内容 | 会被误读成什么 |
|---|---|---|
| `.cov_out.txt` | UTF-16（PowerShell 重定向产物）。**TOTAL 59%**，且结尾明写 `FAILED tests/test_triage.py::test_ledger_traceability` / `1 failed, 9 passed` | "有一次覆盖率跑出来的结果" —— 它其实是一次**红**的记录 |
| `.cov2.txt` | UTF-16。TOTAL 412 stmts / **71%**，19 passed | 同上系列，逐次快照 |
| `.cov5.txt` / `.cov6.txt` | UTF-16。TOTAL **73%**，27 passed | |
| `.cov7.txt` | UTF-16。TOTAL **74%**，29 passed | 被 `releases/suite/perfect-score-report.md` §03 引为"74%"的证据（这一条对得上，但口径见下） |
| `.cov3.txt` | UTF-16。TOTAL 只有 **60 stmts / 93%** —— 统计范围是单个模块，不是同一分母 | 与 .cov2/.cov5-.cov7 并排放时，93% 会被当成"同一套东西的更高覆盖率" |
| `.cov4.txt` | UTF-16。**没有 TOTAL 行**（只有 27 passed 与表格片段） | 分母不明的数字 |
| `pytest_summary.txt` | ASCII。两行：`exit=0` + **40 个点**，没有 `N passed` 汇总行 | "全套测试 0 失败" —— 而 `tests/` 实测收集 748 条，40 个点对不上任何一次全量跑；且本机 pytest 长期存在"用例全过也 rc=1"的环境缺陷（见 `docs/operations.md`），一行手抄的 `exit=0` 不能当门禁结论 |

## 三条共同的口径问题

1. **它们量的是 `releases/triage/`（412 stmts = `app.py` + `notify_center.py`），不是本产品 `pm/`。**
   放在仓库根，读者会以为那是 PromptMaster 的覆盖率。
2. **本产品目前没有任何覆盖率门禁**：`pyproject.toml` 里没有 `--cov`、没有 `fail_under`，
   CI 的各 pytest 步骤也不带覆盖率参数。所以"覆盖率是多少"只能现场跑出来：
   `python -m pytest tests/ --cov=pm --cov-report=term-missing`
3. **一次性的终端输出不该进 git**。重跑一次的命令在上面第 2 条，产出可复现的数字比留一份
   会腐烂的快照便宜得多。

## 想看真实现场

```bash
# 三服务套件的 triage 覆盖率（这些文件原本量的东西）
cd releases/triage && python -m pytest tests/ --cov=app --cov=notify_center --cov-report=term-missing
# 本产品
python -m pytest tests/ --cov=pm --cov-report=term-missing
```
