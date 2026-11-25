"""CLI for versioned evaluation datasets."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from pm.datasets import DatasetError, add_version, diff_datasets, get_dataset, list_datasets

from .support import EXIT_CONFIG, read_cases_file


def _emit(payload: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    if isinstance(payload, list):
        if not payload:
            print("暂无数据集。用 dataset add <名称> --cases-file <文件> 创建。")
            return
        for row in payload:
            if row.get("error"):
                print(f"[错误] {row['name']}: {row['error']}")
            else:
                print(
                    f"{row['name']}  v{row['latest_version']}  "
                    f"{row['case_count']} 条用例  {row['description']}"
                )
        return
    version = payload.get("version")
    if not isinstance(version, dict):
        print(
            f"已添加数据集：{payload['name']}  版本：{version}  "
            f"{payload['case_count']} 条用例  SHA256：{payload['sha256']}"
        )
        return
    print(f"数据集：{payload['name']}  版本：{version['version']}")
    print(f"用例数：{version['case_count']}  SHA256：{version['sha256']}")
    if payload.get("description"):
        print(f"说明：{payload['description']}")
    for index, case in enumerate(version["cases"], 1):
        print(f"  {index}. {json.dumps(case, ensure_ascii=False)}")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="run.py dataset", description="版本化评测数据集")
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("list", help="列出数据集").add_argument("--json", action="store_true")
    add = sub.add_parser("add", help="追加一个新版本")
    add.add_argument("name")
    add.add_argument("--cases-file", required=True)
    add.add_argument("--description", default="")
    add.add_argument("--json", action="store_true")
    show = sub.add_parser("show", help="读取数据集或指定版本")
    show.add_argument("name")
    show.add_argument("--version", type=int)
    show.add_argument("--json", action="store_true")
    diff = sub.add_parser("diff", help="比较两个版本")
    diff.add_argument("name")
    diff.add_argument("--from-version", type=int, required=True)
    diff.add_argument("--to-version", type=int, required=True)
    diff.add_argument("--json", action="store_true")
    return parser


def dataset_command(argv: list[str]) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.action == "list":
            payload: Any = list_datasets()
        elif args.action == "add":
            payload = add_version(
                args.name, read_cases_file(parser, args.cases_file), args.description
            )
        elif args.action == "show":
            payload = get_dataset(args.name, args.version)
        else:
            payload = diff_datasets(args.name, args.from_version, args.to_version)
    except DatasetError as exc:
        print(f"数据集错误：{exc}", file=sys.stderr)
        return EXIT_CONFIG
    _emit(payload, args.json)
    return 0
