# 目录收录提交材料（PF-1b）

**为什么单独一页**：收录是 owner 的手动动作，但四处目录要的是同一套字段（名字 / 一句话 /
分类 / 演示 / 安装路径）。话术定死一次，四处复制粘贴即可 —— 每次现编，口径就会漂：
一处写"最好的提示词工具"，另一处写"零调用审计"，目录维护者看到两套说法会直接不收。

## 1. 四处目标

规模是 2026-10-29 第七轮竞品对照的读数（见 `docs/iteration/optimization-mapping.md`）。

| 目录 | 规模 | 提交方式 | 状态 |
|---|---|---|---|
| `ComposioHQ/awesome-claude-skills` | 78,189★ | PR 改 README（也有 issue 模板） | ⏳ 待提交 |
| `VoltAgent/awesome-openclaw-skills` | 53,405★ | PR 改 README | ⏳ 待提交 |
| `VoltAgent/awesome-agent-skills` | 36,512★ | PR 改 README | ⏳ 待提交 |
| `skills.sh` | — | 站点提交表单 | ⏳ 待提交 |

三个目录本轮扫描（关键词级）都**没有**「提示词审计」这一格：`awesome-agent-skills`
有 security / web / SEO 各类 audit 格；`awesome-openclaw-skills` 自述 "curated, not audited"
并维护恶意技能表。格子空着 = 位置还在，但也说明这一格需要解释成本。

## 2. 统一字段（照抄，别改写）

- **名称**：PromptMaster
- **一句话（中）**：给提示词做零调用审计 —— 不花一分钱、不出一次网，直接说这版哪里会翻车；
  判不了就说判不了，不硬给分。
- **一句话（英）**：Zero-call prompt auditing — flags what will break before you spend a token,
  and refuses to grade when the evidence isn't there.
- **分类**：`prompt-engineering`（若目录有 `security` / `audit` 格，同时选它）
- **仓库**：https://github.com/fengxingxuerong/prompt-master
- **演示资产**：`docs/assets/check-demo.png`（真实终端输出 before/after 两屏，不是示意图）
- **安装**：`git clone https://github.com/fengxingxuerong/prompt-master && pip install .`
  （PyPI 包名 `prompt-master` 已备好；**上传成功之后**再改成 `pip install prompt-master`）
- **零调用证据**：`python run.py check <文件>` / `python run.py gate` / `python run.py panel`
  三条都不需要 API Key、不出网

## 3. 提交时别写的东西

| 别写 | 原因 |
|---|---|
| 「最好 / 第一 / 唯一 / 领先」 | 极限词，目录方会删；本仓自己的文档口径也不允许 |
| `pip install prompt-master` | PyPI **尚未上传**（等 `PYPI_TOKEN`）。写了就是给用户一条装不上的命令 |
| 「准确率 XX%」 | 评委可信度当前的公开口径是「不可估」（AUC 95% CI 覆盖 0.5），见 `docs/evaluation.md` |
| 「自动优化提示词」 | 本产品**不做**搜索式优化（那是明确不采纳的方向），被按这个预期收进来会天天被问为什么不能自动改 |

## 4. 收录后回填

1. 把 PR / 收录链接补进上表「状态」列，写成 `✅ 2026-MM-DD · <链接>`；
2. 同步 `docs/iteration/optimization-mapping.md` 的 **PF-1b** 行；
3. 四处都收完后，这一页可以从 `docs/operations.md` 的「尚待 owner 亲决」里摘掉。
