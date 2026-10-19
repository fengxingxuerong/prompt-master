# extras/ —— 可选附件

这里的东西**不随 `pip install .` 安装**，也不在 CI 主门禁里。它们服务的是"想点开看看"的场景，
不是被集成的接口；主包只背 CLI / REST / MCP 三条。

## desktop.py —— 原生桌面窗口（Tkinter）

```bash
python run.py desktop          # 从仓库根运行；pm/desktop.py 只是转发壳
pytest tests_extras -q         # 附件的回归（80 条，需要本机有 tkinter）
```

五个标签：① 体检 · ② 考卷 · ③ 两版差分 · ④ 优化闭环 · ⑤ 账本与仪表。
前四个动作全部打同一条 CLI（`run.py check/diff/--task/...`），界面**不新增判据** ——
窗口里看到的判定与命令行逐字相同。

### 为什么从主包移出（2026-10-19）

它是 2087 行 Tkinter 代码，占产品代码约 8%，却只服务一个非集成入口。移出后：

- 主包不再需要 tkinter / 显示器相关依赖；
- CI 主套件不再受窗口层 skip 影响（历史上它曾把 Linux 覆盖率从 96.99% 拉到 89.66%）；
- `run.py desktop` 仍然可用（`pm/desktop.py` 转发到本文件），既有用户不被静默拿走功能。

### 代价（明说）

- **附件没有门禁**：改坏了不会有人红。手动跑 `pytest tests_extras` 是唯一保护。
- **装包态不可用**：`pip install .` 之后 `prompt-master desktop` 会给出提示并返回 2，
  因为 `extras/` 不在 wheel 里。要长期用就从检出目录跑，或 `pip install -e .`。
