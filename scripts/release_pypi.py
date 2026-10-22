"""产品发布：构建 sdist + wheel、机器校验内容、按需上传 PyPI（2026-10-23 新增）。

为什么单成脚本而不是 README 里一段命令：

1. **wheel 内容校验必须机器判**。2026-10-22 发布评审实测到真实事故形态：脏 `build/lib`
   把源码树**已删除**的 `pm/llm.py`、`pm/calibration.py` 重新打进 wheel（100 个条目里
   2 个陈旧），而 `unzip -l` 靠人眼看一眼防不住；同理，`case_templates` 曾经根本不进
   包（装包态面板模板白名单恒为空）。
2. **构建与上传必须分两步**。合成一条命令最容易发生"本来只想构建，结果手滑推上去"。
   `--check` 不联网、不上传，任何机器都能跑；`--upload` 只在 owner 机器、拿得到 token、
   且 HEAD 已打 tag 时才会走。

用法：

    python scripts/release_pypi.py --check      # 构建 + 校验（不联网、不上传）
    python scripts/release_pypi.py --upload     # 上传（需 PYPI_TOKEN 或 TWINE_PASSWORD）

退出码：0 通过 / 1 校验失败 / 2 用法或配置错误（与仓库其余入口同一协议）。
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PKG_VERSION_FILE = ROOT / "pm" / "__init__.py"
TEMPLATE_SRC = ROOT / "case_templates"


def _fail(msg: str) -> int:
    print(f"❌ {msg}", file=sys.stderr)
    return 1


def _version() -> str:
    """从唯一事实源读版本（不 import 包：import 会拉起 langgraph 等重依赖）。"""
    m = re.search(r'^__version__\s*=\s*"([^"]+)"', PKG_VERSION_FILE.read_text(encoding="utf-8"), re.M)
    if not m:
        raise SystemExit(_fail(f"{PKG_VERSION_FILE} 里找不到 __version__"))
    return m.group(1)


def _run(cmd: list[str], *, cwd: Path = ROOT) -> subprocess.CompletedProcess[str]:
    print("$ " + " ".join(cmd))
    return subprocess.run(cmd, cwd=str(cwd), text=True, encoding="utf-8", errors="replace")


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8"
    ).stdout.strip()


def _prepare_dist(dist: Path) -> int:
    if dist.exists():
        shutil.rmtree(dist)
    dist.mkdir(parents=True)
    return 0


def _build(dist: Path) -> int:
    for mod in ("build", "twine"):
        try:
            __import__(mod)
        except ImportError:
            return _fail(
                f"缺少发版工具 {mod}：pip install -e '.[release]'（build + twine 只装在发版机器上）"
            )
    if _prepare_dist(dist):
        return 1
    proc = _run([sys.executable, "-m", "build", "--sdist", "--wheel", "--outdir", str(dist)])
    if proc.returncode != 0:
        return _fail(f"python -m build 失败（rc={proc.returncode}）")
    proc = _run([sys.executable, "-m", "twine", "check", *map(str, sorted(dist.iterdir()))])
    if proc.returncode != 0:
        return _fail(f"twine check 失败（rc={proc.returncode}）")
    return 0


def _wheel_check(wheel: Path, version: str) -> list[str]:
    """wheel 内容校验。返回问题清单（空 = 通过）。每条都对应过一次真实事故。"""
    problems: list[str] = []
    with zipfile.ZipFile(wheel) as zf:
        names = set(zf.namelist())
        # ① 包本体必须在
        if "pm/__init__.py" not in names:
            problems.append("wheel 里没有 pm/__init__.py（包结构坏了）")
        # ② 模板必须在，且与磁盘逐名一致（装包态模板白名单曾恒为空）
        disk_templates = {p.name for p in TEMPLATE_SRC.glob("*.json") if p.is_file()}
        wheel_templates = {
            n.rsplit("/", 1)[-1] for n in names if re.search(r"\.data/data/case_templates/.+\.json$", n)
        }
        if not wheel_templates:
            problems.append(
                "wheel 里没有 case_templates/*.json（装包态模板白名单会恒为空；"
                "检查 pyproject 的 [tool.setuptools.data-files]）"
            )
        elif wheel_templates != disk_templates:
            problems.append(
                f"模板集合与磁盘不一致：缺 {sorted(disk_templates - wheel_templates)}，"
                f"多 {sorted(wheel_templates - disk_templates)}"
            )
        # ③ 元数据必须写对名字与版本
        meta = next((n for n in names if n.endswith(".dist-info/METADATA")), None)
        if not meta:
            problems.append("wheel 里没有 dist-info/METADATA")
        else:
            text = zf.read(meta).decode("utf-8", "replace")
            if "Name: prompt-master" not in text:
                problems.append("METADATA 里 Name 不是 prompt-master")
            if f"Version: {version}" not in text:
                problems.append(f"METADATA 版本与 pm/__init__.py（{version}）不一致")
            if "License-Expression: MIT" not in text and "License: MIT" not in text:
                problems.append("METADATA 里没有 MIT 许可证声明")
        # ④ 陈旧模块不许被复活（脏 build/lib 的真实形态）
        stale = sorted(
            n for n in names if re.match(r"^pm/.+\.py$", n) and not (ROOT / n).is_file()
        )
        if stale:
            problems.append(f"wheel 里有源码树已删除的模块：{stale}（构建前先删 build/）")
        # ⑤ 测试与产物不许进 wheel
        leaked = sorted(n for n in names if n.startswith(("tests/", "logs/")))
        if leaked:
            problems.append(f"wheel 里混进了不该有的目录：{leaked[:5]}")
    return problems


def _sdist_check(sdist: Path, version: str) -> list[str]:
    problems: list[str] = []
    with tarfile.open(sdist, "r:gz") as tf:
        names = {n.replace(f"prompt_master-{version}/", "") for n in tf.getnames()}
        for required in ("pyproject.toml", "README.md", "LICENSE", "pm/__init__.py"):
            if required not in names:
                problems.append(f"sdist 缺 {required}")
        disk_templates = {p.name for p in TEMPLATE_SRC.glob("*.json") if p.is_file()}
        sdist_templates = {
            n.rsplit("/", 1)[-1]
            for n in names
            if re.match(r"^case_templates/.+\.json$", n)
        }
        if sdist_templates != disk_templates:
            problems.append(
                f"sdist 模板与磁盘不一致：缺 {sorted(disk_templates - sdist_templates)}"
            )
    return problems


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def check(dist: Path) -> int:
    version = _version()
    print(f"产品版本（唯一事实源）：{version}")
    tagged = _git("tag", "--points-at", "HEAD")
    tag = f"v{version}"
    if tag in tagged.split():
        print(f"HEAD 已打 tag：{tag}")
    else:
        print(f"⚠️ HEAD 上没有 {tag} tag（--check 允许；--upload 会拒绝）")
    if _git("status", "--porcelain"):
        print("⚠️ 工作树不干净（--check 允许；--upload 会拒绝）")
    if _build(dist):
        return 1

    wheels = sorted(dist.glob("*.whl"))
    sdists = sorted(dist.glob("*.tar.gz"))
    problems: list[str] = []
    if len(wheels) != 1 or len(sdists) != 1:
        problems.append(f"dist/ 里应是 1 个 wheel + 1 个 sdist，实际 {len(wheels)}/{len(sdists)}")
    if not problems:
        problems += _wheel_check(wheels[0], version)
        problems += _sdist_check(sdists[0], version)

    if problems:
        for p in problems:
            print(f"❌ {p}", file=sys.stderr)
        return 1
    print("\n✅ 产物校验通过：")
    for p in (*sdists, *wheels):
        print(f"  {p.name}  {p.stat().st_size:,} bytes  sha256={_sha256(p)[:16]}…")
    return 0


def upload(dist: Path) -> int:
    if _git("status", "--porcelain"):
        return _fail("工作树不干净：先提交/还原，再上传（上传内容必须与 tag 逐字对应）")
    version = _version()
    tag = f"v{version}"
    if tag not in _git("tag", "--points-at", "HEAD").split():
        return _fail(f"HEAD 上没有 {tag} tag：先打 tag 再上传（PyPI 版本不可撤回）")
    if check(dist):
        return 1
    token = (os.getenv("PYPI_TOKEN") or os.getenv("TWINE_PASSWORD") or "").strip()
    if not token:
        return _fail("缺少 PYPI_TOKEN（或 TWINE_PASSWORD）：token 只放环境变量，别写进任何文件")
    env = {**os.environ, "TWINE_USERNAME": "__token__", "TWINE_PASSWORD": token}
    cmd = [
        sys.executable,
        "-m",
        "twine",
        "upload",
        "--non-interactive",
        *map(str, sorted(dist.iterdir())),
    ]
    print("$ " + " ".join(cmd[:4]) + " …（凭据来自环境变量，不打印）")
    proc = subprocess.run(cmd, cwd=str(ROOT), text=True, encoding="utf-8", errors="replace", env=env)
    if proc.returncode != 0:
        return _fail(f"twine upload 失败（rc={proc.returncode}）")
    print(f"\n✅ 已上传 {version}：https://pypi.org/project/prompt-master/{version}/")
    print(f"   打 tag 记得一起推：git push origin {tag}")
    return 0


def _bootstrap_stdio() -> None:
    """与其余入口同一套编码兜底（本机默认码页 cp936，不设就崩在打印 ⚠️/✅ 上）。"""
    sys.path.insert(0, str(ROOT))
    from pm.bootstrap import ensure_utf8_stdio

    ensure_utf8_stdio()


def main(argv: list[str] | None = None) -> int:
    _bootstrap_stdio()
    ap = argparse.ArgumentParser(description="PromptMaster 发版：构建 + 校验（+ 可选上传 PyPI）")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="构建 sdist+wheel 并校验（不上传）")
    mode.add_argument("--upload", action="store_true", help="校验后上传 PyPI（需 PYPI_TOKEN）")
    ap.add_argument("--dist-dir", default="dist", help="产物目录（默认 dist/）")
    args = ap.parse_args(argv)
    dist = (ROOT / args.dist_dir).resolve()
    return upload(dist) if args.upload else check(dist)


if __name__ == "__main__":
    sys.exit(main())
