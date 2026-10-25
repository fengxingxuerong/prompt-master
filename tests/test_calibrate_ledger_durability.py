"""校准账本：坏文件不许被"当空账本"吃掉，写入必须原子。

实测缺陷（2026-10-31 审计，全部在系统临时目录复现）：

1. **静默清零**：`_load_history` 遇到损坏 JSON 就 `return []`，而 `_save_entry`
   随后**整文件重写** —— 一份被截断的 2 轮账本，跑完一轮（真实计费）后只剩新写的 1 条，
   全程零提示。账本是校准结论的唯一出处，这等于"花钱买了一次历史清空"。
2. **合法 JSON 但顶层不是数组**：旧版把 dict 直接赋给 `history`，`.append` 抛
   AttributeError —— 而那正好发生在付费跑完之后、渲染之前：整轮白跑且没有任何输出。
3. **非原子写**：`write_text` 写到一半崩掉会留下语法坏掉的账本 ⇒ 下一个人读出"空"，
   于是整段历史被一次写入吃掉。

判据都在"坏输入下仍然保住历史"这一侧——正常路径的用例已经够多了。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pm.cli import calibrate as cli_calibrate  # noqa: E402

from test_cli_units import _patch_log_dir  # noqa: E402

_LEDGER = "judge_calibration_history.json"


def _seed(tmp_path: Path, text: str) -> Path:
    p = tmp_path / _LEDGER
    p.write_text(text, encoding="utf-8")
    return p


def test_truncated_ledger_is_preserved_not_wiped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """截断的账本：改名保留 + 告警 + 本次读到空（而不是静默把它覆盖掉）。"""
    _patch_log_dir(monkeypatch, tmp_path)
    original = _seed(tmp_path, '[{"judge": "evaluator", "bias": 1.0}, {"judge": "ev')
    kept = list(original.read_text(encoding="utf-8"))

    got = cli_calibrate._load_history()
    assert got == [], "坏了就该按空读，但下面两条必须同时成立"

    survivors = list(tmp_path.glob(_LEDGER + ".corrupt-*"))
    assert survivors, "坏账本被直接覆盖了：没有任何 .corrupt-* 保留件"
    assert list(survivors[0].read_text(encoding="utf-8")) == kept, "保留下来的内容变了"

    err = capsys.readouterr().err
    assert "校准账本不可用" in err, "改名了但没说出口，用户不知道历史去哪了"


def test_ledger_that_is_not_a_list_is_quarantined(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """顶层是对象也属"读得出来但不能用"：旧版会在 `.append` 上抛 AttributeError。"""
    _patch_log_dir(monkeypatch, tmp_path)
    _seed(tmp_path, '{"judge": "evaluator", "bias": 1.0}')

    assert cli_calibrate._load_history() == []
    assert list(tmp_path.glob(_LEDGER + ".corrupt-*")), "非数组账本没被保留"
    assert "顶层不是数组" in capsys.readouterr().err


def test_non_object_rows_are_skipped_with_a_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """数组里混进字符串：跳过它并说出来，别让一次坏行废掉整份账本。"""
    _patch_log_dir(monkeypatch, tmp_path)
    rows = [{"judge": "evaluator", "bias": 1.0}, "garbage", {"judge": "evaluator", "mae": 2.0}]
    _seed(tmp_path, json.dumps(rows, ensure_ascii=False))

    got = cli_calibrate._load_history()
    assert len(got) == 2 and all(isinstance(r, dict) for r in got)
    assert "不是对象" in capsys.readouterr().err


def test_missing_ledger_is_silent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    """第一次跑（账本不存在）不许告警——那是正常状态，不是损坏。"""
    _patch_log_dir(monkeypatch, tmp_path)
    assert cli_calibrate._load_history() == []
    assert capsys.readouterr().err == ""


def test_write_is_atomic_and_leaves_valid_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """写盘后必须能原样读回，且不留临时文件。"""
    _patch_log_dir(monkeypatch, tmp_path)
    path = tmp_path / _LEDGER
    rows = [{"judge": "evaluator", "bias": 1.0, "mae": 0.5}]
    cli_calibrate._write_history(path, rows)

    assert json.loads(path.read_text(encoding="utf-8")) == rows
    assert not list(tmp_path.glob(_LEDGER + ".tmp")), "临时文件没被 replace 掉"
