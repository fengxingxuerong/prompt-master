"""独立 UTF-8 stdio 引导（与 pm.bootstrap 同义，保持包零依赖）。"""
import sys

def ensure_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

