# 部署与浏览器运行时验证报告（2026-09-13）

对 `docs/qa_report_2026-09-13.md` 两项遗留项（Dockerfile 构建验证、playwright 浏览器运行时校验）
的逐项处理结果。环境事实：**本机无 docker / podman**，**未安装 python playwright**，
但存在 node `playwright-core@1.62.1` 与本地缓存的 chromium（`chromium-1234`）。

---

## 一、Dockerfile 构建验证

### 状态

| 项 | 结果 |
|---|---|
| 真实 `docker build` | ❌ **无法执行**：`docker` / `podman` 均未安装（`command not found`） |
| 替代验证（构建 + 启动 + 健康检查 + 任务闭环） | ✅ **已完成并通过**（见下） |
| 静态一致性检查（COPY 可达性 / 端口 / 非 root / 挂载点） | ✅ 通过 |
| 验证中发现并修复的问题 | 1 处（构建残留 `build/` + `*.egg-info` 留在镜像层） |

### 替代验证方式与原理

Dockerfile 里**真正有失败风险**的只有两步：`pip install .`（依赖能否装全）与
入口能否起来 + `/api/health` 是否可答。把这两步在**干净 venv** 里按镜像顺序原样复刻，
即可覆盖构建/启动失败的主要成因：

```bash
# 一键复现（脚本已入库）
PYTHON="<任一 python3.11+>" bash examples/docker_step_sim.sh
```

脚本执行的步骤与 Dockerfile 一一对应：

| 脚本步骤 | 对应 Dockerfile | 本次实测结果 |
|---|---|---|
| 1) COPY pyproject.toml + pm/ | `COPY pyproject.toml ./` / `COPY pm/ ./pm/` | ✅ |
| 2) 干净 venv + `pip install .` | `RUN pip install .` | ✅ 构建出 `prompt_master-1.0.0-py3-none-any.whl`（140,181 字节），依赖全部装成 |
| 3) 校验无开发依赖 | 设计意图（只装运行时依赖） | ✅ `pytest` / `ruff` / `mypy` / `coverage` 全部不存在 |
| 4) COPY 入口与模板 | `COPY run.py run_server.py ./` / `COPY case_templates/` | ✅ |
| 5) 启动 + 健康检查 | `CMD ["python","run_server.py","--host","0.0.0.0","--port","8080"]` + `HEALTHCHECK` | ✅ `/api/health` → `200 {"status":"ok","service":"prompt-master"}` |
| 6) 任务闭环 | 运行时行为 | ✅ 提交任务 → `max_iterations`、报告 980 字节含「## 评分总览」 |
| 7) 构建残留检查 | （本次新增 `rm -rf build ./*.egg-info`） | ✅ 修复后无残留 |

### 静态一致性检查（脚本化断言，输出见下）

```
COPY 指令: [('pyproject.toml','./'), ('pm/','./pm/'), ('run.py','run_server.py'), ('case_templates/','./case_templates/')]
COPY 可达性: OK                      # 4 个 COPY 源都存在，且均未被 .dockerignore 排除
HEALTHCHECK 端口=8080  CMD 端口=8080  一致=True
非 root: True | /data 挂载点: True    # PM_LOG_DIR=/data/logs、PM_TASK_DB=/data/tasks.db
```

### 本次发现并修复

| 编号 | 问题 | 证据 | 修法 |
|---|---|---|---|
| D1 | `pip install .` 会留下 `build/` 与 `prompt_master.egg-info/`，随镜像层一起被保留（几百 KB 起，且污染工作目录） | 替代验证第 7 步实测列出这两个目录 | Dockerfile 的 RUN 中加入 `rm -rf build ./*.egg-info` |

### 仍需在有 docker 的机器上验证的部分（替代验证覆盖不到）

1. 镜像层缓存与 `--no-cache` 构建差异；
2. `USER pmuser`（uid 10001）在容器内的实际权限 —— 本机无法验证 uid/权限语义；
3. `/data` 卷挂载后的读写权限（`chown -R pmuser /data` 是否覆盖宿主卷）；
4. `HEALTHCHECK` 由 Docker 守护进程按 interval/retries 调度时的真实行为；
5. 网络 namespace 下的出网能力（容器内访问 LLM 端点）。

```bash
# 首测命令（有 docker 的机器）
docker build -t prompt-master .
docker run --rm -p 8080:8080 -v "$PWD/data:/data" -e PM_API_KEY=sk-xxx prompt-master
curl -s http://127.0.0.1:8080/api/health
docker inspect --format '{{.State.Health.Status}}' <container>   # 应为 healthy
```

---

## 二、playwright 浏览器运行时校验

### 状态

| 项 | 结果 |
|---|---|
| python `playwright` | ❌ 未安装（`ModuleNotFoundError: No module named 'playwright'`） |
| **运行时校验本身** | ✅ **已完成并通过（6/6）** —— 用 node `playwright-core` + 本地缓存 chromium，不依赖 python 包 |
| 依赖清单与完整步骤（python 路线） | 见下（供其它环境复现） |

### 本次实际执行的路线（node + 本地 chromium，无需下载浏览器）

