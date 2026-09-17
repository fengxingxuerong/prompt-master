#!/usr/bin/env bash
# 本地端到端联调：用 OpenAI 兼容桩服务跑通真实 HTTP 链路。
#
# 与 tests/ 中的 fake backend 的区别：
#   - fake backend 替换 Python 函数，只验证图拓扑
#   - 本脚本走 真实 HTTP：ChatOpenAI 客户端 → 网络请求 → 响应解析 → Pydantic 校验
#
# 覆盖三条结构化输出通道：
#   场景1：端点"不配合"（返回带围栏的 JSON）→ 验证自动降级与重试
#   场景2：端点支持 response_format     → 验证原生 json_schema 通道
#   场景3：端点支持 tools + PM_STRUCT_METHOD=function_calling
#                                   → 验证 function-calling 通道（A10：
#     langchain-openai 默认走 json_schema，请求体不带 tools，桩的 --tools 分支
#     原本是死码；必须显式指定 method 才能真正覆盖这条通道）
#
# 用法：bash examples/run_e2e_stub.sh
# 可用环境变量覆盖：PY / STUB_PY / PORT
#（Windows 请用等价脚本 examples/run_e2e_stub.ps1：额外做了端口避让与 UTF-8 处理）


set -u
cd "$(dirname "$0")/.."

PY="${PY:-.venv/bin/python}"
STUB_PY="${STUB_PY:-$PY}"   # 桩服务只需要 fastapi/uvicorn，与运行时同一 venv 即可
PORT="${PORT:-8130}"
STUB_BASE="http://127.0.0.1:${PORT}/v1"

# 必须把**每一个角色**的端点都指到桩。
# pm/llm.py 优先读 PM_<ROLE>_BASE_URL，而 load_dotenv() 会把仓库 .env 里的
# PM_EVALUATOR_B_BASE_URL / PM_ARBITER_BASE_URL 注回来 —— 只设全局 PM_BASE_URL 的话，
# "本地联调"会静默地真去打外部付费端点，结果也不复现。
# 先在环境里把同名变量置好：load_dotenv 默认不覆盖已存在的值，就此堵住。
COMMON_ENV=(
  PM_API_KEY=stub-key
  PM_BASE_URL="$STUB_BASE"
  PM_MODEL=stub-model
  PM_TARGET_MODEL=stub-model
)
for role in CLARIFIER OPTIMIZER MOCKGEN EVALUATOR EVALUATOR_B ARBITER REVISER TARGET; do
  COMMON_ENV+=(
    "PM_${role}_API_KEY=stub-key"
    "PM_${role}_BASE_URL=$STUB_BASE"
    "PM_${role}_MODEL=stub-model"
  )
done

# 每个场景用独立缓存/产物目录：否则第二次跑会全部命中缓存，
# 真实 HTTP 链路被跳过，grep 出来的证据是空的（脚本却照样 0 退出）。
WORK_DIR="$(mktemp -d)"
COMMON_ENV+=( PM_CACHE_DIR="$WORK_DIR" PM_LOG_DIR="$WORK_DIR" )

# 桩评委固定给 8 分上下：把达标阈值调高，流程才会真的走到
# revise / 早停 / 迭代上限兜底 这些分支（默认 8.0 时首轮就 passed，这些路径 0 覆盖）。
COMMON_ENV+=( PM_PASS_THRESHOLD=9.5 )
# 桩 e2e 验的是“客户端 → HTTP → 解析 → 校验”链路，不是统计置信度：采样固定 1。
# 但基线与成对盲评保持开启——两个新节点同样需要真实 HTTP 覆盖。
COMMON_ENV+=( PM_SAMPLES_PER_CASE=1 PM_BASELINE=1 PM_PAIRWISE=1 )

PID=""
cleanup() {
  [ -n "${PID:-}" ] && kill "$PID" 2>/dev/null
  rm -rf "$WORK_DIR" 2>/dev/null
}
trap cleanup EXIT

