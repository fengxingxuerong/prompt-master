@echo off
rem Aussie Lobster demo - one-shot start (uses sibling prompt-master venv or system python)
cd /d %~dp0
if exist "..\..\.venv\Scripts\python.exe" (
  "..\..\.venv\Scripts\python.exe" app.py --port 8791
) else (
  python app.py --port 8791
)
