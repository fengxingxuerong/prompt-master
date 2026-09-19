#!/usr/bin/env bash
# 2026-09-18 基准重建：测量口径变了（用例只由需求生成 / 校验码归代码 / 评委跨家族 /
# 编造类锚点下移），docs 里 09-13 与 09-16 两份矩阵的 Δ 从此不可比，需重跑一版。
#
#   bash scripts/run_matrix_20260918.sh          # 跑全部三点
#   bash scripts/run_matrix_20260918.sh 2        # 只跑第 2 点
#
# 可断点续跑：已有 logs/matrix18_<id>_report.md 的点直接跳过（目标输出与评估缓存也让
# 重跑只补未覆盖的部分）。被宿主回收后重新执行本脚本即可接着跑。
#
# ⚠️ 不要在脚本运行期间编辑本文件：bash 是按字节偏移流式读取脚本的，
# 中途改写会让它的读取位置落到新内容的中间，报 "syntax error near unexpected token"
# 并静默丢掉后面的点（2026-09-18 就这么丢过第 3 点）。要改先停链。
#
# 成本口径：--samples 2 --max-iter 1，三点合计约 60~75 次调用；
# .env 里 PM_TARGET_MAX_CONCURRENCY=1（429 频繁），所以慢是预期内的。
set -u
cd "$(dirname "$0")/.." || exit 1
export PATH="/usr/bin:/bin:$PATH"
PY=./.venv/Scripts/python.exe
ONLY="${1:-all}"
SALES_TASK="写一个提示词，让 AI 分析销售数据并给出趋势结论与异常点；数据不足或缺失时必须显式标注「数据缺失」，不得编造"
TRIAGE_TASK="写一个提示词，让 AI 对客服工单做根因分类并给出处置建议；信息不足时必须先列出缺失项，不得猜测"

run_point() {
  local id="$1" name="$2" task="$3"
  shift 3
  if [ "$ONLY" != "all" ] && [ "$ONLY" != "$id" ]; then return 0; fi
  rep="logs/matrix18_${name}_report.md"
  if [ -f "$rep" ]; then
    # 只有"跑成了"的报告才算完成：status=failed 的报告（端点抖动让 clarify 三次解析失败
    # 那种）必须允许重跑，否则断点续跑会把一份 854 字节的空壳当成已覆盖，静默留下缺格。
    if grep -q '状态：`failed`' "$rep"; then
      echo "=== [$id] ${name}：已有报告但是 failed，重跑 ==="
      mv "$rep" "${rep%.md}.failed-$(date +%H%M).md"
    else
      echo "=== [$id] ${name}：已有报告，跳过（要重跑先删它）==="
      return 0
    fi
  fi
  echo "=== [$id] ${name} 启动 $(date '+%F %T') ==="
  PYTHONUNBUFFERED=1 "$PY" run.py --task "$task" "$@" \
    --samples 2 --max-iter 1 --out "$rep" \
    > "logs/matrix18_${name}_stdout.log" 2>&1
  rc=$?
  # 退出码必须紧跟命令取：`echo "... $?"` 取到的是 echo 自己的状态，跑失败也看不出来
  echo "=== [$id] ${name} 完成 $(date '+%F %T')，run.py 退出码 $rc ==="
  [ "$rc" -eq 0 ] || tail -5 "logs/matrix18_${name}_stdout.log"
}

# A：新任务域 + 种子用例（历史第 2、5 轮的误杀教训是否复现）
run_point 1 triage_seed "$TRIAGE_TASK" \
  --cases-file case_templates/support_triage.json

# B：mockgen 现场生成用例 —— 判据独立性与代码校验码后的注入存活链路
run_point 2 sales_mockgen "$SALES_TASK"

# C：rule 断言模式 —— 语义规则交给评委核验的链路（默认只提醒不否决）
run_point 3 sales_rule "$SALES_TASK" \
  --cases-file case_templates/sales_analysis.json --assert-mode rule

echo "=== 全部完成 $(date '+%F %T') ==="
ls -1 logs/matrix18_*_report.md 2>/dev/null
