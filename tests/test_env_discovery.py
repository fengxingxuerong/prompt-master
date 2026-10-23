"""`.env` 发现顺序（2026-10-22 修的真实用户缺陷）。

修复前的形状：`prepare_console()` 直接调 `load_dotenv()`，而 python-dotenv 的默认
`find_dotenv()` 从**调用方文件**所在目录向上找 —— 装包后调用方是 site-packages 里的
`pm/bootstrap.py`，于是用户在自己项目里放的 `.env` 永远读不到，表现是"明明配了 Key
却报没 Key"。检出模式下反过来：从 /tmp 跑仓库的 run.py 也会读到仓库那份 `.env`。

现在的契约：
1. 当前工作目录向上找是主来源（用户项目优先）；
2. 检出模式下再退回仓库根那份（开发方便）；
3. 进程环境里已有的变量不被 `.env` 覆盖（CI 显式注入优先）。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from pm.bootstrap import load_env_files

ROOT = Path(__file__).resolve().parents[1]


def _write_env(path: Path, **values: str) -> None:
    path.write_text(
        "".join(f"{k}={v}\n" for k, v in values.items()),
        encoding="utf-8",
    )


def test_cwd_env_wins_and_checkout_env_is_a_fallback(tmp_path, monkeypatch) -> None:
    """两个来源都在时：cwd 的值生效，检出根的值只补缺。"""
    key = "PM_TEST_CWD_WINS_20261022"
    other = "PM_TEST_FALLBACK_ONLY_20261022"
    cwd_dir = tmp_path / "project"
    repo_dir = tmp_path / "checkout"
    cwd_dir.mkdir()
    repo_dir.mkdir()
    _write_env(cwd_dir / ".env", **{key: "from-cwd"})
    _write_env(repo_dir / ".env", **{key: "from-repo", other: "only-in-repo"})

    monkeypatch.chdir(cwd_dir)
    monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv(other, raising=False)
    try:
        loaded = load_env_files(repo_root=repo_dir)
        assert os.environ[key] == "from-cwd", "cwd 的 .env 必须优先于检出根"
        assert os.environ[other] == "only-in-repo", "检出根只补 cwd 没有的键"
        assert len(loaded) == 2, f"两个文件都该被记入加载清单：{loaded}"
    finally:
        os.environ.pop(key, None)
        os.environ.pop(other, None)


def test_existing_environment_is_never_overridden(tmp_path, monkeypatch) -> None:
    """CI/Agent 显式注入的变量优先于任何 .env。"""
    key = "PM_TEST_ENV_PRECEDENCE_20261022"
    cwd_dir = tmp_path / "project"
    repo_dir = tmp_path / "checkout"
    cwd_dir.mkdir()
    repo_dir.mkdir()
    _write_env(cwd_dir / ".env", **{key: "from-file"})

    monkeypatch.chdir(cwd_dir)
    monkeypatch.setenv(key, "from-process")
    loaded = load_env_files(repo_root=repo_dir)
    assert os.environ[key] == "from-process"
    assert loaded == [str(cwd_dir / ".env")], "没有检出根 .env 时只该加载 cwd 那份"


def test_gbk_dotenv_fallback_in_process(tmp_path, monkeypatch, capsys) -> None:
    """进程内也走一遍 GBK `.env` 回退：subprocess 用例不计入覆盖率。"""
    key = "PM_TEST_GBK_DOTENV_INPROC_20261023"
    cwd_dir = tmp_path / "cwd"
    cwd_dir.mkdir()
    (cwd_dir / ".env").write_bytes(("# 中文注释\n" + f"{key}=inproc\n").encode("gbk"))
    monkeypatch.chdir(cwd_dir)
    monkeypatch.delenv(key, raising=False)
    try:
        loaded = load_env_files(repo_root=tmp_path / "no-checkout")
        assert os.environ[key] == "inproc"
        assert loaded and loaded[0].endswith(".env")
        assert "gb18030" in capsys.readouterr().err
    finally:
        os.environ.pop(key, None)


def test_undecodable_dotenv_is_skipped_with_a_warning(tmp_path, monkeypatch, capsys) -> None:
    """二进制/坏编码的 .env：告警并跳过，不能让所有命令启动即崩。"""
    cwd_dir = tmp_path / "broken"
    cwd_dir.mkdir()
    (cwd_dir / ".env").write_bytes(b"\x00\xff\xfe\x81")
    monkeypatch.chdir(cwd_dir)
    loaded = load_env_files(repo_root=tmp_path / "no-checkout")
    err = capsys.readouterr().err
    assert "无法解码" in err
    assert loaded, "文件路径仍应记入加载清单（好让调用方知道它被看见过）"


def test_installed_style_entry_reads_the_users_cwd_dotenv(tmp_path) -> None:
    """装包态等价场景：入口是用户目录里的脚本，用户的 .env 必须被读到。

    复现要点是**调用方文件不在仓库里**（这里是一份临时 probe.py，PYTHONPATH 指向仓库），
    这正是 console script 的形状。修复前本用例读到的是空串。
    """
    marker = "PM_TEST_INSTALLED_CWD_20261022"
    cwd_dir = tmp_path / "user-project"
    cwd_dir.mkdir()
    _write_env(cwd_dir / ".env", **{marker: "loaded-from-user-project"})
    probe = cwd_dir / "probe.py"
    probe.write_text(
        "import os\n"
        "from pm.bootstrap import prepare_console\n"
        "prepare_console()\n"
        f"print(os.environ.get({marker!r}) or '')\n",
        encoding="utf-8",
    )
    env = {**os.environ, "PYTHONPATH": str(ROOT), "PYTHONIOENCODING": "utf-8"}
    env.pop(marker, None)
    proc = subprocess.run(
        [sys.executable, str(probe)],
        cwd=str(cwd_dir),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().endswith("loaded-from-user-project"), (
        "用户目录里的 .env 没被读到；装包态会表现为「配了 Key 却说没 Key」\n"
        f"stdout={proc.stdout!r}\nstderr={proc.stderr[-500:]!r}"
    )


def test_gbk_dotenv_is_decoded_instead_of_crashing(tmp_path) -> None:
    """老家用记事本存的 GBK `.env`（含中文注释）不该让所有命令启动即崩。"""
    marker = "PM_TEST_GBK_DOTENV_20261022"
    cwd_dir = tmp_path / "legacy-project"
    cwd_dir.mkdir()
    (cwd_dir / ".env").write_bytes(
        ("# 这是中文注释\n" + f"{marker}=loaded-from-gbk-env\n").encode("gbk")
    )
    probe = cwd_dir / "probe.py"
    probe.write_text(
        "import os\n"
        "from pm.bootstrap import prepare_console\n"
        "prepare_console()\n"
        f"print(os.environ.get({marker!r}) or '')\n",
        encoding="utf-8",
    )
    env = {**os.environ, "PYTHONPATH": str(ROOT), "PYTHONIOENCODING": "utf-8"}
    env.pop(marker, None)
    proc = subprocess.run(
        [sys.executable, str(probe)],
        cwd=str(cwd_dir),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().endswith("loaded-from-gbk-env")
    assert "gb18030" in proc.stderr, "回退解码要在 stderr 明说，不能悄悄换编码"


def test_zero_call_entry_survives_without_python_dotenv(monkeypatch) -> None:
    """零调用子命令不该因为没装 python-dotenv 就启动失败。"""
    monkeypatch.setitem(sys.modules, "dotenv", None)
    assert load_env_files() == []
