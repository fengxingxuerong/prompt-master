"""PromptMaster 面板（插件形态）的后端 —— 三个零调用端点 + 一张连接自检。

为什么挂在 `pm.server:app` 上而不是另起一个进程：CSP/nonce、`PM_API_TOKEN` 鉴权、
安全响应头、限流、调度器（④那张卡要用 `/api/optimize`）都只有一份。另起一个服务
就得把那套再实现一遍，而"两份安全头实现"正是本仓一直在收的那类漂移。

判据一律**不在这个文件里产生**：三个 POST 都是 subprocess 打 `run.py`，与 MCP 的
计算类工具同一条路（`prompt_check` / `cases_check` / `prompt_diff`）。面板改了渲染
也不会让判据跟着变；判据变了面板自动跟上。转发底层的加固项（list 参数不走 shell、
stdin 接 DEVNULL、`PYTHONIOENCODING`、退出码 0~3 才算正常）与 `pm/mcp_server.py`
逐条相等，由 `tests/test_panel.py::test_panel_and_mcp_forwarders_agree_on_hardening` 钉住。

⚠️ 这个面板**不承诺**"优化更好"。④那张卡渲染的是自评 Δ 与成对盲评，而按项目自己的
判据（C1 盲评 ≥3/4 且 C2 Δ>本轮噪声带），"优化有效"至今 0 例确证 —— 面板上那行字
不是免责声明，是当前读数。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from pm import __version__
from pm.cli.support import MAX_SEED_CASES
from pm.netguard import CSP_NONCE_PLACEHOLDER, require_token
from pm.state import SEED_PROMPT_MAX_CHARS
from pm.web._panel import PANEL_HTML

router = APIRouter()

ROOT = Path(__file__).resolve().parent.parent
RUN_PY = ROOT / "run.py"
TEMPLATE_DIR = ROOT / "case_templates"

# 用例文本上限：8 条 × 每条 8000 字的 input/expected，再算上 JSON 结构本身。
CASES_MAX_CHARS = 400_000

# 连接自检里的"转发真的能用"是一次**真跑**的探针（不是"文件存在"）。
# 结果按进程缓存：探针本身 ~0.3s，而每次开面板都打一发会把首屏拖慢。
# ⚠️ 命中条件必须是"跑过"而不是"有 note"——成功时 note 是空串，用 note 当闸门
# 会让每次开面板都重新派一发子进程（这条被 tests/test_panel.py 当场抓到过）。
_PROBE_TTL = 60.0
_probe_cache: dict[str, Any] = {"at": 0.0, "ran": False, "ok": False, "note": ""}


def run_cli_json(args: list[str], timeout: float = 60.0) -> Any:
    """subprocess 调 `run.py ... --json`，返回解析后的 JSON（加固项与 MCP 逐条相等）。"""
    proc = subprocess.run(
        [sys.executable, str(RUN_PY), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        cwd=str(ROOT),
        stdin=subprocess.DEVNULL,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    if proc.returncode not in (0, 1, 2, 3):
        raise RuntimeError(f"run.py 退出码异常 {proc.returncode}：{proc.stderr[-300:]}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"run.py 没有给出合法 JSON（{e}）；stderr 尾部：{(proc.stderr or '')[-300:]}"
        ) from e


def _cli_probe() -> dict[str, Any]:
    """真的跑一次零调用体检，把"转发链路通不通"变成实测而不是推断。"""
    now = time.time()
    if _probe_cache["ran"] and now - float(_probe_cache["at"]) < _PROBE_TTL:
        return dict(_probe_cache)
    try:
        d = run_cli_json(["check", "--prompt", "探针：一份很短的提示词", "--json"], timeout=30.0)
        ok = str(d.get("mode")) == "prompt_check"
        note = "" if ok else f"转发返回的 mode 不是 prompt_check：{d.get('mode')!r}"
    except (RuntimeError, subprocess.SubprocessError, OSError) as e:
        ok, note = False, f"零调用转发不可用：{e}"
    _probe_cache.update({"at": now, "ran": True, "ok": ok, "note": note})
    return dict(_probe_cache)


class PromptIn(BaseModel):
    prompt: str = Field(min_length=1, max_length=SEED_PROMPT_MAX_CHARS, description="待体检正文")


class CasesIn(BaseModel):
    template: str = Field(default="", max_length=80, description="case_templates/ 下的模板名")
    cases: str = Field(default="", max_length=CASES_MAX_CHARS, description="JSON 数组正文")
    assert_mode: str = Field(
        default="contains", max_length=40, description="单条没写 mode 时的缺省"
    )


class DiffIn(BaseModel):
    before: str = Field(min_length=1, max_length=SEED_PROMPT_MAX_CHARS)
    after: str = Field(min_length=1, max_length=SEED_PROMPT_MAX_CHARS)


def _template_path(name: str) -> Path:
    """把模板名解析成 `case_templates/` 里的真实文件。

    ⚠️ 这里必须**白名单**而不是"看起来没 `..` 就行"：面板可以被任何人贴一个
    `template=../../.env`，拼路径就等于把仓库外的文件读出来回显。目录列举与请求名
    两边都走同一个 `_template_names()`，所以列举里没的名字一定进不去。
    """
    if name not in _template_names():
        raise HTTPException(
            status_code=404, detail=f"模板不存在：{name}（只认 case_templates/ 里的名字）"
        )
    p = TEMPLATE_DIR / name
    if not p.is_file():
        raise HTTPException(status_code=404, detail=f"模板文件读不到：{name}")
    return p


def _template_names() -> list[str]:
    """当场列举，不缓存：新增模板要立刻在面板里可选（缓存会让"删掉的模板"继续可点）。

    这里**不写 try/except**：实测连本机那个 ACL 坏掉的目录都只返回空迭代
    （`Path(".pytest_tmp").glob("*.json") -> []`，3.12），异常分支够不到；
    而目录真的不存在时同样退成空表 —— 面板第二张卡只会少几个选项，不会整页 500。
    """
    return sorted(p.name for p in TEMPLATE_DIR.glob("*.json") if p.is_file())


@router.get("/panel", response_class=HTMLResponse)
async def panel_page(request: Request) -> str:
    """插件面板（单 HTML、内联 CSS/JS、零外部依赖）。nonce 与响应头必须同源。"""
    nonce = getattr(request.state, "csp_nonce", "")
    return PANEL_HTML.replace(CSP_NONCE_PLACEHOLDER, nonce)


@router.get("/api/panel/capabilities")
async def capabilities() -> dict[str, Any]:
    """面板首屏的连接自检：只给布尔与一句人话，不回显任何 Key 内容。"""
    probe = _cli_probe()
    fake = (os.getenv("PM_FAKE_BACKEND") or "").strip()
    key_ready = (
        bool(fake)
        or bool((os.getenv("PM_API_KEY") or "").strip())
        or bool((os.getenv("PM_API_KEYS") or "").strip())
    )
    return {
        "service": "prompt-master-panel",
        "version": __version__,
        "cli_ok": bool(probe["ok"]),
        "token_required": bool((os.getenv("PM_API_TOKEN") or "").strip()),
        "key_ready": key_ready,
        "mode": "fake" if fake else ("real" if key_ready else "none"),
        "note": str(probe["note"] or ""),
    }


@router.get("/api/panel/templates")
async def templates() -> dict[str, Any]:
    """`case_templates/` 的当场列举（只给名字与条数，不给正文——正文由 POST 现取）。"""
    out = []
    for name in _template_names():
        try:
            raw = json.loads((TEMPLATE_DIR / name).read_text(encoding="utf-8"))
            count = len(raw) if isinstance(raw, list) else 0
        except (OSError, json.JSONDecodeError):
            count = 0
        out.append({"name": name, "cases": count})
    return {"templates": out, "max_cases": MAX_SEED_CASES}


@router.post("/api/panel/check", dependencies=[Depends(require_token)])
async def panel_check(body: PromptIn) -> JSONResponse:
    """① 提示词体检：转发 `run.py check --prompt ... --json`（零 LLM 调用）。"""
    d = run_cli_json(["check", "--prompt", body.prompt, "--json"])
    return JSONResponse(d)


@router.post("/api/panel/cases-check", dependencies=[Depends(require_token)])
async def panel_cases_check(body: CasesIn) -> JSONResponse:
    """② 考卷体检：模板名或粘贴正文二选一，转发同一条 CLI 判据。"""
    if body.template:
        src = str(_template_path(body.template))
    elif body.cases.strip():
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "cases.json"
            f.write_text(body.cases, encoding="utf-8")
            res = run_cli_json(
                ["check", "--cases-file", str(f), "--assert-mode", body.assert_mode, "--json"]
            )
        return JSONResponse(res)
    else:
        raise HTTPException(status_code=422, detail="template 与 cases 至少要给一个")
    res = run_cli_json(["check", "--cases-file", src, "--assert-mode", body.assert_mode, "--json"])
    return JSONResponse(res)


@router.post("/api/panel/diff", dependencies=[Depends(require_token)])
async def panel_diff(body: DiffIn) -> JSONResponse:
    """③ 两版差分：临时文件中转（与 MCP 的 `prompt_diff` 同一形状），判据仍在 CLI。"""
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        a, b = Path(d) / "a.md", Path(d) / "b.md"
        a.write_text(body.before, encoding="utf-8")
        b.write_text(body.after, encoding="utf-8")
        res = run_cli_json(["diff", str(a), str(b), "--json"])
    return JSONResponse(res)
