@echo off
chcp 65001 >nul 2>&1
title WorkBuddy Proxy - Setup
cd /d "%~dp0"

echo ==================================================
echo   WorkBuddy Proxy - 环境配置
echo ==================================================
echo.

:: ---- Step 1: Check Python ----
echo [1/3] 检查 Python 环境...
where python >nul 2>&1
if %errorlevel% neq 0 (
    echo.
    echo [ERROR] 未找到 Python！
    echo.
    echo 请按以下步骤安装:
    echo   1. 访问 https://www.python.org/downloads/
    echo   2. 下载 Python 3.10 或更高版本
    echo   3. 安装时务必勾选 "Add Python to PATH"
    echo   4. 安装完成后重新运行此脚本
    echo.
    pause
    exit /b 1
)
for /f "tokens=2 delims= " %%v in ('python --version 2^>^&1') do set PYVER=%%v
echo       Python %PYVER% - OK

:: ---- Step 2: Install dependencies ----
echo.
echo [2/3] 安装 Python 依赖...
python -m pip install -r requirements.txt -q
if %errorlevel% neq 0 (
    echo [ERROR] 依赖安装失败
    echo        如果是网络问题，可尝试使用镜像源:
    echo        python -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
    pause
    exit /b 1
)
echo       依赖安装完成 - OK

:: ---- Step 3: Extract token ----
echo.
echo [3/3] 提取 WorkBuddy Token...

if exist "data\token.json" (
    echo       已存在 data\token.json，跳过提取
    echo       如需重新提取，请删除 data\token.json 后重新运行
    goto :done
)

echo.
echo 请确认以下步骤已完成:
echo   1. WorkBuddy 已安装并至少登录过一次
echo   2. 如 WorkBuddy 已运行，请先保存工作并从系统托盘手动退出
echo   3. WorkBuddy 已用调试模式启动:
echo.
echo      推荐: 双击 start-wb-debug.bat
echo      脚本不会结束或重启正在运行的 WorkBuddy
echo.

set /p READY="WorkBuddy 已用调试模式启动了吗? (y/n): "
if /i not "%READY%"=="y" (
    echo.
    echo [INFO] 已跳过 Token 提取，请稍后手动运行:
    echo        python extract_token.py --save
    goto :done
)

python extract_token.py --save
if %errorlevel% neq 0 (
    echo.
    echo [WARN] Token 提取失败，可能的原因:
    echo        - WorkBuddy 未用 --remote-debugging-port=9222 启动
    echo        - 端口 9222 被其他程序占用
    echo        - WorkBuddy 未登录
    echo.
    echo        可稍后手动运行: python extract_token.py --save
) else (
    echo       Token 提取成功 - OK
)

:done
echo.
echo ==================================================
echo   配置完成！启动服务:
echo     方式 1: 双击 start.bat
echo     方式 2: 运行 python server.py
echo     方式 3: docker compose up -d (需 Docker)
echo ==================================================
echo.
pause
