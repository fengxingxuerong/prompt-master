#!/usr/bin/env bash
# 附件（extras/desktop.py）"在 CI 上到底能不能跑"的诊断。
#
# 2026-10-19：桌面窗口已移出主包（pm/desktop.py 只剩转发壳，测试在 tests_extras/），
# 主套件不再包含窗口层。这一步保留为**附件**的体检 —— 它不进主门禁，也不影响覆盖率。
#
# 为什么要有这一步：连续两轮 CI 只有 `Tests (pytest)` 一步红，而**为什么**红看不到——
# 日志正文要登录才读得到（匿名 API 403）。于是"红的原因是没跑"这个推断只能靠本机复现支撑，
# 没法在远端证真或证伪。这一步把三个决定性事实送到**匿名可读**的位置：
# job summary 与 annotation。
#
# 它自己不判红：真正的门禁是下一步的 pytest。这里只负责让下一次失败能一眼看出是
# "xvfb 不在" / "解释器没有 tkinter" / "有 X 也建不出窗口" / "窗口层真跑了但用例挂了"。
set -uo pipefail

HAS_XVFB=$(command -v xvfb-run >/dev/null 2>&1 && echo yes || echo no)
TK_VER=$(python -c 'import tkinter; print(tkinter.TkVersion)' 2>&1 | tail -1)
BARE=$(python -c 'import tkinter; tkinter.Tk(); print(1)' 2>&1 | tail -1)
WITHX=$(xvfb-run -a python -c 'import tkinter; tkinter.Tk(); print(1)' 2>&1 | tail -1)
COLLECT=$(python -m pytest tests_extras/test_desktop.py -q --collect-only 2>&1 | tail -1)

{
  echo "## 附件 extras/desktop.py 可跑性（诊断，不是门禁）"
  echo "- xvfb-run 在 PATH: $HAS_XVFB"
  echo "- tkinter 版本: $TK_VER"
  echo "- 裸建窗（无 X 时应当失败）: $BARE"
  echo "- xvfb 下建窗: $WITHX"
  echo "- 桌面用例收集: $COLLECT"
} >> "$GITHUB_STEP_SUMMARY"

echo "::notice::xvfb=$HAS_XVFB tk=$TK_VER bare=$BARE withx=$WITHX collect=$COLLECT"
