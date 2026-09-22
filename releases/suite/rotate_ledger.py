"""JSONL 台账轮转 v2（修复 with_suffix 误用：直接用文件名字符串拼接）。"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
from pathlib import Path

p = Path(r"D:\projects\prompt-master\logs\modelhub_ledger.jsonl")
MAX = 1_000_000
KEEP = 3

def rotate(force=False):
    if not p.exists():
        print("no ledger file")
        return
    size = p.stat().st_size
    if not force and size < MAX:
        print(f"OK size={size}B < {MAX}B（无需轮转）")
        return
    base = str(p)  # ...\modelhub_ledger.jsonl
    gen3 = Path(base + ".3")
    gen2 = Path(base + ".2")
    gen1 = Path(base + ".1")
    if gen3.exists():
        gen3.unlink()
    if gen2.exists():
        gen2.replace(gen3)
    if gen1.exists():
        gen1.replace(gen2)
    p.replace(gen1)
    print(f"ROTATED: {size}B → .1（.3 淘汰）")

if __name__ == "__main__":
    rotate(force="--force" in sys.argv)
