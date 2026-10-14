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


__all__ = ["CSP_NONCE_PLACEHOLDER", "render_csp", "require_token"]
