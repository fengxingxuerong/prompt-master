"""A/B 对照脚本（scripts/compare_scoring_ab.py）的配对与拒绝逻辑。

这个脚本决定"评分协议要不要换默认"，所以它**拒绝下结论**的那几条路径比它给出数字
更值得测：两臂分母不同时硬算差值，比"这轮作废、重跑"危险得多。
2026-09-25 的实况就是反面教材——账本只有聚合数（impression n=18 / checklist n=15），
明细没落盘，事后连丢了哪 3 条都查不出来。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "compare_scoring_ab.py"


def _load_module() -> Any:
    spec = importlib.util.spec_from_file_location("compare_scoring_ab", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mod = _load_module()


def _rec(mode: str, anchors: str, **kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "ts": "2026-09-25 03:00:00",
        "mode": mode,
        "judge": "evaluator",
        "anchors": anchors,
        "n": 2,
        "bias": 0.4,
        "mae": 0.6,
        "items": [
            {"id": "a1", "human": 8.0, "judge": 8.6, "delta": 0.6},
            {"id": "a2", "human": 7.0, "judge": 7.4, "delta": 0.4},
        ],
        "rep_items": [
            {"id": "a1", "n": 3, "range": 0.4},
            {"id": "a2", "n": 3, "range": 1.2},
        ],
        "n_failed": 0,
    }
    base.update(kw)
    return base


def _ledger(tmp_path: Path, recs: list[dict[str, Any]]) -> Path:
    p = tmp_path / "history.json"
    p.write_text(json.dumps(recs, ensure_ascii=False), encoding="utf-8")
    return p


def test_from_ledger_refuses_records_without_per_anchor_detail(tmp_path: Path) -> None:
    """补明细字段之前的记录：必须明说"无法配对、只能重跑"，并给出重跑命令。"""
    p = _ledger(
        tmp_path,
        [{"mode": "impression", "n": 18, "mae": 0.83}, {"mode": "checklist", "n": 15, "mae": 1.85}],
    )
    with pytest.raises(ValueError) as e:
        mod.from_ledger(p, "checklist")
    msg = str(e.value)
    assert "无法配对" in msg and "--scoring-mode checklist" in msg


def test_from_ledger_says_so_when_the_mode_is_absent(tmp_path: Path) -> None:
    p = _ledger(tmp_path, [_rec("impression", "abc")])
    with pytest.raises(ValueError) as e:
        mod.from_ledger(p, "checklist")
    assert "没有 mode=checklist 的记录" in str(e.value)


def test_ledger_pairing_uses_only_anchors_both_arms_scored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """两臂各丢一条不同的锚点 ⇒ 配对集必须是两边都成功的那两条，不是各自的全集。"""
    a = _rec(
        "impression",
        "same-set",
        items=[
            {"id": "a1", "human": 8.0, "judge": 8.6, "delta": 0.6},
            {"id": "a2", "human": 7.0, "judge": 7.4, "delta": 0.4},
            {"id": "a3", "human": 9.0, "judge": 6.0, "delta": -3.0},
        ],
        rep_items=[
            {"id": "a1", "n": 3, "range": 0.4},
            {"id": "a2", "n": 3, "range": 1.2},
            {"id": "a3", "n": 3, "range": 9.9},  # 只有印象式臂有它
        ],
        n_failed=0,
    )
    b = _rec(
        "checklist",
        "same-set",
        items=[
            {"id": "a1", "human": 8.0, "judge": 9.0, "delta": 1.0},
            {"id": "a2", "human": 7.0, "judge": 9.0, "delta": 2.0},
        ],
        rep_items=[
            {"id": "a1", "n": 3, "range": 0.1},
            {"id": "a2", "n": 3, "range": 0.2},
            {"id": "a9", "n": 3, "range": 0.0},  # 只有判定式臂有它
        ],
        n_failed=1,
        failed=[{"id": "a3", "error": "GatewayError: 500"}],
    )
    p = _ledger(tmp_path, [a, b])
    monkeypatch.setattr(sys, "argv", ["x", "--ledger", str(p)])
    assert mod.main() == 0
    out = capsys.readouterr().out
    assert "配对比较（两臂都成功的 2 条）" in out, out
    # a3（Δ=-3.0）与 a9 都不许混进配对集，否则"判定式更稳"是幸存者偏差
    assert "n_failed=1" in out
    assert "a3" not in out.split("配对比较")[1], "被丢掉的锚点不该进入配对段"


def test_different_anchor_sets_are_not_comparable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """两臂锚点集指纹不同就是两批考卷，硬算差值没有意义 —— 直接拒。"""
    p = _ledger(tmp_path, [_rec("impression", "set-1"), _rec("checklist", "set-2")])
    monkeypatch.setattr(sys, "argv", ["x", "--ledger", str(p)])
    assert mod.main() == 2
    assert "不是同一批考卷" in capsys.readouterr().err


def test_repeat_1_arm_still_reports_accuracy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """判定式臂只打了单发（`--repeat 1`）：稳定性确实无从算起，但准确性配对照样有效。

    这条断言补的是我自己踩到的坑：脚本原来在"无配对极差"时直接 `return 1`，
    于是 24000-token 那一轮（唯一真正想知道的 MAE）被一起砍掉，只剩一句"A/B 作废"。
    更要紧的是不能让它把空集演算成 `降幅 0.000 → 未测出改进`：那是把"没测"写成"测了没赢"。
    """
    b = _rec(
        "checklist",
        "same-set",
        items=[
            {"id": "a1", "human": 8.0, "judge": 9.0, "delta": 1.0},
            {"id": "a2", "human": 7.0, "judge": 9.0, "delta": 2.0},
        ],
        rep_items=[],
        rep_times=1,
    )
    a = _rec("impression", "same-set", rep_times=3)
    p = _ledger(tmp_path, [a, b])
    monkeypatch.setattr(sys, "argv", ["x", "--ledger", str(p)])
    assert mod.main() == 0
    out = capsys.readouterr().out
    assert "没有稳定性读数" in out, out
    assert "MAE：impression 0.50 → checklist 1.50" in out, out  # 单发分配对，照算
    assert "未被评估" in out and "未测出改进" not in out, out
    assert "默认保持 impression" in out, out


def test_nothing_at_all_paired_is_still_void(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """连单发分都没有交集时仍然是"作废"，不许走上面那条"只有准确性读数"的温和出口。"""
    a = _rec("impression", "same-set", items=[{"id": "a1", "human": 8.0, "judge": 8.6}])
    b = _rec(
        "checklist",
        "same-set",
        items=[{"id": "b1", "human": 7.0, "judge": 7.5}],
        rep_items=[],
        rep_times=1,
    )
    p = _ledger(tmp_path, [a, b])
    monkeypatch.setattr(sys, "argv", ["x", "--ledger", str(p)])
    assert mod.main() == 1
    assert "作废" in capsys.readouterr().out


def test_positions_and_ledger_are_mutually_exclusive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """一条路都不给：要报"该给什么"，不能拿 None 路径去 open() 崩在栈里。"""
    monkeypatch.setattr(sys, "argv", ["x"])
    with pytest.raises(SystemExit) as e:
        mod.main()
    assert e.value.code == 2  # argparse 的参数错误
