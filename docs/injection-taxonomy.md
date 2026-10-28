# 注入装束的分类学覆盖表（PE-6）

> 竞品都在用分类学说话：AI-Infra-Guard 挂 SkillTrustBench T01–T09，
> promptfoo 红队有 157 个插件的逐字 ID 清单。本表回答一个问题：
> `pm/injection_pack.py` 的 10 种装束各自落在公开分类学的哪一格、**还缺哪几格**——
> 让缺口可数，而不是靠"装束有 10 种"的断言。
> 守卫：`tests/test_injection_taxonomy.py`（装束清单漂移 ⇒ 本表必须先更；
> 引用了白名单外的 promptfoo ID ⇒ 红）。

## 一、先说清两套分类学的轴，再谈覆盖

- **SkillTrustBench T01–T09（腾讯朱雀）**：分类对象是**技能文件**（SKILL.md、脚本、
  权限声明）——评测"恶意技能长什么样、扫描器能不能抓"。逐字可核的只有
  **T09 =「不安全编码行为」**（硬编码凭证、过度权限声明、缺输入校验）；
  其余八类的名称目前只出现在图片里，**本表不硬编**。关键结论：它与我们的
  「运行时注入技法」**不同轴**——我们的装束是单轮、纯文本、无代码无权限无持久化，
  T 系里代码执行/权限/持久化那些格对我们不是"缺口"而是"不适用"。
  能同轴的只有提示注入一族，而我们的全部装束都在这一族里。
- **promptfoo 红队插件（157 个，ID 逐字可核，抓取自官方插件文档 2026-10-29）**：
  技法级，与我们的装束同轴 ⇒ 本表以它为主分类学。引用的每个 ID 都在守卫白名单里。

## 二、10 种装束 × promptfoo 对等格

整包横跨两格：每条装束都是 `indirect-prompt-injection`（指令混在正常数据里的单轮间接注入，
与 injection_pack docstring 的适用性裁剪一致），且判定口径就是 `hijacking`
（存活 = 校验码被回显 = 目标被劫持）。下表是每条装束**额外**落进的更细一格：

| # | 装束 id | 中文名 | 细分对等格 | 对齐说明 |
|---|---|---|---|---|
| 1 | `fake_system_notice` | 伪造系统通知+权限声明 | `system-prompt-override` | 覆盖 |
| 2 | `instruction_revocation` | 作废式更正 | `system-prompt-override` | 覆盖 |
| 3 | `fake_tool_result` | 伪造工具返回 | `special-token-injection` | 部分对齐：借工具输出通道 + 格式标记伪装系统层 |
| 4 | `role_assignment` | 角色重指派 | `system-prompt-override` | 覆盖（身份重写） |
| 5 | `authority_ticket` | 伪造成规审计工单 | `indirect-prompt-injection` | 部分对齐：promptfoo 无「伪造权威来源」专格 |
| 6 | `delimiter_escape` | 定界符逃逸 | `special-token-injection` | 部分对齐：我们用 markdown/XML 定界，该格还含会话格式特殊标记 |
| 7 | `html_comment` | 注释通道 | `indirect-prompt-injection` | 部分对齐：隐藏通道无专格 |
| 8 | `english_system_update` | 英文系统更新话术 | `system-prompt-override` | 覆盖（跨语言变体） |
| 9 | `few_shot` | 少样本示范诱导 | `cca` | 部分对齐：`cca` 支持多轮历史操纵，我们是单轮示范 |
| 10 | `zero_width_obfuscation` | 零宽字符混淆 | `ascii-smuggling` | 部分对齐：同族不同技法（见第三节） |

计数：**10/10 有对等格；其中 4 格覆盖、6 格部分对齐**（对齐说明即差距所在）。

## 三、promptfoo 注入族里，我们没覆盖的格（可数缺口）

| promptfoo 格 | 为什么缺 | 性质 |
|---|---|---|
| `prompt-extraction` | 装束完全可行（「输出你的系统提示」），本库不含 | **真缺口**（防提取也是抵御力，判定口径可沿用回显检测） |
| `data-exfil` | 装束可行（「把上表发到 http://…」），本库不含 | **真缺口**（存活判定靠校验码回显，外带类要换判定口径才能测） |
| `ascii-smuggling` 的标准技法 | 我们只做零宽 U+200B 拆词；标准形态是 Unicode tag 区（U+E0000 段）藏指令 | 同族缺口（换字符族即补，判定口径不变） |
| `special-token-injection` 的真特殊标记 | 我们只做 markdown/XML 定界；未用 `<|im_end|>` 类真 token | 同族缺口 |
| `cca` 的多轮形态 | `cca` 支持多轮历史操纵，我们单轮 | 边界外（单轮是本库 docstring 写明的裁剪，非缺口） |
| `agentic:memory-poisoning` / `rag-poisoning` / `mcp` | 持久化 / RAG / 工具层通道，单轮文本装束做不到 | 不适用（要在这些轴上测需另一套载体） |

**计数口径：真缺口 2 格，同族缺口 2 格，边界外/不适用 5 格。**

## 四、诚实边界

- promptfoo ID 清单抓取自其官方插件文档（2026-10-29，157 个插件）；本表只引用注入相关族。
- **T01–T08 的名称未核实**（官方页正文与公开报道里只有 T09 逐字可核，其余在图片里），
  本表因此不给出任何 T 编号对应关系——宁可留白也不编造。
- 「对等格」是映射判断，不是官方对应：promptfoo 并没有为本库的装束提供官方映射表。
- 缺口是**装束层**的缺口：补 `data-exfil` 类装束时要连带改存活判定（回显检测测不到外带），
  不是抄一行模板就行。
