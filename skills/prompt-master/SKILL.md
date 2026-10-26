---
name: prompt-master
description: 体检、差分与门禁提示词（零调用、零出网），以及「生成 → 用例 → 评估 → 修订」的完整优化闭环（计费）。当用户要检查/优化/对比提示词，问"这版提示词有没有硬伤""改完是不是变好了""怎么给提示词上 CI 门禁"，或说到 prompt check / prompt gate / prompt diff / optimize this prompt / PromptMaster 时使用。默认只做零调用体检；跑计费闭环必须先拿到用户的明确同意。
---

# PromptMaster

把「让 AI 分析销售数据」这种一句话需求，变成**被测过、被评过、被证伪过一轮**的提示词，并如实说清哪里还不行。
产出四件：最终提示词 / 交付报告 / 可选 `SKILL.md` / 机器可读 JSON。

**它不是提示词润色器。** 按花不花钱排，只有三种用法：

| 你想干的事 | 命令 | 成本 |
|---|---|---|
| 体检一版已有提示词 | `prompt-master check --prompt-file x.md` | **零调用、零出网**；命中规则退 1 |
| 只拦本次新引入的坑（CI 门禁） | `prompt-master diff --base-ref origin/main x.md` | **零调用**；有新引入退 1 |
| 跑完整闭环：生成 → 用例 → 评估 → 修订 | `prompt-master --task "让 AI 分析销售数据"` | 计费；先 `--dry-run` |

## 运行模式：加载本技能**不**授权花钱

1. **默认 = 只读体检**（零调用、零出网，不需要 API Key）：`check`（规则体检一版提示词）、
   `diff`（版本差分，含 `--base-ref`，**你的 markdown/文本提示词走这条**）、
   `doctor`（配置逐项核对）、`history` / `library`（历史运行与资产库）、
   `calibrate --aggregate`（账本重算）。这些命令可以直接跑，先免费看一眼再决定要不要花钱。
   ⚠️ `gate` 是**本仓自用**的增量门禁：它的对象是 Python 模块里的「全大写 = 字符串常量」提示词模板
   （默认 `pm/prompts.py`），**喂 markdown 会退 2 并报"源码无法解析"**。给你的提示词文件上门禁，用
   `diff --base-ref origin/main 你的.md`（或本仓根下的 GitHub Action）。
2. **计费闭环**：只有当用户**明确要求**"跑一轮 / 帮我优化 / 生成一版"时才走。走之前先 `--dry-run` 报出调用数区间；
   交互终端下它会先报价、要一句 y/N；非交互场景由 `PM_MAX_LLM_CALLS` 预算总闸兜底。
3. 不要为了"顺手试一下"而用 `--task` / `submit`：一轮完整闭环是 45~102 次调用（`--fast` 档 12~22 次）。

## 退出码：给 Agent 做分支的协议

| 码 | 含义 | 你该怎么做 |
|---|---|---|
| 0 | 达标 / 没有新引入 | 取产物 |
| 1 | 未达标但已交付；对 `check`/`diff` 是"命中规则 / 新引入规则问题" | 读报告里的遗留问题，**不是跑挂了** |
| 2 | 参数或配置错 | 改参数，不要原样重试 |
| 3 | 运行失败 | 看 `errors`，原样重试是安全的 |

加 `--json` 时 stdout 恰好一个 JSON 对象，进度全走 stderr（这是本技能能被脚本消费的原因）。

⚠️ `gate` 把 3 的语义收窄了：**首次推送、没有基线可对比**时它按放行处理并打横幅
（配 `--require-baseline` 才判失败）——那是「没比成」，不是「检查通过」，别读反。

## 你能拿到真值的那部分，交给真值

`--cases-file cases.json`：用例里 `expected` 走 `--assert-mode rule` 时**有一票否决权**（不靠模型打分）；
`custom:<名>` 可以挂你自己的断言函数。拿不到真值的部分，它选择如实承认，而不是假装有。

## MCP 接入（推荐，13 个工具，stdio）

```json
{"command": "python", "args": ["-m", "pm.mcp_server"]}
```

计算类工具（`prompt_check` / `prompt_diff` / `cases_check` / `estimate_cost`）直接跑 CLI、零调用；
长任务类（`optimize_submit` / `optimize_status` / `optimize_wait` / `optimize_report`）转发本机常驻服务。

## 安装

```bash
pip install .             # 之后有 `prompt-master` 命令（与 `python run.py` 同一套实现、同一个 main）
python run.py --selftest  # 不配 Key：验代码能不能跑（不验效果）
python run.py doctor      # 逐项核对 Python / .env / Key / 角色 / 日志 / 端口 / 常驻服务 / MCP
```

真实运行最少只填三个字段：`PM_PROVIDER`（`openai` 兼容端点或 `anthropic`）、`PM_API_KEY`、`PM_BASE_URL`。

## 诚实边界（用之前先知道）

1. **这把尺子还没对准**：评委判别力的读数是 AUC 0.623（95% CI 覆盖 0.5，即"不可采信"）、留一折净增益 +0.000。
   所以 `Δ`（比你原稿好多少）**只在基线有效时才有意义**，零有效样本时它会报"不可采信"。
2. **达标 ≠ 有效果**：分数是方向参考，不是验收依据。
3. **注入防御是模型相关的**：同一提示词在 A 端点免疫、B 端点可能被一句话劫持。
4. 因此当前能放心用的是：零调用能力线（`check` / `diff` / `gate`）与资产库；`Δ` 类数字仍在实验室状态。

发现与本技能描述不符的行为（README 说能跑、实际退 2）时，请直接当缺陷报，不要照文档猜。

## 去哪读更多

| 想看 | 去哪 |
|---|---|
| 全部 CLI 参数与子命令、Agent 调用协议、MCP 工具清单 | [`docs/agent-cli-guide.md`](../../docs/agent-cli-guide.md) |
| 给 Agent 的接入契约（MCP 工具表 + 四态退出码） | [`docs/agent-skill.md`](../../docs/agent-skill.md) |
| 评测方法学与全部实测（含负结论） | [`docs/evaluation.md`](../../docs/evaluation.md) |
| HTTP 接口 | [`docs/rest-api.md`](../../docs/rest-api.md) |
| 工程口径（门禁、覆盖率地板、部署） | [`docs/operations.md`](../../docs/operations.md) |