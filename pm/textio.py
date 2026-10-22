"""用户文本文件解码：UTF-8 优先，中文 Windows 的 GB18030 兜底（2026-10-22）。

为什么需要：产品的中文用户大量把提示词/用例存成 GBK（记事本默认、老编辑器、
Excel 导出）。此前 `check` / `diff` / `--task-file` / `--cases-file` 都直接
`read_text(encoding="utf-8")`，遇到 GBK 文件抛 `UnicodeDecodeError` 裸栈、
退出码还是 1 —— 用户看到的是"程序坏了"，不知道该把文件另存为 UTF-8。

契约：
- 有 BOM 的 UTF-8/16/32 按 BOM 解码；
- 无 BOM 先试 UTF-8（含 BOM 容忍），失败再试 GB18030（GBK 的超集）；
- 含 NUL 且无 BOM 的按二进制拒绝，避免把图片/可执行文件解成乱码；
- 回退到 GB18030 时由调用方打印一次 stderr 提示（stdout 的 JSON 契约不受影响）。

这是**读**侧的兜底，不改变写侧：产物仍一律 UTF-8。
"""

from __future__ import annotations

import codecs
import sys
from dataclasses import dataclass
from pathlib import Path


class TextDecodeError(ValueError):
    """文件既不是可识别的 Unicode，也不是 GB18030 —— 给用户一句能照做的话。"""


@dataclass(frozen=True)
class DecodedText:
    text: str
    encoding: str
    path: str

    @property
    def used_fallback(self) -> bool:
        """是否走了非 UTF-8 的回退（调用方据此提示一次）。"""
        return self.encoding not in ("utf-8", "utf-8-sig")


# 顺序有讲究：UTF-32 LE 的 BOM 以 UTF-16 LE 的 BOM 开头，必须先判 32。
_BOMS: tuple[tuple[bytes, str], ...] = (
    (codecs.BOM_UTF32_LE, "utf-32"),
    (codecs.BOM_UTF32_BE, "utf-32"),
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF16_LE, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16"),
)


def read_text_auto(path: str | Path) -> DecodedText:
    """读取用户文本文件，按 UTF-8 → GB18030 的顺序解码。

    空文件返回空串（大小写/空正文由调用方按业务报错，不在这里替它决定）。
    `OSError` 原样抛给调用方：文件不存在/权限不足与"编码不对"是两类错误。
    """
    p = Path(path)
    raw = p.read_bytes()
    if not raw:
        return DecodedText("", "utf-8", str(p))

    for bom, encoding in _BOMS:
        if raw.startswith(bom):
            try:
                return DecodedText(raw.decode(encoding), encoding, str(p))
            except UnicodeDecodeError as e:  # pragma: no cover - BOM 正确时几乎不可达
                raise TextDecodeError(f"{p} 的 {encoding} BOM 无法解码：{e}") from e

    if b"\x00" in raw:
        raise TextDecodeError(
            f"{p} 看起来是二进制文件，或没有 BOM 的 UTF-16/32；请另存为 UTF-8 或 GB18030 文本"
        )

    for encoding in ("utf-8", "gb18030"):
        try:
            text = raw.decode(encoding)
            # 与 Path.read_text 的 universal newlines 同口径：CRLF/CR 统一成 LF。
            # 不归一化会让"同一份文件"在 diff 两侧因换行风格不同而全行重写。
            return DecodedText(text.replace("\r\n", "\n").replace("\r", "\n"), encoding, str(p))
        except UnicodeDecodeError:
            continue
    raise TextDecodeError(f"{p} 既不是 UTF-8 也不是 GB18030 编码；请另存为 UTF-8 后重试")


def warn_if_fallback(decoded: DecodedText, label: str = "文件") -> None:
    """回退解码时在 stderr 说一句（stdout 留给 JSON/结果）。"""
    if decoded.used_fallback:
        print(
            f"提示：{label} 不是 UTF-8，已按 {decoded.encoding} 解码：{decoded.path}",
            file=sys.stderr,
        )


__all__ = ["DecodedText", "TextDecodeError", "read_text_auto", "warn_if_fallback"]
