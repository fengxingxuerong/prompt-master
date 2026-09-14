#!/usr/bin/env bash
# 第 8 轮：验证"解析失败升温重试"修复（对照第 7 轮同口径：2 采样 / max-iter 2）
#
# 为什么要有这个脚本：Agent 会话内用 & 拉起的进程会被宿主回收（第 6/7/8 轮各踩一次），
# 长跑轮次必须由用户在**自己的终端**里启动，进程才不会被回收。
#
# 用法（在 Git Bash 里）：
#   cd /d/projects/prompt-master && bash scripts/run_iter8.sh
# 或双击 scripts/run_iter8.cmd

set -u
cd "$(dirname "$0")/.." || exit 1
export PATH="/usr/bin:/bin:$PATH"

PY=./.venv/Scripts/python.exe
OUT=logs/iter8_report.md
STDOUT=logs/iter8_stdout.log

echo "=== 第 8 轮启动 $(date '+%F %T') ==="
echo "任务：销售数据分析（数据缺失须显式标注）"
echo "口径：--samples 2 --max-iter 2（与第 7 轮一致）"
echo "日志：$STDOUT"
echo

$PY run.py \
  --task "写一个提示词，让 AI 分析销售数据并给出趋势结论与异常点；数据不足或缺失时必须显式标注「数据缺失」，不得编造" \
  --target-model "DeepSeek-V4-Flash" \
  --cases-file case_templates/sales_analysis.json \
  --samples 2 \
  --max-iter 2 \
  --out "$OUT" 2>&1 | tee "$STDOUT"

echo
echo "=== 完成 $(date '+%F %T') ==="
echo "报告：$OUT"
grep -E "完整运行日志|报告已保存" "$STDOUT" | tail -3
