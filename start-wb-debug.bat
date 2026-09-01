@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul 2>&1
title Start WorkBuddy with Local CDP
cd /d "%~dp0"

set "WB_CDP_PORT_ARG=%~1"
if defined WB_CDP_PORT_ARG set "WB_CDP_PORT=!WB_CDP_PORT_ARG!"
if not defined WB_CDP_PORT set "WB_CDP_PORT=%WB_DEBUG_PORT%"
if not defined WB_CDP_PORT set "WB_CDP_PORT=9222"

powershell.exe -NoProfile -Command "$p=$env:WB_CDP_PORT; $n=0; if ($p -notmatch '^[0-9]{1,5}$' -or -not [int]::TryParse($p,[ref]$n) -or $n -lt 1 -or $n -gt 65535) { exit 1 }" >nul 2>&1
if errorlevel 1 (
    echo [ERROR] 端口必须在 1-65535 范围内
    exit /b 1
)

set "WB_RUNNING="
tasklist /FI "IMAGENAME eq WorkBuddy.exe" /FO CSV /NH 2>nul | findstr /I /C:"WorkBuddy.exe" >nul
if not errorlevel 1 set "WB_RUNNING=1"

call :check_cdp
if not errorlevel 1 if defined WB_RUNNING (
    echo [OK] WorkBuddy 调试端口 %WB_CDP_PORT% 已就绪
    exit /b 0
)
if not errorlevel 1 (
    echo [ERROR] 端口 %WB_CDP_PORT% 上有 workbench，但本机没有 WorkBuddy 进程
    echo         为防止连接其他应用，本脚本拒绝继续
    exit /b 3
)

if defined WB_RUNNING (
    echo [ERROR] WorkBuddy 已在运行，但端口 %WB_CDP_PORT% 没有可用的 workbench
    echo         请先保存工作并从系统托盘手动退出 WorkBuddy，再重新运行本脚本
    echo         本脚本不会结束或重启 WorkBuddy
    exit /b 2
)

netstat -ano -p TCP 2>nul | findstr /R /C:":%WB_CDP_PORT% .*LISTENING" >nul
if not errorlevel 1 (
    echo [ERROR] 端口 %WB_CDP_PORT% 已被其他程序占用
    echo         可改用其他端口，例如: start-wb-debug.bat 9223
    echo         同时将 CDP_URL 和 extract_token.py --port 改成同一端口
    exit /b 3
)

set "WB_BIN="
if defined WB_EXE if not exist "%WB_EXE%" (
    echo [ERROR] WB_EXE 指向的文件不存在: %WB_EXE%
    exit /b 4
)
if defined WB_EXE set "WB_BIN=%WB_EXE%"
if not defined WB_BIN if exist "%LOCALAPPDATA%\Programs\WorkBuddy\WorkBuddy.exe" set "WB_BIN=%LOCALAPPDATA%\Programs\WorkBuddy\WorkBuddy.exe"
if not defined WB_BIN if exist "%LOCALAPPDATA%\WorkBuddy\WorkBuddy.exe" set "WB_BIN=%LOCALAPPDATA%\WorkBuddy\WorkBuddy.exe"
if not defined WB_BIN if exist "%ProgramW6432%\WorkBuddy\WorkBuddy.exe" set "WB_BIN=%ProgramW6432%\WorkBuddy\WorkBuddy.exe"
if not defined WB_BIN if exist "%ProgramFiles%\WorkBuddy\WorkBuddy.exe" set "WB_BIN=%ProgramFiles%\WorkBuddy\WorkBuddy.exe"
if not defined WB_BIN if exist "%ProgramFiles(x86)%\WorkBuddy\WorkBuddy.exe" set "WB_BIN=%ProgramFiles(x86)%\WorkBuddy\WorkBuddy.exe"
if not defined WB_BIN for /f "delims=" %%I in ('where WorkBuddy.exe 2^>nul') do if not defined WB_BIN set "WB_BIN=%%I"

if not defined WB_BIN (
    echo [ERROR] 未找到 WorkBuddy.exe
    echo         可先设置 WB_EXE 为 WorkBuddy.exe 的完整路径后重试
    exit /b 4
)

echo [INFO] 正在以本机调试端口 %WB_CDP_PORT% 启动 WorkBuddy...
start "" "%WB_BIN%" --remote-debugging-address=127.0.0.1 --remote-debugging-port=%WB_CDP_PORT%

for /L %%I in (1,1,60) do (
    timeout /t 1 /nobreak >nul
    call :check_cdp
    if not errorlevel 1 goto :ready
)

echo [ERROR] WorkBuddy 已启动，但 60 秒内未发现 workbench 调试目标
echo         请确认 WorkBuddy 已登录，并检查端口是否与 CDP_URL 一致
exit /b 5

:ready
echo [OK] WorkBuddy 调试端口已就绪: 127.0.0.1:%WB_CDP_PORT%
echo      下一步: python extract_token.py --port %WB_CDP_PORT% --save
exit /b 0

:check_cdp
powershell.exe -NoProfile -Command "$ErrorActionPreference='Stop'; $targets=Invoke-RestMethod -Uri 'http://127.0.0.1:%WB_CDP_PORT%/json' -TimeoutSec 2; $hasTarget=@($targets ^| Where-Object { $_.type -eq 'page' -and $_.url -match 'workbench' -and $_.webSocketDebuggerUrl }).Count -gt 0; $owned=$false; foreach($listener in @(Get-NetTCPConnection -State Listen -LocalPort %WB_CDP_PORT% -ErrorAction Stop)){ try { $process=Get-Process -Id $listener.OwningProcess -ErrorAction Stop; if($process.ProcessName -like 'WorkBuddy*'){ $owned=$true; break } } catch {} }; if($hasTarget -and $owned){ exit 0 }; exit 1" >nul 2>&1
exit /b %errorlevel%
