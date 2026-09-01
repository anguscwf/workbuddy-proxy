@echo off
REM PM2 / 计划任务用：在完整 CMD 环境下走 PATH（兼容 pyenv-win 的 python.bat）
chcp 65001 >nul 2>&1
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
python -u server.py
