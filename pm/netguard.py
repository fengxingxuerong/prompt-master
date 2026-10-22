"""HTTP 边界的两件共享事：CSP（含逐响应 nonce）与静态 token 鉴权。

为什么从 `pm/server.py` 里搬出来：面板（`pm/panel.py`）要挂同一个 `app`，
于是它既要用 `require_token`，又要用 `CSP_NONCE_PLACEHOLDER` 渲染自己的页面。
留在 server.py 里就只有两条路 —— 面板 `import pm.server`（server 反过来 import 面板
就是循环导入，且顺序一变就炸），或者把这两段抄一遍（那就是第三份 token 比较，
`pm/modelhub/server.py:71` 已经有一份"同 pm/server.py 的 require_token"的注释在提示这件事）。

`pm/server.py` 继续 re-export 这两个名字，所以 `from pm.server import require_token`
与 `server_mod.CSP_NONCE_PLACEHOLDER` 这类既有引用面（含测试）一条都不用改。
"""

from __future__ import annotations

import os
import secrets

from fastapi import Header, HTTPException

# 控制台与面板都是零外部依赖的单页（CSS/JS/canvas 全内联，无任何 CDN 请求），所以能上
# `default-src 'self'`：即便有人往报告里塞了 <img src=外部地址>，外联也会被浏览器掐掉。
#
# `script-src` / `style-src` 用**逐响应 nonce** 而不是 'unsafe-inline'：
# 'unsafe-inline' 一旦打开，注入进来的内联脚本照样能跑，等于把 CSP 降级成"只防外联"。
# 代价是页面自己也不能用内联事件属性（onclick=...）与 style 属性 —— 那部分已改成
# data-* + 事件委托与工具类（见 pm/web/），所以这里可以不给 'unsafe-inline'。
CSP_NONCE_PLACEHOLDER = "__CSP_NONCE__"


def render_csp(nonce: str) -> str:
    return (
        "default-src 'self'; "
        f"script-src 'self' 'nonce-{nonce}'; "
        f"style-src 'self' 'nonce-{nonce}'; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "font-src 'self' data:; "
        "object-src 'none'; "
        "base-uri 'none'; "
        "form-action 'self'; "
        "frame-ancestors 'none'"
    )


def require_token(x_api_key: str | None = Header(default=None)) -> None:
    """可选的静态 token 鉴权：设了 `PM_API_TOKEN` 就强制校验。

    服务默认能启动就能烧钱，没有任何访问控制是不安全的（H3）。
    未设 `PM_API_TOKEN` 时保持旧行为（本地开发不碍事），但 server.py 会记一条告警。
    """
    expected = os.getenv("PM_API_TOKEN", "").strip()
    if not expected:
        return
    # 常量时间比较：字符串 != 可被逐字节计时侧信道分辨出"前缀对了多长"
    if not x_api_key or not secrets.compare_digest(x_api_key, expected):
        raise HTTPException(status_code=401, detail="缺少或错误的 X-API-Key")


def is_loopback_bind(host: str) -> bool:
    """host 参数是否只绑本机回环（含空串 = uvicorn 缺省 127.0.0.1）。

    `::` 与 `0.0.0.0` 是"所有网卡"，不是回环——容易误当成"还是本机"。
    """
    return (host or "").strip().lower() in {"", "127.0.0.1", "localhost", "::1", "[::1]"}


def refuse_exposed_without_token(
    host: str, *, token_env: str = "PM_API_TOKEN", service: str = "PromptMaster API"
) -> str | None:
    """fail-closed 门（2026-10-23）：非回环绑定且未设 token 时返回拒绝文案，否则 None。

    旧姿态是"只打一条 warning 照常启动"：写接口没有任何访问控制，README 说
    「公网暴露前必须处理」却没有机制兜底——Dockerfile 的示例命令甚至不带
    PM_API_TOKEN 就映射端口（`-p 8080:8080` + 容器内 `--host 0.0.0.0`）。
    ModelHub 网关同形：未设 `PMH_GATEWAY_TOKEN` 时 `/v1/keys` 可**无凭据**
    签发/删除/列出虚拟密钥。三个启动器（run_server / run_modelhub / panel）
    共用这一份判据，谁也不许各自演化出第四种说法。
    """
    if is_loopback_bind(host):
        return None
    if (os.getenv(token_env) or "").strip():
        return None
    return (
        f"拒绝启动：--host {host} 会把 {service} 暴露给本机以外的调用者，"
        f"而 {token_env} 未设置——写接口没有任何访问控制，任何能连到端口的人"
        "都能消耗你的 API 额度（ModelHub 网关还能签发/删除密钥）。二选一：\n"
        f"  ① 设 {token_env}=<随机长串> 后再对外监听；\n"
        "  ② 只在本机使用：--host 127.0.0.1（默认值）。\n"
        "（2026-10-23 起由「启动时 warning」改为「拒绝启动」，退出码 2 = 配置错误。）"
    )


__all__ = [
    "CSP_NONCE_PLACEHOLDER",
    "is_loopback_bind",
    "refuse_exposed_without_token",
    "render_csp",
    "require_token",
]
