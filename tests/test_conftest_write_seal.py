"""conftest 密封夹具的守卫 —— 本文件**故意不写任何路径夹具**。

为什么单独一个文件：密封的性质是"任何用例忘了设变量也碰不到运营文件"。如果把守卫
写在 `test_modelhub_ledger_and_usage.py` 里，那个文件自己的 autouse 夹具会先把路径
设好，conftest 那条夹具被删掉也照样绿 —— 守卫就成了自证。

真实事故（2026-09-25，我自己造成的）：新用例把隔离变量记成 `PMH_DATA_DIR`，而
`ledger.ledger_path()` 读的是 `PMH_LEDGER_PATH`（默认 `<repo>/logs/modelhub_ledger.jsonl`），
一次 `write_text` 把线上台账（411 条调用事件）截断成 5 行测试数据；轮换件之前的历史还在
（`modelhub_ledger.jsonl.1`），那 411 条找不回来。
"""

from __future__ import annotations

import json
from pathlib import Path

from pm.modelhub import ledger as LG
from pm.modelhub import usage_store as US

ROOT = Path(__file__).resolve().parents[1]


def test_ledger_path_is_sealed_by_conftest_only() -> None:
    """不设任何夹具时，台账路径也不许指向仓库里那份运营文件。"""
    repo_default = (ROOT / "logs" / "modelhub_ledger.jsonl").resolve()
    assert LG.ledger_path().resolve() != repo_default, (
        "conftest 的 isolate_modelhub_write_paths 失效了："
        "任何忘记设 PMH_LEDGER_PATH 的用例都会直接写运营台账"
    )


def test_usage_store_path_is_sealed_by_conftest_only() -> None:
    real = (ROOT / "data" / "usage_daily.json").resolve()
    assert US._path().resolve() != real, "conftest 的 PMH_DATA_DIR 密封失效：会写真日聚合"


def test_appending_an_event_leaves_the_repo_ledger_byte_identical() -> None:
    """行为层的最终判据：写一条事件，仓库那份台账必须一字不变。"""
    real = ROOT / "logs" / "modelhub_ledger.jsonl"
    before = real.read_bytes() if real.exists() else None
    LG.append_event({"type": "call", "request_id": "seal-canary", "model": "m", "success": True})
    after = real.read_bytes() if real.exists() else None
    assert after == before, "测试写入落到了运营台账上"


def test_calibration_history_path_is_sealed_by_conftest_only() -> None:
    """校准账本也不能指向仓库那份 —— 它是 A/B 协议判定的唯一事实源。

    这条比前两条更值得守：假记录长得和真记录一模一样，`compare_scoring_ab.py`
    又是"取该模式最近一条有明细的记录"，所以污染方式是**静默**的。
    """
    from pm.cli import calibrate as cli_calibrate

    real = (ROOT / "logs" / "judge_calibration_history.json").resolve()
    got = cli_calibrate._calib_history().resolve()
    assert got != real, f"conftest 的 seal_calibration_ledger 失效，用例可直接写 A/B 账本：{got}"


def test_a_forgotten_patch_still_cannot_reach_the_real_ab_ledger() -> None:
    """模拟"某个用例忘了 `_patch_log_dir()`"：按模块属性写一次，仓库账本必须一字不变。

    只断言属性值不够（属性可能对、写盘路径可能被别处重新拼出来），所以走一次真写入。
    """
    from pm.cli import calibrate as cli_calibrate

    real = ROOT / "logs" / "judge_calibration_history.json"
    before = real.read_bytes() if real.exists() else None
    target = cli_calibrate._calib_history()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps([{"id": "seal-canary"}]), encoding="utf-8")
    assert target.exists(), "密封把路径指到了写不出去的地方（那也算失效）"
    after = real.read_bytes() if real.exists() else None
    assert after == before, "测试写入落到了 A/B 判定用的校准账本上"
