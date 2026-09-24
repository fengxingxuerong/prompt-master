# tests_modelhub/ —— 这是**脚本目录**，不是 pytest 用例

> 2026-09-25 补此说明。此前 6 个文件里 `def test_` 计数为 0，但文件名以 `_test.py` 结尾，
> 很容易被读成"modelhub 有测试"。实测 `pytest tests_modelhub/ --collect-only` 收 **0 条**
> （`testpaths = ["tests"]`，CI 也只跑 `pytest tests/`），所以这个目录**整条都不在门禁里**。

## 这些文件是什么

v1.1 / v1.1.1 的**验收脚本**：各自 `if __name__ == "__main__"` 直跑，对着一个**活着的**
ModelHub 网关（默认 `http://127.0.0.1:8687`）打场景，把结果写进同名 `*_results.json`。

| 文件 | 覆盖 | 跑法 |
|---|---|---|
| `e2e_test.py` | 端到端验收矩阵（openai SDK / LangChain 两种接入、固定角色、主备切换） | `python tests_modelhub/e2e_test.py` |
| `exception_test.py` | 异常路径 X1-X9（配置缺失/损坏、Key 缺失、无效模型名…必须显式报错） | 同上 |
| `final_extra_test.py` | X8 重复提交 / X9 无效模型名 / S1-S2 浸泡 | 同上 |
| `release_test.py` | 发布套件 U1-U3 + 并发 C1 + 性能 P1-P2 | 同上 |
| `v111_test.py` | v1.1.0 新功能：**流式** / 虚拟密钥 / 用量 / metrics / 控制台 / 持久化 | 同上 |
| `rerun_u2.py` | 单条用例（U2）的一次性重跑脚本，**内含硬编码绝对路径** `D:\projects\prompt-master` | 换机器要先改这一行 |

`*_results.json`（5 份）是**当时那一次跑的产物**，不是当前状态的证据；引用它们当"测试通过"
的凭据前，先确认日期与你手上这份代码对得上。

## 为什么它们进不了流水线

脚本要一个能出网的活网关 + 真上游 Key，而 CI 的原则是"禁止任何真实出网"
（`.github/workflows/ci.yml` 里把 `PM_BASE_URL` 指向死端点 `127.0.0.1:9`）。
所以它们只能在本地按需跑，不能当回归门禁。

## pm/modelhub 现在有哪些自动化保护

1. **静态**：`ruff check` / `ruff format --check` / `mypy --strict` 覆盖 `pm/modelhub/*`
   （2026-09-25 起 HEAD 归绿；此前 51 个 mypy 错长期挂着，等于门禁不把关）。
2. **动态**：`tests/test_modelhub_stream_contract.py` —— SSE 流式通道的契约回归。
   它不是从这些脚本里长出来的，而是因为**一次真实回归没人拦得住**补的：
   `_stream_response()` 里 `sse_gen()` 定义完没有 `return`，`stream=true` 直接拿到 `None`，
   而 `pytest tests/` 全绿。同一次排查还抓到中断错误帧写成 `str + bytes`（必抛 TypeError）。
   这两处 mypy 只要函数带返回值标注就能各抓一次 —— 这也是当初补标注的理由之一。

## 想把它们变成真门禁吗

最小路径：把脚本里的"对活网关打请求"换成 FastAPI `TestClient`（进程内，零出网，
`releases/{lobster,triage}/tests/` 就是这个模式），函数名改成 `test_` 前缀，
放进阶梯 `pytest tests/`。做不到的部分（真上游连通性、性能/浸泡）留作发布前本地跑。
