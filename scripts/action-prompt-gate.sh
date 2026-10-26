#!/usr/bin/env bash
# PromptMaster GitHub Action 的执行体（action.yml 只负责装 Python 与包，然后调本脚本）。
#
# 为什么逻辑放脚本、而不是 action.yml 里的多行 run：
#   yml 里的多行 bash **没法在本地跑**，而本仓有一条"不许发布没跑过的接线"的规矩（Round 34 为此
#   拒绝过 Action）。脚本能在本机 bash 下真跑，见 tests/test_skill_package.py 的端到端用例
#   （临时 git 仓库 + 真造一次回归 → 期望退出码 1）与 iteration-ledger.md 的 Round 44。
#
# 判据与 CLI 完全同口径，这里不另立一套：
#   rc 0 = 没有新引入；rc 1 = 新引入（门禁拦下）；rc 2 = 参数/配置错；rc 3 = 运行失败。
#
# 不做的事（有意）：不跑计费闭环（没有 --task / submit）、不联网、不读 Key、不改用户仓库。
set -uo pipefail

PATHS=${PM_PATHS:-}
BASE_REF=${PM_BASE_REF:-}
PM_CLI=${PM_CLI:-prompt-master}
PM_CLI_BIN=${PM_CLI%% *}

die() { echo "::error::$*"; exit 2; }

[ -n "$PATHS" ] || die "inputs.paths 为空：至少给一个提示词文件（空格或换行分隔，支持 prompts/*.md 这类通配）"
command -v "$PM_CLI_BIN" >/dev/null 2>&1 || {
  echo "::error::$PM_CLI_BIN 不在 PATH —— action.yml 的安装步没跑成"
  exit 3
}

# 基线：调用方给了就用（**显式给 HEAD 是正当用法**：比"工作区 vs 已提交"）；
# 没给就在仓库里按与 `gate` **同一份候选表**探测，并跳过等于 HEAD 的候选 ——
# 这条不是我们自己想的：`pm/promptgate.py::resolve_base` 有实测缺陷记录（2026-10-02）——
# 单提交仓库里 `master` 能解析且**就是 HEAD**，拿它当基线 diff 出来永远是"没有改动"，
# 门禁**静默通过**。"长得像一次成功的检查"比报错危险得多，所以这里必须同一条规矩。
ZERO_SHA=$(printf '0%.0s' 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20 \
  21 22 23 24 25 26 27 28 29 30 31 32 33 34 35 36 37 38 39 40)
no_baseline=0
if [ "$BASE_REF" = "$ZERO_SHA" ]; then
  # 与 `gate` 的 rc=3 同口径：GitHub 在"新分支首次推送"时给的就是全零 SHA，
  # 那是**客观没有基线**，不是"检查通过"。放行但打横幅，并明写"未做判定"。
  echo "::warning::base-ref 是全零 SHA：本次为新分支首次推送，客观上没有可对比的版本"
  BASE_REF=""
  no_baseline=1
elif [ -z "$BASE_REF" ]; then
  head_sha=$(git rev-parse --verify -q HEAD 2>/dev/null)
  saw_head_itself=0
  for cand in "origin/main" "origin/master" "main" "master" "HEAD~1"; do
    cand_sha=$(git rev-parse --verify -q "${cand}^{commit}" 2>/dev/null) || continue
    if [ -n "$head_sha" ] && [ "$cand_sha" = "$head_sha" ]; then
      saw_head_itself=1
      echo "::notice::候选基线 $cand 与 HEAD 是同一个提交（比对自己 = 没比），跳过"
      continue
    fi
    BASE_REF=$cand
    break
  done
  if [ -z "$BASE_REF" ] && [ "$saw_head_itself" -eq 1 ]; then
    die "自动探测到的候选基线全都等于当前 HEAD：没有「更早的状态」可比（比对自己 = 假绿）。请显式给 base-ref（例如 PR 的 base.sha 或 origin/main）"
  fi
fi

if [ "$no_baseline" -eq 0 ]; then
  [ -n "$BASE_REF" ] || die "拿不到可用的 git 基线：请显式给 base-ref（例如 origin/main）"
  git rev-parse --verify -q "$BASE_REF" >/dev/null 2>&1 || die "base-ref '$BASE_REF' 不是本仓库能解析的引用（CI 上需要 actions/checkout 的 fetch-depth: 0）"
  echo "基线 = $BASE_REF"
fi

checked=0
skipped=0
regressed=0
errf=$(mktemp)
trap 'rm -f "$errf"' EXIT

for f in $PATHS; do
  if [ "$no_baseline" -eq 1 ]; then
    # 没有基线时的每一个文件都**未做判定**：计 skipped 而不是 checked，
    # 免得 output 把"没比"读成"比过且没问题"。
    echo "::notice::$f 未做回归判定（本次没有可对比的基线）"
    skipped=$((skipped + 1))
    continue
  fi
  if [ ! -f "$f" ]; then
    echo "::warning::跳过（文件不存在）：$f"
    skipped=$((skipped + 1))
    continue
  fi
  # 新增文件在基线里没有对照物：这不算"通过"，所以明说是跳过而不是悄悄放过。
  if ! git cat-file -e "$BASE_REF:$f" 2>/dev/null; then
    echo "::notice::$f 在基线 $BASE_REF 里不存在（新增文件）—— 没有比较对象，跳过（不计入通过）"
    skipped=$((skipped + 1))
    continue
  fi

  out=$($PM_CLI diff --base-ref "$BASE_REF" --no-diff --json "$f" 2>"$errf")
  rc=$?
  checked=$((checked + 1))
  case $rc in
    0)
      echo "::notice file=$f::$f 相对 $BASE_REF 没有新引入的规则问题"
      ;;
    1)
      regressed=$((regressed + 1))
      echo "::error file=$f::$f 相对 $BASE_REF 新引入了规则问题（完整差分见下方 JSON）"
      printf '%s\n' "$out"
      ;;
    *)
      echo "::error::$f 差分失败（rc=$rc）：$(cat "$errf")"
      exit "$rc"
      ;;
  esac
  if [ -s "$errf" ]; then cat "$errf"; fi
done

echo "体检 $checked 个文件，跳过 $skipped 个，新引入 $regressed 个"
if [ -n "${GITHUB_OUTPUT:-}" ]; then
  {
    echo "checked=$checked"
    echo "skipped=$skipped"
    echo "regressed=$regressed"
  } >>"$GITHUB_OUTPUT"
fi

[ "$regressed" -eq 0 ] || exit 1
exit 0