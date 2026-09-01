@echo off
chcp 65001 >nul 2>&1
title Start WorkBuddy (Debug Mode)
REM 带 CDP 调试端口启动 WorkBuddy，供 workbuddy-proxy 提取 token
REM 用完 token 后正常托盘退出即可；下次启动继续用本脚本可保持 proxy 自动续期能力
start "" "C:\Users\anguscui\AppData\Local\Programs\WorkBuddy\WorkBuddy.exe" --remote-debugging-port=9222
echo WorkBuddy started with --remote-debugging-port=9222
timeout /t 3 >nul
