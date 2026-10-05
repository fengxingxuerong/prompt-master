#!/usr/bin/env bash
# 通用 e2e 轮次启动器：日志落盘、支持端点环境变量覆盖、完成标志可 grep。
#
# 用法（在 Git Bash 里）：
#   bash scripts/run_round.sh 9
#   bash scripts/run_round.sh 9 --samples 2 --max-iter 2
#
# 换端点而不改 .env（pm/llm.py 的 load_dotenv() 默认 override=False，环境变量优先）：
#   PM_BASE_URL=https://token.sensenova.cn/v1 PM_MODEL=deepseek-v4-pro PM_API_KEY=sk-xxx \
#     bash scripts/run_round.sh 9
#
# ⚠️ 换端点时**不要**叠加 PM_FORCE_JSON_CHANNEL=1（第 9 轮踩坑）：
#   强制文本 JSON 通道是给"不支持原生结构化输出"的端点用的降级路径；
#   对支持原生 structured output 的端点（如 sensenova）反而更差 ——
#   第 9 轮首次尝试就这么崩的（clarify 3 次解析全败），见 logs/iter9_attempt1_stdout.log。
#
# ⚠️ 长跑进程存活（第 6/7/8 轮各踩一次）：
#   Agent 会话结束时，该轮启动的后台进程会被宿主回收。可行方式只有两种：
#     ① 用户自己在终端里跑本脚本（最稳）；
#     ② 宿主托管 run_in_background + 轮次内持续 sleep 陪跑（不结束回复）。
#   详见 skill: longrun-process-survival

set -u
cd "$(dirname "$0")/.." || exit 1
export PATH="/usr/bin:/bin:$PATH"

ROUND="${1:?用法: run_round.sh <轮次号> [--samples N] [--max-iter N]}"
shift || true

# 默认生产口径（用户未传参数时）
if [ $# -eq 0 ]; then
  set -- --samples 2 --max-iter 2
fi

# 真端点轮次必须串行（2026-10-04 实测教训，见 docs/evaluation.md §十七·七）：
# 多个会话各自起一轮会共用同一个 Key 池，噪声带被互相污染 —— 第六轮 50 次调用
# 就换到一个"不可引用"的读数。这把锁缺省关闭（不改产品并发），**launcher 负责打开它**：
# 保护的单位是"一轮"，不是"一次调用"。要故意做并发实验时才显式取消。
export PM_LIVE_LOCK="${PM_LIVE_LOCK:-1}"
if [ "${PM_LIVE_LOCK}" = "1" ]; then
  echo "串行锁：开（PM_LIVE_LOCK=1；已有别的轮次在跑时会立刻退 2 并点名对方 PID）"
  echo "        排队等：PM_LIVE_LOCK_WAIT=<秒>（默认 0 = 不等）"
fi

PY=./.venv/Scripts/python.exe
OUT="logs/iter${ROUND}_report.md"
STDOUT="logs/iter${ROUND}_stdout.log"

echo "=== 第 ${ROUND} 轮启动 $(date '+%F %T') ==="
echo "任务：销售数据分析（数据缺失须显式标注）"
echo "参数：$*"
if [ -n "${PM_BASE_URL:-}" ]; then
  echo "⚠️  端点已被环境变量覆盖：PM_BASE_URL=${PM_BASE_URL} PM_MODEL=${PM_MODEL:-<未设>}"
fi
echo "日志：$STDOUT"
echo

PYTHONUNBUFFERED=1 "$PY" run.py \
  --task "写一个提示词，让 AI 分析销售数据并给出趋势结论与异常点；数据不足或缺失时必须显式标注「数据缺失」，不得编造" \
  --target-model "${PM_MODEL:-DeepSeek-V4-Flash}" \
  --cases-file case_templates/sales_analysis.json \
  "$@" \
  --out "$OUT" 2>&1 | tee "$STDOUT"

echo
echo "=== 完成 $(date '+%F %T') ==="
echo "报告：$OUT"
grep -E "完整运行日志|报告已保存" "$STDOUT" | tail -3
