@echo off
REM 第 8 轮启动器（双击即可）——在自己的终端里跑，进程不会被宿主回收
cd /d "%~dp0.."
"C:\Program Files\Git\bin\bash.exe" scripts/run_iter8.sh
pause