assert_all_local() {
  # 预检：8 个角色的 base_url 必须全是本地桩，否则立刻中止，绝不往外发请求
  local bad
  bad=$(env "${COMMON_ENV[@]}" "$PY" -c '
from pm.llm import build_config
roles = ("clarifier","optimizer","mockgen","evaluator","evaluator_b","arbiter","reviser","target")
bad = [r for r in roles if not (build_config(r).base_url or "").startswith("http://127.0.0.1:")]
print("ALL_LOCAL" if not bad else "LEAK:" + ",".join(bad))
')
  if [ "$bad" != "ALL_LOCAL" ]; then
    echo "错误：端点预检未通过（$bad）—— 仍有角色指向非本地端点，已中止" >&2
    exit 1
  fi
  echo "端点预检：8 个角色全部指向本地桩 ✔"
}

FAILED=0

run_case() {
  local label="$1"; shift
  # 额外的每场景环境变量（如 PM_STRUCT_METHOD）以 KEY=VALUE 形式追加在标签之后
  # （`${arr[@]+...}` 写法兼容 bash 3.2：set -u 下空数组直接展开会报 unbound variable）
  local extra_env=()
  if [ "$#" -gt 0 ]; then extra_env=("$@"); fi
  echo ""
  echo "============================================================"
  echo "$label"
  echo "============================================================"
  local log="$WORK_DIR/run_case.log"
  env "${COMMON_ENV[@]}" ${extra_env[@]+"${extra_env[@]}"} "$PY" run.py --task "帮我写个 prompt 让 AI 分析销售数据" \
      --target-model stub-model --cases 3 --max-iter 2 > "$log" 2>&1
  local code=$?
  grep -E "run_id|^- 状态|^- 迭代|^- LLM|评分|平均分|通过用例|判定|用例数|基线|成对盲评|置信度|channel|降级|RuntimeError" \
    "$log" | head -20

  # 不能只看退出码：节点被静默跳过、比较全部失败时 run.py 仍然返 0
  # 退出码协议（feat cli 起）：0=达标、1=未达标但已交付——桩端点的评分不代表
  # 真实水位，e2e 验证链路（channel + 报告完整），两者都算跑通；2/3 才是真失败。
  if [ $code -gt 1 ]; then
    echo "✗ run.py 退出码 $code" >&2
    FAILED=1
  fi
  if grep -qE "结构化输出失败|Traceback|RuntimeError" "$log"; then
    echo "✗ 日志里有硬失败（结构化输出重试耗尽或抛异常）" >&2
    FAILED=1
  fi
  local sec
  for sec in "与基线对比" "成对盲评" "置信度与采样噪声"; do
    if ! grep -q "$sec" "$log"; then
      echo "✗ 报告缺少测量层小节：$sec" >&2
      FAILED=1
    fi
  done
}

assert_all_local

echo "启动桩服务（模型不遵守 JSON 模式）..."
$STUB_PY examples/openai_stub_server.py --port "$PORT" --uncooperative > "$WORK_DIR/stub_plain.log" 2>&1 &
PID=$!
sleep 4
run_case "场景 1：端点返回带围栏的 JSON —— 应自动降级为文本解析（channel=json_fallback）"
kill "$PID" 2>/dev/null; PID=""

sleep 1

echo ""
echo "启动桩服务（支持 response_format）..."
$STUB_PY examples/openai_stub_server.py --port "$PORT" --tools > "$WORK_DIR/stub_tools.log" 2>&1 &
PID=$!
sleep 4
run_case "场景 2：端点支持结构化输出 —— 应走原生 json_schema 通道（channel=structured_output，无降级告警）"
kill "$PID" 2>/dev/null; PID=""

sleep 1

echo ""
echo "启动桩服务（支持 function calling）..."
$STUB_PY examples/openai_stub_server.py --port "$PORT" --tools > "$WORK_DIR/stub_fc.log" 2>&1 &
PID=$!
sleep 4
run_case "场景 3：PM_STRUCT_METHOD=function_calling —— 应走 function-calling 通道（channel=function_calling，请求体带 tools）" \
  "PM_STRUCT_METHOD=function_calling"
kill "$PID" 2>/dev/null; PID=""

echo ""
if [ "$FAILED" -ne 0 ]; then
  echo "存在失败项，完整日志见 $WORK_DIR" >&2
  exit 1
fi
echo "完成：3 个场景全部通过（含基线与成对盲评的真实 HTTP 覆盖）。"
