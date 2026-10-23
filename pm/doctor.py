"""`run.py doctor`：零 LLM 调用的环境诊断（2026-10-23）。

用户配错 Key / 端点 / 输出目录时，现有反馈要么要等到真跑才出现，要么散落在
`--preflight`（会花钱）与报错栈里。doctor 把"现在这套配置到底能不能用"提前成一张
检查单：Python、`.env` 发现、Key 覆盖、角色配置、日志目录、端口、常驻服务、MCP 依赖。

边界：
- **零 LLM 调用、零出网**；只做本机文件/环境变量/端口与 localhost 健康检查。
- 不写任何文件（日志目录只检查可写性，不创建）。
- 不回显完整 Key：只给尾号与占位符标记。
- 退出码：0=无问题；1=只有警告；2=有错误（配置层面跑不起来）。
"""

from __future__ import annotations

import importlib.util
import os
import socket
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import __version__
from .bootstrap import load_env_files
from .keycheck import KEY_ROLES, is_placeholder_key, key_status, mask_key
from .paths import log_dir

_ICON = {"ok": "✓", "warn": "⚠", "error": "✗", "info": "ℹ"}


@dataclass
class Check:
    name: str
    status: str  # ok / warn / error / info
    detail: str
    hint: str = ""

    def as_dict(self) -> dict[str, str]:
        return {"name": self.name, "status": self.status, "detail": self.detail, "hint": self.hint}


@dataclass
class DoctorReport:
    checks: list[Check] = field(default_factory=list)
    facts: dict[str, Any] = field(default_factory=dict)

    @property
    def errors(self) -> list[Check]:
        return [c for c in self.checks if c.status == "error"]

    @property
    def warnings(self) -> list[Check]:
        return [c for c in self.checks if c.status == "warn"]

    @property
    def exit_code(self) -> int:
        if self.errors:
            return 2
        return 1 if self.warnings else 0

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": "doctor",
            "ok": self.ok,
            "exit_code": self.exit_code,
            "version": __version__,
            "checks": [c.as_dict() for c in self.checks],
            "warnings": len(self.warnings),
            "errors": len(self.errors),
            **self.facts,
        }


