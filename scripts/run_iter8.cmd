@echo off
REM ---------------------------------------------------------------------------
REM 第 8 轮启动器（双击即可）——在自己的终端里跑，进程不会被宿主回收
REM
REM 背景：Agent 会话内用 & 拉起的进程会被宿主回收（第 6/7/8 轮各踩一次）。
REM       Git Bash 无 setsid；PowerShell Start-Process 在沙箱内被静默拦截（0 字节日志 + 无进程）。
REM       实测有效：宿主托管的 run_in_background（Bash 工具 run_in_background=true），
REM                 或用户自己在终端里直接跑本脚本。
REM ---------------------------------------------------------------------------
cd /d "%~dp0.."
echo === 第 8 轮启动 %DATE% %TIME% ===
echo 口径：--samples 2 --max-iter 2（与第 7 轮一致）
echo 日志：logs\iter8_stdout.log
echo.
if exist .venv\Scripts\python.exe (
  .venv\Scripts\python.exe run.py ^
    --task "写一个提示词，让 AI 分析销售数据并给出趋势结论与异常点；数据不足或缺失时必须显式标注「数据缺失」，不得编造" ^
    --target-model "DeepSeek-V4-Flash" ^
    --cases-file case_templates/sales_analysis.json ^
    --samples 2 ^
    --max-iter 2 ^
    --out logs\iter8_report.md 2>&1 | tee logs\iter8_stdout.log
) else (
  echo [错误] 找不到 .venv\Scripts\python.exe
)
echo.
echo === 完成 %DATE% %TIME% ===
pause