```bash
# 1) 起一个控制台服务（演示后端，零模型调用）
PM_FAKE_BACKEND=progress PM_LOG_DIR=logs/pmcheck/browserlogs PM_TASK_DB=logs/pmcheck/browser.db \
  ./.venv/Scripts/python.exe run_server.py --port 8097
# 2) 真实 Chromium 跑 6 项断言（脚本已入库）
node examples/browser_runtime_check.cjs http://127.0.0.1:8097/
```

实测输出：

```
PASS  加载期无 CSP 违规 — violations=0
PASS  tab 事件委托生效（切换到流水线面板） — tabs=4, active=true
PASS  内联 style 属性被 CSP 拦掉（正向对照） — computed 未生效=true, violations=1
PASS  XSS 回归：围栏内 <img onerror> 保持转义 — {"available":true,"escaped":true,"imgs":0,"xssFired":false}
PASS  无未捕获 JS 错误（排除故意触发项） — errors=0
PASS  无真实资源加载失败（favicon 除外） — failures=0

汇总：6/6 通过
```

**为什么这组断言值钱**：第 3 条是**正向对照** —— 故意往 DOM 写内联 `style` 属性，
必须被 CSP 拦掉且产生一条 `style-src` 违规；如果策略没生效，这条会红。
第 4 条把围栏内的 `<img src=x onerror=...>` 真正插进 DOM，断言既不产生 `<img>` 元素、
`onerror` 也没执行（XSS 修复的运行时证据，此前只有源码层与 node 单元层证据）。

### 依赖清单

| 路线 | 依赖 | 版本/来源 | 体积 |
|---|---|---|---|
| **A. node（本次使用）** | `playwright-core` | 工作区 node_modules，`1.62.1` | ~几 MB（不含浏览器） |
| | Chromium 二进制 | 已缓存：`%LOCALAPPDATA%\ms-playwright\chromium-1234\chrome-win64\chrome.exe` | ~150 MB（复用已有缓存，无下载） |
| **B. python（其它环境）** | `playwright`（pip） | `pip install playwright`（≥1.40） | ~几十 MB |
| | Chromium | `playwright install chromium`（自动下载） | ~150 MB |
| | 系统依赖（Linux） | `playwright install-deps chromium` | 视发行版 |

### 完成该运行时校验的完整步骤（python 路线）

```bash
# 1) 安装依赖（建议装在项目 venv 里，避免污染系统环境）
cd D:\projects\prompt-master
.venv/Scripts/python.exe -m pip install playwright
.venv/Scripts/python.exe -m playwright install chromium     # 下载浏览器（约 150MB）
# Linux 额外：.venv/bin/python -m playwright install-deps chromium

# 2) 起控制台服务（演示后端即可，不消耗模型调用）
PM_FAKE_BACKEND=progress .venv/Scripts/python.exe run_server.py --port 8097

# 3) 跑校验（本仓库脚本是 node 版；python 版按同一断言语义改写即可）
#    node 版：node examples/browser_runtime_check.cjs http://127.0.0.1:8097/
#    python 版核心 API 对照：
#      from playwright.sync_api import sync_playwright
#      browser = p.chromium.launch(); page = browser.new_page()
#      page.add_init_script("window.__csp=[];document.addEventListener('securitypolicyviolation',e=>window.__csp.push(e.violatedDirective))")
#      page.goto("http://127.0.0.1:8097/")
#      assert page.evaluate("window.__csp") == []                       # 无违规
#      page.click('#tabs .tab[data-tab="pipeline"]')                    # 委托生效
#      assert page.evaluate("md2html('```\\n<img src=x onerror=\"window.__xss=1\">\\n```').includes('&lt;img')")
#      assert page.evaluate("window.__xss") is None                     # XSS 未触发

# 4) 断言失败时：page.on("console", ...) 收集到的 CSP 报文会指明是哪条指令被拦截
```

> 注意：`no_proxy=127.0.0.1,localhost` 必须设置（或 curl 用 `--noproxy '*'`），
> 否则本机 Clash 代理会拦截 127.0.0.1 请求，把代理行为误判成服务缺陷（本次实测踩过）。

---

## 三、覆盖率补齐（对应 qa_report 第七节遗留项）

| 文件 | 补前 | 补后 | 新增用例 |
|---|---|---|---|
| `pm/llm.py` | 77% | **100%** | `tests/test_llm_branches.py`（36 例）：429 退避重试 / 退避预算耗尽 / 非限流直接上抛 / 单 Key 退避加倍 / Key 池轮换 / 限流误判回归（400+100429 tokens） / 通道 A 命中与降级（实例 / dict / None / 异常） / 强制文本通道 / 端点不兼容记忆（只记确定性错误） / 通道 B 降温重试与全败 / token 记账 / JSON 抽取边界 / anthropic 分支（桩模块） |
| `pm/cache.py` | 83% | **100%** | `tests/test_cache_branches.py`（10 例）：更新已存在键必须落盘 / 容量淘汰 / 写盘失败只告警 / 损坏与非 dict 文件容错 / `clear()` unlink 异常 / 单例与开关 / 演示后端禁用缓存 |
| 全项目 | 91% | **94%** | 合计新增 46 例，pytest 由 326 → **372 passed** |

两个目标文件均已达 **100% 行覆盖**（`pm/llm.py`、`pm/cache.py`）。
全项目未覆盖的 173 行集中在 `server.py`（异常兜底）、`nodes/*`（降级路径）与
`assertions.py`（正则子进程隔离）等防御分支，属可接受的测试边界。