def _port_state(host: str, port: int) -> str:
    """返回 "free" / "in_use"；只 bind 一瞬间，不监听、不发请求。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((host, port))
    except OSError:
        return "in_use"
    finally:
        sock.close()
    return "free"


def _is_loopback(url: str) -> bool:
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    return host in ("127.0.0.1", "localhost", "::1", "0.0.0.0")


def _server_state(url: str, timeout: float = 0.5) -> dict[str, Any]:
    if not _is_loopback(url):
        return {"running": False, "detail": "跳过：PM_SERVER_URL 不是本机地址（doctor 不出网）"}
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/api/health", timeout=timeout) as resp:
            body = resp.read(200).decode("utf-8", errors="replace")
            return {"running": resp.status == 200, "detail": body[:120]}
    except Exception as e:  # noqa: BLE001 - 诊断只报状态，不能因为连不上而崩
        return {"running": False, "detail": f"{type(e).__name__}: {e}"[:160]}


def _logs_fact() -> dict[str, Any]:
    d = log_dir()
    files = sorted(d.glob("run_*.json")) if d.is_dir() else []
    total = 0
    for f in files:
        try:
            total += f.stat().st_size
        except OSError:
            continue
    target = d if d.is_dir() else d.parent
    writable = target.exists() and os.access(target, os.W_OK)
    return {
        "path": str(d),
        "run_files": len(files),
        "bytes": total,
        "writable": bool(writable),
    }


def _roles_fact(pool_count: int = 0) -> tuple[list[dict[str, Any]], str | None]:
    """逐角色构造配置；返回 (角色表, 依赖错误)。"""
    try:
        from .llm.config import build_config
    except Exception as e:  # noqa: BLE001 - 缺依赖时给一句能照做的话
        return [], f"{type(e).__name__}: {e}"
    roles: list[dict[str, Any]] = []
    for role in KEY_ROLES:
        try:
            cfg = build_config(role)
        except Exception as e:  # noqa: BLE001
            roles.append(
                {
                    "role": role,
                    "error": f"{type(e).__name__}: {e}",
                    "model": "",
                    "base_url": "",
                    "provider": "",
                    "key_tail": "(无)",
                    "placeholder": False,
                }
            )
            continue
        if cfg.api_key:
            key_tail = mask_key(cfg.api_key)
        elif pool_count:
            key_tail = f"(多 Key 池 {pool_count} 把)"
        else:
            key_tail = "(无)"
        roles.append(
            {
                "role": role,
                "provider": cfg.provider,
                "model": cfg.model,
                "base_url": cfg.base_url or "(默认端点)",
                "key_tail": key_tail,
                "placeholder": is_placeholder_key(cfg.api_key),
            }
        )
    return roles, None


def build_report() -> DoctorReport:
    """跑全部检查，返回可渲染/可 JSON 的报告（不打印、不退出）。

    CC 13 已登记进复杂度台账（`tests/complexity_budget.json`）：这里是 8 段**线性**
    检查的清单，段与段之间没有嵌套、也不共享控制流；拆成 8 个 `_check_*` 只会把
    「一次跑完、按固定顺序追加」这个语义打散，可读性收益小于跨函数传 `checks` 与
    facts 的代价。若将来某一段自己长出嵌套，应该单独拆那一段而不是整体。
    """
    report = DoctorReport()
    checks = report.checks

    py_ok = sys.version_info >= (3, 11)
    checks.append(
        Check(
            "Python",
            "ok" if py_ok else "error",
            f"{sys.version.split()[0]}（需要 ≥3.11）",
            "" if py_ok else "请升级 Python 到 3.11+ 后重装依赖",
        )
    )
    checks.append(Check("版本", "ok", __version__))
    checks.append(Check("工作目录", "ok", str(Path.cwd())))

    env_files: list[str] = []
    try:
        env_files = [str(p) for p in load_env_files()]
    except Exception as e:  # noqa: BLE001
        checks.append(
            Check("配置文件", "warn", f".env 读取失败：{e}", "检查文件权限或另存为 UTF-8")
        )
    if env_files:
        checks.append(Check("配置文件", "ok", "已加载：" + "、".join(env_files)))
    else:
        checks.append(
            Check(
                "配置文件",
                "info",
                "未找到 .env（将只使用进程环境变量）",
                "可 cp .env.example .env；或直接 export PM_API_KEY=...",
            )
        )

    fake = (os.getenv("PM_FAKE_BACKEND") or "").strip()
    keys = key_status()
    placeholders = list(keys["placeholders"])
    missing = list(keys["missing_roles"])
    if fake:
        checks.append(Check("Key", "ok", f"演示模式（PM_FAKE_BACKEND={fake}），不需要 Key"))
    elif placeholders:
        checks.append(
            Check(
                "Key",
                "error",
                "检测到占位符 Key：" + "、".join(placeholders),
                "把 .env 里的占位符替换成真实 Key（占位符真跑只会 401）",
            )
        )
    elif missing:
        checks.append(
            Check(
                "Key",
                "error",
                "缺少角色 Key：" + "、".join(missing),
                "配置全局 PM_API_KEY / PM_API_KEYS，或为每个角色设 PM_<角色>_API_KEY",
            )
        )
    else:
        parts = []
        if keys["global"]:
            parts.append("全局 PM_API_KEY")
        if keys["pool_count"]:
            parts.append(f"多 Key 池 {keys['pool_count']} 把")
        if keys["role_keys"]:
            parts.append(f"角色级 {len(keys['role_keys'])} 个")
        checks.append(Check("Key", "ok", " / ".join(parts) or "已配置"))

    roles, dep_error = _roles_fact(pool_count=int(keys["pool_count"]))
    if dep_error:
        checks.append(
            Check(
                "角色配置",
                "error",
                f"无法构造角色配置：{dep_error}",
                "先安装依赖：pip install -r requirements.txt",
            )
        )
    else:
        provider = (os.getenv("PM_PROVIDER") or "openai").strip() or "openai"
        if provider == "anthropic" and importlib.util.find_spec("langchain_anthropic") is None:
            checks.append(
                Check(
                    "角色配置",
                    "error",
                    "PM_PROVIDER=anthropic，但未安装 langchain-anthropic",
                    "pip install '.[anthropic]'",
                )
            )
        else:
            checks.append(
                Check("角色配置", "ok", f"{len(roles)} 个角色可构造（provider={provider}）")
            )

    logs = _logs_fact()
    size_mb = logs["bytes"] / (1024 * 1024)
    logs_detail = f"{logs['path']}（run_*.json {logs['run_files']} 个 / {size_mb:.1f} MB）"
    if not logs["writable"]:
        checks.append(
            Check("日志目录", "error", logs_detail + " 不可写", "检查目录权限或设 PM_LOG_DIR")
        )
    elif size_mb > 500:
        checks.append(
            Check(
                "日志目录",
                "warn",
                logs_detail,
                "超过 500MB：可清理旧的 run_*.json / report_*.md（logs/ 是运行产物）",
            )
        )
    else:
        checks.append(Check("日志目录", "ok", logs_detail))

    ports = {"8080": _port_state("127.0.0.1", 8080), "8765": _port_state("127.0.0.1", 8765)}
    busy = [p for p, state in ports.items() if state == "in_use"]
    checks.append(
        Check(
            "端口",
            "info" if busy else "ok",
            ("已被占用：" + "、".join(busy) + "（可能已有服务在跑）")
            if busy
            else "8080 / 8765 空闲",
        )
    )

    server_url = (os.getenv("PM_SERVER_URL") or "http://127.0.0.1:8080").strip()
    server = _server_state(server_url)
    checks.append(
        Check(
            "常驻服务",
            "ok" if server["running"] else "info",
            (f"{server_url} 已在运行") if server["running"] else (f"{server_url} 未启动"),
            ""
            if server["running"]
            else "submit/wait 需要先 python run_server.py（同步 --task 不需要）",
        )
    )

    mcp_installed = importlib.util.find_spec("mcp") is not None
    checks.append(
        Check(
            "MCP",
            "ok" if mcp_installed else "warn",
            "已安装" if mcp_installed else "未安装 mcp 包，python -m pm.mcp_server 不可用",
            "" if mcp_installed else "pip install -r requirements.txt",
        )
    )

    report.facts = {
        "python": sys.version.split()[0],
        "cwd": str(Path.cwd()),
        "env_files": env_files,
        "fake_backend": fake,
        "keys": keys,
        "roles": roles,
        "logs": logs,
        "ports": ports,
        "server": {"url": server_url, **server},
        "mcp": {"installed": mcp_installed},
    }
    return report


def render_doctor(report: DoctorReport) -> str:
    lines = ["PromptMaster 环境诊断（零 LLM 调用）", ""]
    for c in report.checks:
        lines.append(f"{_ICON.get(c.status, '·')} {c.name}：{c.detail}")
        if c.hint:
            lines.append(f"   → {c.hint}")
    roles = report.facts.get("roles") or []
    if roles:
        lines += ["", "角色配置：", "| 角色 | 模型 | 端点 | Key |", "|---|---|---|---|"]
        for r in roles:
            if r.get("error"):
                lines.append(f"| {r['role']} | — | — | {r['error']} |")
            else:
                lines.append(f"| {r['role']} | {r['model']} | {r['base_url']} | {r['key_tail']} |")
    lines += ["", f"结论：{_verdict(report)}"]
    return "\n".join(lines)


def _verdict(report: DoctorReport) -> str:
    if report.errors:
        return f"{len(report.errors)} 项错误需要处理（配置层面还跑不起来）。"
    if report.warnings:
        return f"{len(report.warnings)} 项警告；不挡运行，但建议处理。"
    return "配置可用。真实运行前建议再跑一次 `run.py --preflight`（会花少量调用）。"


__all__ = ["Check", "DoctorReport", "build_report", "render_doctor"]
