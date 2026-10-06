"""变异核验（临时探针，跑完可删）：逐处拆掉实现，确认新断言真的会红。

纪律：
- 锚点不唯一的变异直接判"未施加"，不算通过；
- 还原用**内存里的原始字节**写回（不用 cp 备份、不用 git checkout——后者会把本轮
  整个特性一起删掉），跑完立刻 sha1 比对；
- 全程 bytes，避开 Windows 文本模式的 CRLF 转换（上一版就是这么把源文件改脏的）。
"""

import hashlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "pm" / "calibration.py"
TESTS = "tests/test_discrimination.py"

MUTATIONS: dict[str, tuple[bytes, bytes]] = {
    "M1 AUC 恒 0.5（判别力失明）": (
        b"    return (wins + 0.5 * ties) / (len(pos) * len(neg))",
        b"    return 0.5",
    ),
    "M2 阈值可逃到可表达区间外（=允许'全部判不达标'）": (
        b"    return [round(lo + i * CUT_SCAN_STEP, 4) for i in range(steps + 1)]",
        b"    return [round(lo + i * CUT_SCAN_STEP, 4) for i in range(steps + 400)]",
    ),
    "M3 留一折退化成同批拟合上界": (
        b"    if n < 3:\n        return None\n    hit = 0",
        b"    if n < 3:\n        return None\n    return _best_cut(scores, labels)[1]\n    hit = 0",
    ),
    "M4 可估性门槛形同虚设（>=1）": (
        b"MIN_ESTIMABLE_CELL = 10",
        b"MIN_ESTIMABLE_CELL = 1",
    ),
    "M5 聚合侧漏掉按锚点聚类口径": (
        b"            humans, judges, PASS_THRESHOLD, group_keys=[str(p.get(\"id\")) for p in pairs]",
        b"            humans, judges, PASS_THRESHOLD",
    ),
    "M6 analyze 不再挂判别力（入口断线）": (
        b'        "discrimination": discrimination_stats(humans, judges, PASS_THRESHOLD),\n',
        b"",
    ),
}


def run_suite() -> tuple[int, str]:
    r = subprocess.run(
        [sys.executable, "-m", "pytest", TESTS, "-q", "--no-header"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    out = (r.stdout or "") + (r.stderr or "")
    lines = [ln for ln in out.splitlines() if ("passed" in ln or "failed" in ln or "error" in ln)]
    return r.returncode, lines[-1] if lines else out[-400:]


def main() -> int:
    original = TARGET.read_bytes()
    h0 = hashlib.sha1(original).hexdigest()
    rc, summary = run_suite()
    print(f"基线（未变异）：rc={rc}  {summary}")
    if rc != 0:
        print("基线不绿，变异测试无意义，先修基线")
        return 1
    missed: list[str] = []
    try:
        for name, (needle, repl) in MUTATIONS.items():
            if original.count(needle) != 1:
                print(f"{name}: 锚点命中 {original.count(needle)} 次 -> **变异未施加**，判为无效")
                missed.append(name + "（锚点不唯一）")
                continue
            TARGET.write_bytes(original.replace(needle, repl, 1))
            rc, summary = run_suite()
            print(f"{name}: rc={rc}  {summary}")
            if rc == 0:
                missed.append(name)
    finally:
        TARGET.write_bytes(original)
    same = hashlib.sha1(TARGET.read_bytes()).hexdigest() == h0
    print(f"还原校验：sha1 {'一致 ✓' if same else '**不一致，手工检查！**'}")
    print(f"未被抓住的变异：{missed or '无 ✓'}")
    return 0 if same and not missed else 1


if __name__ == "__main__":
    raise SystemExit(main())
