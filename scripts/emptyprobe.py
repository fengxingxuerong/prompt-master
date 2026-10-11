"""一次真实探针，区分三种空法（判据写在下面，跑完看数）。

代码已经读清了机制（不是猜）：
  `_warn_if_token_budget_exhausted`（pm/llm.py:900）**只在异常路径触发**——
  入口是 `_is_length_finish_error(e)`。而 `plain_call` 是
  `text = resp.content`，**成功返回空 content 根本走不到那个诊断**。

  ⇒ run_41a833fb9b41 那两次调用：HTTP 200、token 照记、content 空
    ⇒ 诊断一次都不触发（与日志吻合），根因对 §三十·十六·三 是同一个。

现在要验的是"空"到底是哪一种：
  A) content 真的是空串           → 模型全烧在 reasoning
  B) content 有内容，被 _strip_code_fence 剥光
  C) content 有内容，质量门判死

只花 1 次调用（≈100s）。读数：content 长度 + completion/reasoning token。
注意：探针只回答这一件事，不外推到别处。
"""

import os
import sys

os.environ.setdefault("PM_BASE_URL", os.environ.get("PM_BASE_URL", ""))
sys.path.insert(0, ".")

from pm import backend  # noqa: E402
from pm.llm import build_config, plain_call  # noqa: E402

ROLE = "optimizer"
cfg = build_config(ROLE)
print(f"端点 : {getattr(cfg, 'base_url', '?')}")
print(f"模型 : {cfg.model}")
print(f"max_tokens : {getattr(cfg, 'max_tokens', '?')}")
print(f"温度 : {getattr(cfg, 'temperature', '?')}")

seen: dict[str, object] = {}
real_invoke = None


def spy(role, system, user, overrides=None):
    """包一层，把真实响应抓下来看 content 到底有多长。"""
    from pm import llm as _llm

    cfg2 = _llm.build_config(role, overrides)
    llm = _llm.get_llm(role, overrides)
    from langchain_core.messages import HumanMessage, SystemMessage

    resp = llm.invoke([SystemMessage(content=system), HumanMessage(content=user)])
    content = resp.content
    txt = content if isinstance(content, str) else str(content)
    seen["content_len"] = len(txt)
    seen["content_head"] = txt[:300]
    um = getattr(resp, "usage_metadata", None) or {}
    seen["usage"] = um
    details = getattr(resp, "usage_metadata", None)
    print(f"\n--- 真实响应 ---")
    print(f"content 类型 : {type(content).__name__}")
    print(f"content 长度 : {len(txt)}")
    print(f"usage_metadata : {um}")
    print(f"completion_tokens_details : "
          f"{getattr(resp, 'response_metadata', None)}")
    print(f"content 前 300 字 :\n{txt[:300]!r}")
    _llm._record_usage(role, int(um.get("input_tokens", 0) or 0),
                       int(um.get("output_tokens", 0) or 0), 0)
    return txt, {"role": role, "model": cfg2.model, "channel": "plain"}


from pm.nodes.optimize import _strip_code_fence  # noqa: E402

SYSTEM = "你是提示词工程专家。请写出一个用于数据分析的提示词。"
USER = "需求：分析销售数据并给出趋势结论与异常点。"

print("\n=== 探针开始（1 次真实调用）===")
with backend.use(backend.CallHook(plain=spy)):
    text, meta = plain_call(ROLE, SYSTEM, USER)

stripped = _strip_code_fence(text)
print(f"\n=== 结果 ===")
print(f"原始 content 长度 : {seen.get('content_len')}")
print(f"_strip_code_fence 后 : {len(stripped)}")
if stripped.strip():
    print(f"剥光后前 200 字 : {stripped[:200]!r}")
    print("形态 B/C：content 有内容，剥完还有货")
else:
    print("形态 A：剥完是空 —— content 本身就没正文")

from pm.quality import check_prompt_quality  # noqa: E402

r = check_prompt_quality(stripped)
print(f"\n质量门 ok={r.ok}  {r.describe()}")