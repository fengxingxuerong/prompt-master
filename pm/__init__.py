"""PromptMaster —— 提示词自动生成 / 测试 / 评估 / 迭代优化（LangGraph 实现）。

版本号只有一个事实源：这里。`pyproject.toml` 走 `dynamic = ["version"]` 从这里读，
`pm/server.py` 的 FastAPI app 也用它。ModelHub 网关是**另一条发布线**（自己带
`GATEWAY_VERSION`），别把两者混成一个数——历史上这里 2.0.0、server 自报 2.1.0、
pyproject 写 1.1.0、tag 到 v1.4.6，四处各说各话（2026-09-25 收口）。
"""

__version__ = "2.1.0"

__all__ = ["__version__"]
