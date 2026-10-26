#!/usr/bin/env bash
# 本地端到端冒烟：action.yml 的执行体（scripts/action-prompt-gate.sh）在**真 git 仓库**里的三条路径。
# 用例文本取自 tests/test_diff_base_ref.py 的 CLEAN / REGRESSED（同一对，跨文件复用以免两处漂移）。
# 期望：回归 → rc 1（ghout 里 regressed=1）；干净 → rc 0；新增文件 → rc 0 且 skipped=1（不算通过）。
set -uo pipefail

ROOT=$(cygpath -u 'D:\projects\prompt-master')
PY=$(cygpath -u 'D:\projects\prompt-master\.venv\Scripts\python.exe')
REPO="$ROOT/.pytest_tmp/action-smoke"

rm -rf "$REPO"
mkdir -p "$REPO/prompts"
cd "$REPO" || exit 3
git init -q .
git config user.email ci@example.invalid
git config user.name CI

CLEAN='[角色] 你是资深销售数据分析师。
[任务] 依据用户提供的销售数据，输出趋势结论、异常点与下一步建议。
[输出格式] Markdown 表格：区域｜销售额｜同比，随后给出 3 条结论。
[边界处理] 输入完全为空时标注「数据缺失」；仅部分字段缺失时已有数据照常输出。'

REGRESSED='[角色] 你是资深销售数据分析师，负责把区域销售数据整理成可决策的结论。
[任务] 依据用户提供的销售数据，输出趋势结论、异常点与下一步建议三部分内容。
[输出格式] Markdown 表格：区域｜销售额｜同比，随后给出 3 条结论。
[判断规则] 所有结论必须引用输入中的具体数值，禁止只给定性描述。
[边界处理] 输入异常时停止处理，不输出任何结论。
[补充说明] 结论按重要性排序，每条不超过 50 字，避免重复表述同一现象。'

printf '%s\n' "$CLEAN" >prompts/p.md
git add . >/dev/null && git commit -q -m base

export PM_CLI="$PY $ROOT/run.py"
export GITHUB_OUTPUT="$REPO/ghout.txt"

echo "===== ① 工作区制造回归（相对 HEAD）====="
printf '%s\n' "$REGRESSED" >prompts/p.md
PM_PATHS="prompts/p.md" PM_BASE_REF=HEAD bash "$ROOT/scripts/action-prompt-gate.sh"
echo "RC1=$?"
echo "--- ghout ---"; cat "$GITHUB_OUTPUT"

echo "===== ② 干净版（与基线一致）====="
: >"$GITHUB_OUTPUT"
git checkout -q -- prompts/p.md
PM_PATHS="prompts/p.md" PM_BASE_REF=HEAD bash "$ROOT/scripts/action-prompt-gate.sh" 2>&1 | tail -3
PM_PATHS="prompts/p.md" PM_BASE_REF=HEAD bash "$ROOT/scripts/action-prompt-gate.sh" >/dev/null 2>&1
echo "RC2=$?"
echo "--- ghout ---"; cat "$GITHUB_OUTPUT"

echo "===== ③ 新增文件（基线里没有）====="
: >"$GITHUB_OUTPUT"
printf '%s\n' "$CLEAN" >prompts/new.md
PM_PATHS="prompts/new.md" PM_BASE_REF=HEAD bash "$ROOT/scripts/action-prompt-gate.sh"
echo "RC3=$?"
echo "--- ghout ---"; cat "$GITHUB_OUTPUT"

echo "===== ④ 空 paths（应当 rc 2 并给人话）====="
PM_PATHS="" PM_BASE_REF=HEAD bash "$ROOT/scripts/action-prompt-gate.sh"
echo "RC4=$?"

echo "===== ⑤ 坏 base-ref（应当 rc 2）====="
PM_PATHS="prompts/p.md" PM_BASE_REF="origin/nope" bash "$ROOT/scripts/action-prompt-gate.sh"
echo "RC5=$?"