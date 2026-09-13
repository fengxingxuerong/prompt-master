"""目标模型家族档案：按 target_model 关键字命中后注入对应的写法指引。

（自原 pm/nodes.py「工具函数」段拆出，供 optimize 节点使用。）
"""

from __future__ import annotations

_MODEL_PROFILES: tuple[tuple[tuple[str, ...], str], ...] = (
    (
        ("claude", "anthropic"),
        "善用 XML 标签分块；可承载多步推理与显式自检清单；负向约束（禁止做 X）遵循良好。"
        "避免深层嵌套与隐含暗示，规则要能逐条核对。",
    ),
    (
        ("gpt-4", "gpt4", "o1", "o3", "openai"),
        "可承载较长指令链；对结构化输出模板遵循稳定；给「可照抄的示例骨架」比抽象形容有效得多。"
        "避免在同一条里塞多个并列要求。",
    ),
    (
        ("deepseek", "qwen", "llama", "glm", "yi", "moonshot", "kimi", "senseno"),
        "指令要短、直、显式：避免深层嵌套、微妙暗示与长距离回指；把关键约束放在开头和结尾各重申一次。"
        "格式示例必须完整给出，不要指望模型推断。",
    ),
    (("gemini",), "对表格/JSON 等结构格式响应好；安全类措辞不要过度，否则容易触发无谓拒答。"),
)


def _model_profile(target_model: str) -> str:
    """把"适配目标模型"从一句口号变成按模型给出的具体档案。

    旧版只在 system 里写"GPT 可承载复杂指令链、Claude 善用 XML……"，模型并不会据此真的区分；
    现在按 target_model 关键字命中后，只把**这一条**注入 user 段，其余家族噪声不进上下文。
    """
    name = (target_model or "").strip().lower()
    if not name or name in ("未指定", "unknown"):
        return (
            "目标模型未指定：采用通用最佳实践——短句、显式格式模板、不过度嵌套、"
            "关键约束在开头与结尾各出现一次。"
        )
    for keys, profile in _MODEL_PROFILES:
        if any(k in name for k in keys):
            return f"目标模型 {target_model} 的已知倾向与对应写法：{profile}"
    return (
        f"目标模型 {target_model} 无已知档案：采用通用最佳实践——短句、显式格式模板、"
        "不过度嵌套，并对关键约束显式重申。"
    )
