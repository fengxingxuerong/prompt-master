"""端点预检（--preflight）回归。

覆盖：
1. 冒烟成功：ok=True、延迟、Key 脱敏（只留尾号 4 位，绝不出现在完整形式）；
2. 冒烟失败：错误签名归类（rate_limit/gateway/404/quota/401）与对症建议；
3. 条件角色：PM_JUDGES=1 时不探测 evaluator_b；PM_PAIRWISE=0 时不探测 comparator；
4. 缺 Key：不发起调用，直接给 auth 失败与配置建议；
5. run.py --preflight 接线：全绿退出 0、有失败退出 1。
"""

from __future__ import annotations

import os
import subprocess
import sys

from pm import backend
from pm.preflight import (
    PreflightReport,
    RoleReport,
    _classify_error,
    _mask_key,
    preflight_roles,
    render_preflight,
    run_preflight,
)
from pm.testing import _fake_plain


def test_mask_key_never_reveals_full_secret():
    assert _mask_key("sk-abcdefghij123456") == "…3456"
    assert _mask_key("abc") == "…"
    assert _mask_key("") == ""
    assert "sk-abcdefghij123456" not in _mask_key("sk-abcdefghij123456")


def test_classify_error_signatures():
    assert _classify_error("Error code: 429 - rate limit exceeded")[0] == "rate_limit"
    assert _classify_error("504 Gateway Time-out")[0] == "gateway"
    assert _classify_error("HTTP 401 Unauthorized")[0] == "auth"
    assert _classify_error("Error code: 404 - model not found")[0] == "not_found"
    assert _classify_error("insufficient_quota")[0] == "quota"
    assert _classify_error("something weird")[0] == "other"
    # 建议必须给到可操作文本
    _kind, advice = _classify_error("429 rate limit")
    assert advice and "5 分钟" in advice


def test_conditional_roles_respect_env(monkeypatch):
    monkeypatch.setenv("PM_JUDGES", "1")
    monkeypatch.setenv("PM_PAIRWISE", "0")
    roles = preflight_roles()
    assert "evaluator_b" not in roles
    assert "arbiter" not in roles, "单评委配置下不会有分歧仲裁，不必探测"
    assert "comparator" not in roles
    assert "evaluator" in roles and "target" in roles

    monkeypatch.setenv("PM_JUDGES", "2")
    monkeypatch.setenv("PM_PAIRWISE", "1")
    roles2 = preflight_roles()
    assert "evaluator_b" in roles2
    assert "arbiter" in roles2, "仲裁评委只在跑批中途出场，预检必须提前发现它配错"
    assert "comparator" in roles2


def test_run_preflight_success_and_masking(monkeypatch):
    monkeypatch.setenv("PM_API_KEY", "sk-secret-key-9876")
    hook = backend.CallHook(structured=None, plain=_fake_plain, disable_cache=True)
    with backend.use(hook):
        report = run_preflight()
    assert report.roles, "至少探测基础角色"
    assert report.all_ok
    rendered = render_preflight(report)
    # Key 只以尾号出现，完整 Key 绝不能出现在渲染结果里
    assert "9876" in rendered
    assert "sk-secret-key-9876" not in rendered


def test_run_preflight_failure_classification(monkeypatch):
    monkeypatch.setenv("PM_API_KEY", "sk-test-key-1111")

    def failing_plain(role, system, user, overrides=None):
        if role == "target":
            raise RuntimeError("Error code: 429 - rate limit exceeded")
        return _fake_plain(role, system, user, overrides=overrides)

    hook = backend.CallHook(structured=None, plain=failing_plain, disable_cache=True)
    with backend.use(hook):
        report = run_preflight()
    assert not report.all_ok
    target = next(r for r in report.roles if r.role == "target")
    assert target.ok is False
    assert target.error_kind == "rate_limit"
    assert target.advice and "5 分钟" in target.advice
    # 其余角色不受单个角色失败影响
    assert next(r for r in report.roles if r.role == "clarifier").ok


def test_run_preflight_missing_key_skips_call(monkeypatch):
    monkeypatch.delenv("PM_API_KEY", raising=False)
    monkeypatch.delenv("PM_TARGET_API_KEY", raising=False)

    def must_not_call(role, system, user, overrides=None):
        raise AssertionError("缺 Key 时不应发起任何调用")

    hook = backend.CallHook(structured=None, plain=must_not_call, disable_cache=True)
    with backend.use(hook):
        report = run_preflight()
    assert not report.all_ok
    missing = [r for r in report.roles if r.error_kind == "auth"]
    assert missing, "缺 Key 角色应报 auth 失败"
    assert all("PM_API_KEY" in (r.advice or "") for r in missing)


def test_render_preflight_summary_lines():
    report = PreflightReport(
        roles=[
            RoleReport(role="clarifier", model="m", base_url="http://x", key_tail="…1234", ok=True),
            RoleReport(
                role="target",
                model="t",
                base_url="http://y",
                key_tail="…5678",
                ok=False,
                error_kind="gateway",
                advice="错峰或换端点",
            ),
        ]
    )
    text = render_preflight(report)
    assert "1 个角色预检失败：target" in text
    assert "gateway" in text and "错峰或换端点" in text


def test_run_py_preflight_wiring(tmp_path):
    """run.py --preflight 接线：端点不可达时快速失败、退出码 1、不崩。

    关键坑：run.py 的 load_dotenv() 会把仓库 .env 的真实 Key/端点注回来
    （测试进程里 pop 掉环境变量挡不住它）——不钉死角色端点的话，
    这条"离线"测试会真的去打付费端点并被限流退避卡死（实测 120s 超时）。
    环境变量优先级高于 dotenv（load_dotenv 默认 override=False），
    所以把全部角色的 base_url 钉到不可达的 loopback 端口即可安全离线。
    """
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PM_API_KEY"] = "sk-test-dummy-key-0000"
    env["PM_TIMEOUT"] = "5"
    env["PM_RATE_LIMIT_MAX_WAIT"] = "0"  # 不退避：连接拒绝是普通错误，直接上抛
    dead = "http://127.0.0.1:9/v1"  # discard 端口，本机必然连接拒绝
    for role in (
        "",
        "CLARIFIER_",
        "OPTIMIZER_",
        "MOCKGEN_",
        "EVALUATOR_",
        "EVALUATOR_B_",
        "ARBITER_",
        "REVISER_",
        "TARGET_",
        "COMPARATOR_",
    ):
        env[f"PM_{role}BASE_URL"] = dead
    proc = subprocess.run(
        [sys.executable, os.path.join(repo, "run.py"), "--preflight"],
        capture_output=True,
        env=env,
        cwd=repo,
        timeout=90,
    )
    # 全部角色不可达：退出码 1、渲染出失败汇总、无崩溃栈
    err = proc.stderr.decode("utf-8", "replace")
    out = proc.stdout.decode("utf-8", "replace")
    assert proc.returncode == 1, f"预期 exit 1，实际 {proc.returncode}；stderr={err[-300:]}"
    assert "Traceback" not in err
    assert "预检失败" in out
    assert "sk-test-dummy-key" not in out  # Key 脱敏必须生效
