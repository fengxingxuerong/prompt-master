"""PromptMaster Web 控制台 —— Agent 风格富 SPA（单 HTML，内联 CSS/JS/canvas，零外部依赖）。

布局：
- 左栏：任务/对话历史（每个 run 一条，带状态徽章 + 实时分数）
- 中栏：选中任务后的四标签工作区
  · 对话：提需求（聊天输入）→ agent 实时反馈卡片 → 最终提示词 + 报告渲染
  · 流水线：节点级 trace 时间线（clarify→optimize→...→report），按迭代分组
  · 提示词：版本切换 + 全文查看 + 双版本 diff + 一键复制
  · 评分：维度雷达图（canvas）+ 版本分数曲线（canvas）+ 聚合/基线 Δ/盲评结论
- 底部：聊天输入条（agent 感）

由 server.py 的 GET / 路由提供。后端 /api/status 已暴露 trace/evaluations/prompt_version_list
等富字段（见 scheduler._progress_of），本页直接消费。

源码组织（2026-09-18 从单文件 pm/web.py 拆包）：
- `_css.py`    → 样式（CSS 常量）
- `_body.py`   → HTML 骨架（BODY_HTML 常量）
- `_js.py`     → 脚本（JS_* 常量，按功能域分段）
三个部分在下方组装为同一个 `WEB_CONSOLE_HTML`（运行时仍是单 HTML、内联零外部依赖、
CSP nonce 占位符机制不变）。`from pm.web import WEB_CONSOLE_HTML` 的导入面完全兼容。
"""

from __future__ import annotations

from ._body import BODY_HTML
from ._css import CSS
from ._js import (
    JS_CANVAS,
    JS_CHAT,
    JS_CORE,
    JS_EVENTS,
    JS_HISTORY,
    JS_INIT,
    JS_MD,
    JS_PIPELINE,
    JS_PROMPT,
    JS_RACE,
    JS_SCORE,
    JS_TOOLS,
)

# 拼接顺序即原 pm/web.py 的出现顺序（JS 函数提升保证运行顺序不变；
# 顶层执行语句集中在 JS_INIT，最后一个拼接）。
_JS = "\n".join(
    [
        JS_CORE,
        JS_CHAT,
        JS_PIPELINE,
        JS_PROMPT,
        JS_SCORE,
        JS_CANVAS,
        JS_MD,
        JS_TOOLS,
        JS_RACE,
        JS_EVENTS,
        JS_HISTORY,
        JS_INIT,
    ]
)

WEB_CONSOLE_HTML = (
    "<!DOCTYPE html>\n"
    '<html lang="zh-CN">\n'
    "<head>\n"
    '<meta charset="UTF-8">\n'
    '<meta name="viewport" content="width=device-width, initial-scale=1.0">\n'
    "<title>PromptMaster · Agent</title>\n"
    '<style nonce="__CSP_NONCE__">\n'
    f"{CSS}\n"
    "</style>\n"
    "</head>\n"
    f"{BODY_HTML}"
    "\n"
    '<script nonce="__CSP_NONCE__">\n'
    f"{_JS}\n"
    "</script>\n"
    "</body>\n"
    "</html>\n"
)

__all__ = ["WEB_CONSOLE_HTML"]
