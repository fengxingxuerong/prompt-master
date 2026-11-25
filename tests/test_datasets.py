"""版本化数据集仓库与 CLI 接线的回归测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pm.cli.main import _build_parser, _load_seed_cases
from pm.datasets import (
    DatasetError,
    add_version,
    diff_datasets,
    get_dataset,
    list_datasets,
    parse_ref,
)


def test_versions_are_append_only_and_hash_the_canonical_cases(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PM_DATASET_DIR", str(tmp_path))
    first = add_version("sales", [{"input": "甲"}, {"input": "乙", "expected": "值"}], "销售集")
    second = add_version(
        "sales", [{"expected": "值", "input": "乙"}, {"input": "甲"}, {"input": "丙"}]
    )

    assert (first["version"], second["version"]) == (1, 2)
    assert get_dataset("sales", 1)["version"]["cases"][0]["input"] == "甲"
    assert len(get_dataset("sales", 1)["version"]["cases"]) == 2
    assert len(get_dataset("sales")["version"]["cases"]) == 3
    assert first["sha256"] != second["sha256"]
    assert list_datasets() == [
        {
            "name": "sales",
            "description": "销售集",
            "versions": 2,
            "latest_version": 2,
            "case_count": 3,
        }
    ]


def test_diff_names_added_removed_changed_and_unchanged(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PM_DATASET_DIR", str(tmp_path))
    add_version("qa", ["keep", "change", "remove"])
    add_version("qa", ["keep", "changed", "remove", "new"])

    diff = diff_datasets("qa", 1, 2)
    assert diff["added"] == [{"input": "new"}]
    assert diff["removed"] == []
    assert diff["changed"] == [
        {"case": 2, "before": {"input": "change"}, "after": {"input": "changed"}}
    ]
    assert diff["unchanged"] == 2
    reverse = diff_datasets("qa", 2, 1)
    assert reverse["added"] == [] and reverse["removed"] == [{"input": "new"}]
    assert reverse["changed"] == [
        {"case": 2, "before": {"input": "changed"}, "after": {"input": "change"}}
    ]


@pytest.mark.parametrize("reference", ["../x", "/x", "a/b", "", "x:0x", "x:"])
def test_invalid_dataset_references_are_rejected(reference: str) -> None:
    with pytest.raises(DatasetError):
        parse_ref(reference)


def test_latest_and_pinned_version_are_distinct(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PM_DATASET_DIR", str(tmp_path))
    add_version("qa", ["old"])
    add_version("qa", ["new"])
    assert parse_ref("qa") == ("qa", None)
    assert parse_ref("qa:1") == ("qa", 1)
    assert get_dataset("qa", 1)["version"]["cases"] == [{"input": "old"}]
    with pytest.raises(DatasetError, match="没有版本 3"):
        get_dataset("qa", 3)


def test_dataset_and_cases_file_are_mutually_exclusive(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PM_DATASET_DIR", str(tmp_path))
    cases = tmp_path / "cases.json"
    cases.write_text('[{"input":"x"}]', encoding="utf-8")
    add_version("qa", ["x"])
    parser = _build_parser("run.py")
    args = parser.parse_args(["--task", "t", "--cases-file", str(cases), "--dataset", "qa"])
    with pytest.raises(SystemExit):
        _load_seed_cases(parser, args)


def test_pinned_dataset_feeds_the_existing_seed_case_path(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PM_DATASET_DIR", str(tmp_path))
    add_version("qa", [{"input": "旧题", "expected": "旧答案", "mode": "contains"}])
    add_version("qa", [{"input": "新题", "expected": "新答案", "mode": "contains"}])
    parser = _build_parser("run.py")

    assert _load_seed_cases(parser, parser.parse_args(["--task", "t", "--dataset", "qa:1"])) == [
        {"input": "旧题", "expected": "旧答案", "mode": "contains"}
    ]


def test_dataset_cli_add_show_and_diff(tmp_path: Path, monkeypatch, capsys) -> None:
    from pm.cli.datasetcmd import dataset_command

    monkeypatch.setenv("PM_DATASET_DIR", str(tmp_path))
    first = tmp_path / "v1.json"
    second = tmp_path / "v2.json"
    first.write_text(json.dumps([{"input": "a", "expected": "A"}]), encoding="utf-8")
    second.write_text(
        json.dumps([{"input": "a", "expected": "A"}, {"input": "b"}]), encoding="utf-8"
    )

    assert dataset_command(["add", "qa", "--cases-file", str(first)]) == 0
    assert "已添加数据集：qa  版本：1" in capsys.readouterr().out
    assert dataset_command(["add", "qa", "--cases-file", str(second), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["version"] == 2
    assert dataset_command(["show", "qa", "--version", "1", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["version"]["case_count"] == 1
    assert (
        dataset_command(["diff", "qa", "--from-version", "1", "--to-version", "2", "--json"]) == 0
    )
    assert len(json.loads(capsys.readouterr().out)["added"]) == 1


def test_add_rejects_invalid_assertion_shapes(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PM_DATASET_DIR", str(tmp_path))
    with pytest.raises(DatasetError, match="mode 非法"):
        add_version("qa", [{"input": "x", "mode": "made-up"}])
    with pytest.raises(DatasetError, match="contains-any"):
        add_version("qa", [{"input": "x", "expected": "not-an-array", "mode": "contains-any"}])


def test_dataset_check_reuses_the_shared_case_verdict(tmp_path: Path, monkeypatch, capsys) -> None:
    from pm.cli.datasetcmd import dataset_command

    monkeypatch.setenv("PM_DATASET_DIR", str(tmp_path))
    add_version("weak", [{"input": "复制甲", "expected": "甲", "mode": "contains"}])
    add_version("strong", [{"input": "输入甲", "expected": "结论甲", "mode": "contains"}])

    assert dataset_command(["check", "weak", "--json"]) == 1
    weak = json.loads(capsys.readouterr().out)
    assert weak["mode"] == "dataset_check" and weak["verdict"] == "rejected"
    assert weak["dataset_version"] == 1 and weak["dataset_sha256"]
    assert {finding["code"] for finding in weak["findings"]} == {"echoes"}

    assert dataset_command(["check", "strong"]) == 0
    text = capsys.readouterr().out
    assert "数据集体检：strong" in text and "强判据落在第 1 条" in text


def test_malformed_dataset_file_is_reported(tmp_path: Path, monkeypatch, capsys) -> None:
    from pm.cli.datasetcmd import dataset_command

    monkeypatch.setenv("PM_DATASET_DIR", str(tmp_path))
    (tmp_path / "broken.json").write_text("not json", encoding="utf-8")
    assert dataset_command(["list", "--json"]) == 0
    row = json.loads(capsys.readouterr().out)[0]
    assert row["name"] == "broken"
    assert "无法读取" in row["error"]
