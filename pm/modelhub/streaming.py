"""上游 SSE 流式转发（OPT-1）。

对上游发起 stream=true 请求，逐行解析 SSE，透传 OpenAI 格式的
data: {...} 块给客户端；yield 生成器形式，供 FastAPI StreamingResponse 使用。

切换语义：连接建立且首个 data 块到达前失败 → 视为该模型失败，走正常切换链
（客户端尚未收到任何字节，无感）；首个 data 块之后断流 → 客户端已收到部分
内容，向流内注入一条 error 事件并结束（不静默）。
"""

from __future__ import annotations

import json
import urllib.request
from collections.abc import Iterator
from typing import Any


def stream_upstream(
    base_url: str,
    api_key: str,
    payload: dict[str, Any],
    timeout: float,
) -> tuple[str, Iterator[bytes]]:
    """建立上游流式连接。返回 (upstream_model, chunk 迭代器)。

    连接阶段失败抛 GatewayError 语义异常（复用 pool 的分类）。
    """
    from .pool import GatewayError

    url = base_url.rstrip("/") + "/chat/completions"
    body = dict(payload)
    body["stream"] = True
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": "***" + api_key,
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        },
    )
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", errors="replace")[:300]
        except Exception:  # noqa: BLE001
            pass
        raise GatewayError(f"HTTP {e.code}: {detail}", http_status=e.code) from e
    except urllib.error.URLError as e:
        reason = str(getattr(e, "reason", "") or e)
        if "timed out" in reason.lower() or "timeout" in reason.lower():
            raise GatewayError(f"读超时（>{int(timeout)}s）") from e
        raise GatewayError(f"连接失败: {reason}") from e
    except OSError as e:
        raise GatewayError(f"网络错误: {e}") from e

    # 上游返回的 model（可能与请求的 upstream_model 一致）
    ctype = resp.headers.get("Content-Type", "")
    if "text/event-stream" not in ctype and "application/octet-stream" not in ctype:
        # 上游没按流式返回（如直接 JSON）——读完后包一层合成流
        try:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
            model = data.get("model") or payload.get("model") or "?"
            content = ""
            choices = data.get("choices") or []
            if choices:
                content = (choices[0].get("message") or {}).get("content") or ""
            chunk = {
                "id": data.get("id") or "chatcmpl-streamed",
                "object": "chat.completion.chunk",
                "model": model,
                "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": "stop"}],
            }
            model_out = str(model)

            def synth() -> Iterator[bytes]:
                yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8")
                yield b"data: [DONE]\n\n"

            return model_out, synth()
        except json.JSONDecodeError as e:
            raise GatewayError("响应不是 JSON（非流式回退失败）") from e

    def gen() -> Iterator[bytes]:
        first_seen = False
        try:
            for raw in resp:
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                if not line:
                    continue
                if first_seen and not line.startswith("data:") and not line.startswith(":"):
                    continue
                if line.startswith(":"):
                    continue  # SSE 注释心跳
                if line == "data: [DONE]" or line == "data:[DONE]":
                    first_seen = True
                    yield b"data: [DONE]\n\n"
                    return
                if line.startswith("data:"):
                    first_seen = True
                    yield (line + "\n\n").encode("utf-8")
                # 非法行忽略
            # 上游没发 [DONE] 就断流：补发 DONE（正文已透传，客户端能正常收尾）
            if first_seen:
                yield b"data: [DONE]\n\n"
        finally:
            try:
                resp.close()
            except Exception:  # noqa: BLE001
                pass

    # 从首个 chunk 探测 model 名（失败不影响流）
    model_guess = payload.get("model") or "?"
    return model_guess, gen()


def sse_first_content(chunks: Iterator[bytes], max_chars: int = 200000) -> tuple[str, str, bool]:
    """调试/测试辅助：消费流，返回 (完整正文, model, done)。"""
    content_parts: list[str] = []
    model = "?"
    done = False
    total = 0
    for raw in chunks:
        if raw == b"data: [DONE]\n\n":
            done = True
            continue
        try:
            line = raw.decode("utf-8").strip()
            if not line.startswith("data:"):
                continue
            obj = json.loads(line[5:].strip())
            model = obj.get("model") or model
            for ch in obj.get("choices") or []:
                delta = ch.get("delta") or {}
                piece = delta.get("content") or ""
                if piece:
                    content_parts.append(piece)
                    total += len(piece)
                    if total >= max_chars:
                        done = False
                        return "".join(content_parts), model, done
        except json.JSONDecodeError:
            continue
    return "".join(content_parts), model, done
