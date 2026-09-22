# 智能体接入说明（ModelHub 统一接入层）

> 版本 1.0 · 2026-09-21 · 适用 ModelHub v1（PromptMaster 项目内 `pm/modelhub/`）

任何框架的智能体（openai SDK / LangChain / 原生 HTTP / Coze / Dify / 自研程序）都按
**同一个 OpenAI 兼容接口**接入，不需要关心上游是商汤 SenseNova 还是 AMD Radeon 端点，
也不需要关心自动切换。

---

## 1. 前置条件

1. 网关已启动（见《部署与操作手册》第 2 节）：
   ```bash
   cd D:\projects\prompt-master
   .venv\Scripts\python.exe run_modelhub.py --port 8687
   ```
2. 模型池配置就绪：`config/modelhub.json`（12 模型主备链）。
3. 可选鉴权：`.env` 里设 `PMH_GATEWAY_TOKEN=<你的令牌>` 后，所有请求需带
   `Authorization: Bearer <令牌>` 或 `X-API-Key: <令牌>`；未设则本地开放访问。

## 2. 接入三要素（所有框架通用）

| 要素 | 值 | 说明 |
|---|---|---|
| base_url | `http://127.0.0.1:8687/v1` | OpenAI 兼容 |
| api_key | `PMH_GATEWAY_TOKEN` 的值 | 未设令牌时填任意非空字符串 |
| model | 可省略 / 池内任一模型名 | 省略 = 按 priority 主备链自动选择与切换 |

**关键约定：`model` 可以不传。** 网关按 `config/modelhub.json` 的 priority 顺序
选择模型；某模型报错/超时/限流/空返回时自动切换下一个，调用方无感知。
指定 `model` 时该模型优先尝试，失败仍会自动切换兜底。

## 3. 三种接入方式（已实测通过）

### 3.1 openai SDK

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8687/v1", api_key="local-dev", timeout=90)
r = client.chat.completions.create(
    model="deepseek-v4-flash",            # 可省略 → 自动按主备链
    messages=[{"role": "user", "content": "你好"}],
    extra_body={
        "agent": "my-agent",              # 智能体标识（台账按它归属检索）
        "role": "assistant",              # 可选：绑定固定角色（config/roles.json）
        "max_tokens": 2048,
    },
)
print(r.choices[0].message.content)
print(r.modelhub.failovers)               # 扩展字段：本次切换了几次
```

### 3.2 LangChain（ChatOpenAI）

```python
from langchain_openai import ChatOpenAI

llm = ChatOpenAI(
    base_url="http://127.0.0.1:8687/v1",
    api_key="local-dev",
    model="glm-5.2",                      # 可省略
    max_tokens=2048,
)
resp = llm.invoke("你好", extra_body={"agent": "my-langchain-agent", "role": "analyst"})
print(resp.content)
```

### 3.3 原生 HTTP（curl / 任何语言）

```bash
curl http://127.0.0.1:8687/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer local-dev" \
  -d '{"messages":[{"role":"user","content":"你好"}],
       "agent":"my-agent","role":"assistant","max_tokens":1024}'
```

## 4. 角色绑定（固定人设）

角色系统提示词**固定**在 `config/roles.json`（10 个内置角色：clarifier / optimizer /
evaluator / evaluator_b / arbiter / comparator / reviser / target / assistant / analyst）。

- 请求带 `"role": "assistant"` → 网关自动把该角色的 system_prompt 注入为
  messages 的第一条 system 消息（messages 里已有 system 时以请求为准，不覆盖）；
- 角色不存在 → HTTP 422 显式报错并列出可用角色，**不静默**；
- 角色内容与模型无关：切换到任何模型，同一 role 注入的人设完全一致（已在
  端到端测试 E 场景实测：deepseek-v4-flash 与 glm-5.2 输出同人设）。

## 5. 智能体注册（可选，便于台账归属）

```bash
curl -X POST http://127.0.0.1:8687/v1/agents \
  -H "Content-Type: application/json" \
  -d '{"name":"my-agent","framework":"dify","default_role":"assistant",
       "description":"客服分流机器人"}'
```

- 注册是幂等的；`default_role` 让该智能体缺省绑定固定角色；
- 不注册直接在请求里带 `agent` 字段也可以（自动登记为 auto-registered）；
- `GET /v1/agents` 查看全部已接入智能体；`DELETE /v1/agents/{name}` 注销。

## 6. 管理端点速查

| 端点 | 方法 | 用途 |
|---|---|---|
| `/v1/models` | GET | 模型池清单（12 条目，OpenAI 格式） |
| `/v1/chat/completions` | POST | 对话补全（自动切换） |
| `/v1/pool/status` | GET | 断路器/冷却状态（哪个模型在冷却期） |
| `/v1/ledger?model=&success=&type=&since=&until=&limit=` | GET | 台账检索 |
| `/v1/ledger/stats` | GET | 台账汇总（成功率/切换次数/按模型分布） |
| `/v1/roles` | GET | 固定角色清单 |
| `/v1/agents` | GET/POST | 智能体清单 / 注册 |
| `/v1/agents/{name}` | DELETE | 注销智能体 |
| `/api/health` | GET | 健康检查 |

## 7. 已知约束（v1 如实声明）

- **仅非流式**：`stream=true` 返回 HTTP 400 显式报错（流式在 roadmap）；
- 智能体框架若不支持透传 `agent`/`role` 扩展字段，可省略——不影响接入，
  只是台账归属记为空；LangChain 老版本若无 `extra_body`，可用 `model_kwargs` 或直接原生 HTTP；
- 上游端点本身限流（商汤 429）时，切换链可能连续命中 429，最终 502 返回；
  这是"上游配额耗尽"的如实上报，不是网关故障；稍后重试即可（熔断冷却自愈）。

## 8. 五分钟接入自查清单

1. `GET /api/health` → `{"status":"ok"}`
2. `GET /v1/models` → 能看到 12 个模型
3. POST 一条最小对话（带 `agent` 标识）→ 拿到 content
4. `GET /v1/ledger?limit=5` → 能查到刚才那条调用
5. 以上四步都过 = 接入成功。
