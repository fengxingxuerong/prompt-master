#!/usr/bin/env bash
# CI 的 pytest 包装：跑完把**失败用例名**发到 annotation，退出码原样透传。
#
# 为什么要这层包装：GitHub 的日志正文要登录才读得到（匿名 API 403），于是"CI 红了"这件事
# 在没有登录态的机器上只能靠推断。前一轮装了诊断步，问出"xvfb 有、tkinter 有、窗口建得起来"，
# 也就此推翻了我自己上一轮的归因（红已不再是"窗口层没跑"，而是"跑了并且挂了"）。
# 这一步把剩下的猜测也变成事实：到底是哪几条用例红。
#
# 它不改判据：地板、退出码、用例集合都和裸 pytest 一致；只是把结果搬到匿名可读的位置。
set -uo pipefail

OUT=/tmp/pm-pytest.log
if command -v xvfb-run >/dev/null 2>&1; then
  xvfb-run -a python -m pytest "$@" 2>&1 | tee "$OUT" | tail -30
else
  # 没 xvfb 也不能让门禁整个不跑：退化成裸 pytest（窗口层会 skip），并把这件事喊出来。
  echo "::warning::xvfb-run 不在 PATH —— 退回裸 pytest，桌面窗口层将被 skip"
  python -m pytest "$@" 2>&1 | tee "$OUT" | tail -30
fi
rc=${PIPESTATUS[0]}

if [ "$rc" -ne 0 ]; then
  MAPFILE_OK=$(grep -a -E '^(FAILED|ERROR)' "$OUT" | head -10)
  if [ -n "$MAPFILE_OK" ]; then
    # 逐条发出去：annotation 列表能直接读，不用登录看日志
    echo "$MAPFILE_OK" | while IFS= read -r line; do
      echo "::error::pytest 失败用例：${line}"
    done
  fi
  FLOOR=$(grep -a "Required test coverage" "$OUT" | tail -1)
  if [ -n "$FLOOR" ]; then
    echo "::error::覆盖率地板：${FLOOR}"
  fi
  if [ -z "$MAPFILE_OK" ] && [ -z "$FLOOR" ]; then
    echo "::error::pytest rc=$rc，日志里没有 FAILED/ERROR/覆盖率行——把最后几行原样带出：$(tail -3 "$OUT" | tr '\n' ' ' | tr ':' ' ')"
  fi
fi

exit "$rc"
