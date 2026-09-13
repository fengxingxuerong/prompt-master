#!/usr/bin/env bash
# Docker 构建步骤的本机替代验证（开发机无 docker 时使用）。
#
# 为什么这样做：Dockerfile 里真正有风险的两步是 `pip install .`（依赖能否装全）
# 与入口能否起来 + `/api/health` 是否可答。把这两步在干净 venv 里原样跑一遍，
# 能覆盖构建失败与启动失败的主要成因；差的是镜像层缓存、uid/权限与真正的
# namespace 隔离——这三点只能在有 docker 的机器上验（见 docs/deploy_verification_2026-09-13.md）。
#
# 用法：
#   PYTHON="<python3.11+ 可执行文件>" bash examples/docker_step_sim.sh
#   （PYTHON 不传则用 PATH 里的 python3/python）
#
# 注意：Git Bash 下**必须用相对路径**调 Windows python —— MSYS 绝对路径（/d/...）
# 传进去会导致 `python -m venv` 静默建不出 venv（本脚本踩过，故统一 cd 到仓根用相对路径）。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [ -n "${PYTHON:-}" ]; then
  PY_BASE="$PYTHON"
elif command -v python3 >/dev/null 2>&1; then
  PY_BASE="python3"
else
  PY_BASE="python"
fi

SIM="logs/docker_sim_$(date +%H%M%S)"
APP="${SIM}/app"
PORT="${PORT:-8098}"

echo "== 0) 基础解释器 =="
"$PY_BASE" -V

echo "== 1) 复刻 Dockerfile 的 COPY 阶段 =="
mkdir -p "${APP}"
tar -cf - --exclude='__pycache__' pyproject.toml pm | tar -xf - -C "${APP}"
ls "${APP}"

echo "== 2) 干净 venv + pip install .（等价于镜像的 RUN pip install .）=="
"$PY_BASE" -m venv "${SIM}/venv"
# Git Bash 下对 Windows .exe 用 -x 判定会为假（可执行位不适用），只能按文件存在性判断
if [ -f "${SIM}/venv/Scripts/python.exe" ]; then
  VPY="${SIM}/venv/Scripts/python.exe"
else
  VPY="${SIM}/venv/bin/python"
fi
[ -f "$VPY" ] || { echo "  venv 创建失败：$VPY 不存在"; exit 1; }
# 不升级 pip：venv 自带的 pip 足够装本项目；升级会触发卸载旧 pip，
# 在某些受管环境（如带 safe-delete 守卫的宿主）会被拦成 SystemExit
"$VPY" -m pip --version
"$VPY" -m pip install "${APP}"

echo "== 3) 校验镜像里没有开发依赖（Dockerfile 的设计意图）=="
for m in pytest ruff mypy coverage; do
  if "$VPY" -c "import ${m}" 2>/dev/null; then
    echo "  ${m} 存在 —— 不符合预期"; exit 1
  else
    echo "  ${m} 不存在 ✓"
  fi
done

echo "== 4) 复刻运行时 COPY（入口脚本与用例模板在装包之后）=="
cp run.py run_server.py "${APP}/"
cp -r case_templates "${APP}/"

echo "== 5) 启动服务 + HEALTHCHECK 等价调用 =="
mkdir -p "${SIM}/data/logs"
PM_FAKE_BACKEND=progress PM_LOG_DIR="${SIM}/data/logs" PM_TASK_DB="${SIM}/data/tasks.db" \
  "$VPY" "${APP}/run_server.py" --host 127.0.0.1 --port "${PORT}" &
SRV=$!
trap 'kill ${SRV} 2>/dev/null || true' EXIT
sleep 6
"$VPY" -c "
import sys, urllib.request
r = urllib.request.urlopen('http://127.0.0.1:${PORT}/api/health', timeout=3)
print('  health:', r.status, r.read().decode())
sys.exit(0 if r.status == 200 else 1)
"

echo "== 6) 镜像内跑通一次任务闭环（演示后端，零模型调用）=="
"$VPY" -c "
import json, time, urllib.request
U = 'http://127.0.0.1:${PORT}'
def post(p, pl):
    req = urllib.request.Request(U+p, data=json.dumps(pl).encode(), headers={'Content-Type':'application/json'})
    return json.loads(urllib.request.urlopen(req, timeout=20).read())
def get(p):
    return json.loads(urllib.request.urlopen(U+p, timeout=20).read())
rid = post('/api/optimize', {'task': '让 AI 抽取销售数据中的城市与金额', 'n_test_cases': 2, 'max_iterations': 1})['run_id']
st = {}
for _ in range(60):
    time.sleep(0.5)
    st = get('/api/status/' + rid)
    if st.get('status') not in ('pending', 'running'):
        break
rep = get('/api/report/' + rid)
assert '## 评分总览' in rep['report'], '报告缺少评分总览'
print('  status:', st.get('status'), '| 报告字节:', len(rep['report']))
"

echo "== 7) 构建残留检查 =="
echo "  说明：以下残留是 pip install 的正常产物，镜像里由 Dockerfile 的"
echo "  'rm -rf build ./*.egg-info' 清掉；本模拟不执行该清理，仅展示残留形态。"
find "${APP}" -maxdepth 1 \( -name build -o -name '*.egg-info' \) -print | sed 's/^/  /' || true

echo "全部替代验证通过。模拟目录：${SIM}"
