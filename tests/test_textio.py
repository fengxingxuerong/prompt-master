"""用户文件编码兜底：UTF-8 → GB18030（2026-10-22）。

中文 Windows 用户的提示词常在记事本里存成 GBK。修复前 `check` / `diff` /
`--task-file` / `--cases-file` 遇到这种文件直接抛 `UnicodeDecodeError` 裸栈，
退出码还是 1（与"未达标但已交付"撞车）。这里同时钉住**读侧**与真实入口。
"""

from __future__ import annotations

import codecs
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from pm.textio import TextDecodeError, read_text_auto, warn_if_fallback

ROOT = Path(__file__).resolve().parents[1]
RUN = [sys.executable, str(ROOT / "run.py")]

TEXT = "你是销售分析师。请分析数据并输出结论，不要编造缺失值。"
CASES = [{"input": "本月销售额 120", "expected": "引用 120"}]


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*RUN, *args],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )


def test_plain_utf8_and_bom(tmp_path) -> None:
    plain = tmp_path / "plain.md"
    plain.write_bytes(TEXT.encode("utf-8"))
    got = read_text_auto(plain)
    assert got.text == TEXT and got.encoding == "utf-8" and not got.used_fallback

    bom = tmp_path / "bom.md"
    bom.write_bytes(codecs.BOM_UTF8 + TEXT.encode("utf-8"))
    got_bom = read_text_auto(bom)
    assert got_bom.text == TEXT and got_bom.encoding == "utf-8-sig"
    assert not got_bom.used_fallback


def test_every_encoding_path_normalizes_newlines(tmp_path) -> None:
    """CRLF 必须在**每一条**解码路径上归一化成 LF（含带 BOM 的那条）。

    实测缺陷（2026-10-31 审计）：BOM 分支直接 `raw.decode(...)`，只有无 BOM 分支做了
    `replace("\\r\\n", "\\n")`。于是同一份内容存成 UTF-8 无 BOM 是 LF、
    存成 UTF-8-BOM / UTF-16 是 CRLF ⇒ `run.py diff` 把整篇报成"每一行都改了"，
    送进模型的正文字里还带着 \\r。
    """
    body = "第一行\r\n第二行\r\n第三行\r\n"
    want = "第一行\n第二行\n第三行\n"

    plain = tmp_path / "plain.md"
    plain.write_bytes(body.encode("utf-8"))
    assert read_text_auto(plain).text == want

    bom = tmp_path / "bom.md"
    bom.write_bytes(codecs.BOM_UTF8 + body.encode("utf-8"))
    assert read_text_auto(bom).text == want, "UTF-8-BOM 分支没归一化换行"

    utf16 = tmp_path / "utf16.md"
    utf16.write_bytes(codecs.BOM_UTF16_LE + body.encode("utf-16-le"))
    assert read_text_auto(utf16).text == want, "UTF-16 分支没归一化换行"

    lone_cr = tmp_path / "cr.md"
    lone_cr.write_bytes("第一行\r第二行\r".encode())
    assert read_text_auto(lone_cr).text == "第一行\n第二行\n"


def test_gbk_falls_back_and_is_flagged(tmp_path, capsys) -> None:
    gbk = tmp_path / "gbk.md"
    gbk.write_bytes(TEXT.encode("gbk"))
    got = read_text_auto(gbk)
    assert got.text == TEXT and got.encoding == "gb18030" and got.used_fallback
    warn_if_fallback(got, "--prompt-file")
    err = capsys.readouterr().err
    assert "不是 UTF-8" in err and "gb18030" in err


def test_utf16_with_bom_is_decoded(tmp_path) -> None:
    f = tmp_path / "utf16.md"
    f.write_bytes(TEXT.encode("utf-16"))
    got = read_text_auto(f)
    assert got.text == TEXT and got.encoding == "utf-16"
    assert got.used_fallback, "非 UTF-8 都要提示，哪怕它是有 BOM 的 Unicode"


def test_binary_and_empty_files(tmp_path) -> None:
    empty = tmp_path / "empty.md"
    empty.write_bytes(b"")
    assert read_text_auto(empty).text == ""

    binary = tmp_path / "blob.bin"
    binary.write_bytes(b"\x00\x01\x02\xff")
    with pytest.raises(TextDecodeError, match="二进制"):
        read_text_auto(binary)


def test_missing_file_still_raises_oserror(tmp_path) -> None:
    with pytest.raises(OSError):
        read_text_auto(tmp_path / "nope.md")


# --------------------------------------------------------------------------
# 真实入口：用户不会调用 read_text_auto，他们敲的是 run.py
# --------------------------------------------------------------------------
def test_check_reads_a_gbk_prompt(tmp_path) -> None:
    f = tmp_path / "我的提示词.md"
    f.write_bytes(TEXT.encode("gbk"))
    proc = _run("check", "--prompt-file", str(f))
    assert proc.returncode == 1, proc.stderr
    assert "Traceback" not in proc.stderr
    assert "已按 gb18030 解码" in proc.stderr
    assert "提示词体检" in proc.stdout


def test_gbk_check_keeps_json_stdout_pure(tmp_path) -> None:
    """回退提示必须走 stderr：Agent 的 `--json` 契约是 stdout 恰好一个对象。"""
    f = tmp_path / "提示词.md"
    f.write_bytes(TEXT.encode("gbk"))
    proc = _run("check", "--prompt-file", str(f), "--json")
    payload = json.loads(proc.stdout)
    assert payload["mode"] == "prompt_check"
    assert "gb18030" in proc.stderr


def test_task_file_gbk_and_missing_file_are_actionable(tmp_path) -> None:
    f = tmp_path / "需求.txt"
    f.write_bytes("让 AI 分析销售数据".encode("gbk"))
    ok = _run("--task-file", str(f), "--dry-run")
    assert ok.returncode == 0, ok.stderr
    assert "gb18030" in ok.stderr
    assert json.loads(ok.stdout)["mode"] == "dry_run"

    missing = _run("--task-file", str(tmp_path / "nope.txt"), "--dry-run")
    assert missing.returncode == 2, "缺失文件是参数错误，不该裸抛 FileNotFoundError"
    assert "读取失败" in missing.stderr and "Traceback" not in missing.stderr

    as_dir = _run("--task-file", str(tmp_path), "--dry-run")
    assert as_dir.returncode == 2
    assert "不是目录" in as_dir.stderr and "Traceback" not in as_dir.stderr


def test_cases_file_gbk_is_read(tmp_path) -> None:
    f = tmp_path / "用例.json"
    f.write_bytes(json.dumps(CASES, ensure_ascii=False).encode("gbk"))
    proc = _run("check", "--cases-file", str(f))
    assert proc.returncode in (0, 1), proc.stderr
    assert "gb18030" in proc.stderr and "Traceback" not in proc.stderr


def test_diff_reads_gbk_sources(tmp_path) -> None:
    f = tmp_path / "提示词.md"
    f.write_bytes(TEXT.encode("gbk"))
    proc = _run("diff", str(f), str(f))
    assert proc.returncode == 0, proc.stderr
    assert "完全一致" in proc.stdout
    assert "gb18030" in proc.stderr and "Traceback" not in proc.stderr
