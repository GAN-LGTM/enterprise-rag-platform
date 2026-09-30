@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0"

if "%PORT%"=="" set PORT=8000

echo ==========================================================
echo   企业知识检索中台 — 停止服务
echo ==========================================================
echo.

set "FOUND=0"
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":%PORT% " ^| findstr /i "LISTENING"') do (
    set "PID=%%p"
    set "FOUND=1"
    echo 发现占用端口 %PORT% 的进程 PID=!PID!，正在结束 ...
    taskkill /F /PID !PID! >nul 2>&1
)

if "%FOUND%"=="0" (
    echo 端口 %PORT% 上没有运行中的服务，无需停止。
) else (
    echo 服务已停止。
)
echo.
echo 如需重新启动，请运行 start.bat
echo.
pause
