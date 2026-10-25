"""改判打分页（`scripts/build_rejudge_form.py`）的承重判据。

守的是一条线：**所有者必须能看到完整输出才能判分**。

`score_form.owner_rejudge.csv` 那张表把输出截到 160 字（`_make_score_form` 的 `_clip`），
而这次要判的恰恰是只有看全文才判得出来的两类东西——编造的关键事实、核心交付物缺位。
凭摘要打出来的分是"编的分"，与本仓已经踩过一次的事故同类（活账本里 6/11 条锚点的人工分
是编的）。所以这几条判据全部钉在"一字不少"上，而不是钉在"页面能渲染"上。

DOM 那两条需要 node + jsdom，CI 上大概率没有 ⇒ 缺依赖就 skip 并写明原因，
不在 CI 上假装绿；Python 侧的逐字比对不依赖任何外部工具，永远会跑。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
BUILDER = SCRIPTS / "build_rejudge_form.py"
SMOKE = SCRIPTS / "smoke_rejudge_form.js"
ANCHORS = ROOT / "judge_calibration" / "samples.candidates.json"

_DATA_RE = re.compile(r"const DATA = (\[.*?\]);\n", re.S)


def _build(tmp_path: Path) -> Path:
    sys.path.insert(0, str(SCRIPTS))
    sys.path.insert(0, str(ROOT))
    try:
        import build_rejudge_form
    finally:
        sys.path.remove(str(SCRIPTS))
        sys.path.remove(str(ROOT))
    out = tmp_path / "rejudge_form.html"
    assert build_rejudge_form.build(out) == 0
    return out


def _payload(html_path: Path) -> list[dict]:
    m = _DATA_RE.search(html_path.read_text(encoding="utf-8"))
    assert m, "页面里找不到内联数据（生成器改了形状，本文件要跟着改）"
    return json.loads(m.group(1))


def test_every_item_carries_the_whole_output(tmp_path):
    """25 条条条与源 JSON 逐字等长——少一个字都可能正好少掉那处编造。"""
    rows = _payload(_build(tmp_path))
    src = {
        str(it.get("id")): (it.get("test_output") or "")
        for it in json.loads(ANCHORS.read_text(encoding="utf-8"))
        if isinstance(it, dict) and it.get("id")
    }
    assert len(rows) == 25, f"待改判条数变了（{len(rows)}），先确认 plan() 的口径"
    for row in rows:
        assert row["output"] == src[row["id"]], f"{row['id']} 的输出与源 JSON 不一致"


def test_form_carries_no_prefilled_scores(tmp_path):
    """页面上不许预填分：AI 代判的分摆在那儿就是锚定，留空才是"等你判"。"""
    rows = _payload(_build(tmp_path))
    assert all("human_score" not in row for row in rows)


def test_export_columns_match_the_score_form_contract(tmp_path):
    """导出的表必须能被 `--apply-scores` 直接吃下：列名与 `_FORM_COLUMNS` 一致。"""
    sys.path.insert(0, str(ROOT))
    try:
        from pm.cli.calibrate import _FORM_COLUMNS
    finally:
        sys.path.remove(str(ROOT))
    html = _build(tmp_path).read_text(encoding="utf-8")
    m = re.search(r"const cols = (\[.*?\]);", html)
    assert m, "导出那段找不到列定义"
    assert json.loads(m.group(1)) == list(_FORM_COLUMNS)


def test_judge_score_is_hidden_by_default(tmp_path):
    """评委分默认不在视线里：它是同族模型的读数，摆在最显眼处就是锚定。"""
    html = _build(tmp_path).read_text(encoding="utf-8")
    assert ".judge{display:none}" in html.replace(" ", ""), "评委分默认隐藏的样式被改掉了"
    assert "显示评委分参考" in html, "要有一键展开的入口（不是为了藏，是为了不默认锚定）"


@pytest.mark.skipif(shutil.which("node") is None, reason="本机没有 node，跳过 JS 语法检查")
def test_inline_script_is_syntactically_valid(tmp_path):
    """整段 <script> 要能过 `node --check`：JS 是整段编译的，一处语法错会丢掉整个脚本。"""
    html = _build(tmp_path).read_text(encoding="utf-8")
    m = re.search(r"<script>(.*)</script>", html, re.S)
    assert m
    js = tmp_path / "inline.js"
    js.write_text(m.group(1), encoding="utf-8")
    # encoding 必须显式给：Windows 的默认码页是 cp936，node 吐 UTF-8 时
    # `text=True` 会走 locale 解码 → 读取线程抛 UnicodeDecodeError、
    # rc.stdout/stderr 变空。判据就退化成"什么都不含"，报错信息也变成空白。
    rc = subprocess.run(
        [shutil.which("node"), "--check", str(js)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert rc.returncode == 0, rc.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="本机没有 node，跳过 DOM 冒烟")
def test_dom_smoke(tmp_path):
    """真点一遍：卡片数、转义、进度记账、导出行数与列。缺 jsdom 时脚本自己退 3。"""
    import os

    html = _build(tmp_path)
    rc = subprocess.run(
        [shutil.which("node"), str(SMOKE), str(html), str(ANCHORS)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        env=dict(os.environ),
    )
    if rc.returncode == 3:
        pytest.skip("jsdom 不可用（WS 指到的 workspace 里没装）")
    assert rc.returncode == 0, rc.stdout + rc.stderr
    assert "冒烟全部通过" in rc.stdout, rc.stdout
