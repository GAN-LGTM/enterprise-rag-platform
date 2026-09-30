@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0"

if "%PORT%"=="" set PORT=8000
:: 默认只监听本机回环；需要局域网内其他电脑访问时，先执行 set HOST=0.0.0.0 再运行本脚本
if "%HOST%"=="" set HOST=127.0.0.1

set PY="%~dp0.venv\Scripts\python.exe"
if not exist "logs" mkdir "logs"

echo ==========================================================
echo   企业知识检索中台 — 启动程序
echo ==========================================================
echo.

REM ---------- 1. 运行环境 ----------
echo [1/4] 检查运行环境 ...
if not exist "%~dp0.venv\Scripts\python.exe" goto FIRST_INSTALL
echo       虚拟环境已就绪，跳过联网安装（离线可直接启动）
goto CHECK_PORT

:FIRST_INSTALL
echo       首次运行：正在创建虚拟环境 ...
python -m venv ".venv"
if errorlevel 1 (
    echo       [失败] 未找到 Python。请先安装 Python 3.10 以上版本并勾选 Add to PATH。
    pause
    exit /b 1
)
echo       首次运行：正在安装依赖（此步骤需要联网，仅第一次执行）...
"%PY%" -m pip install -q -r requirements.txt
if errorlevel 1 (
    echo       默认源不可达，改用官方源重试 ...
    "%PY%" -m pip install -q -r requirements.txt -i https://pypi.org/simple
)
if errorlevel 1 (
    echo       [失败] 依赖安装失败，请检查网络后重新运行本脚本。
    pause
    exit /b 1
)

REM ---------- 2. 端口占用（历史高危坑：旧实例会导致"改了不生效"） ----------
:CHECK_PORT
echo [2/4] 检查端口 %PORT% ...
set "OLDPID="
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":%PORT% " ^| findstr /i "LISTENING"') do set "OLDPID=%%p"
if defined OLDPID (
    echo       发现旧服务进程 PID=!OLDPID! 占用端口 %PORT%
    echo       若不结束旧进程，会出现"代码改了但界面毫无变化"的假象，因此自动结束它。
    taskkill /F /PID !OLDPID! >nul 2>&1
    timeout /t 2 >nul
)
echo       端口 %PORT% 可用

REM ---------- 3. 启动服务 ----------
echo [3/4] 启动后端服务（首次加载约 15 秒，日志：logs\server.log）...
set PYTHONPATH=%~dp0backend
set PYTHONUTF8=1
start "企业知识中台-服务（请勿关闭此窗口）" /min cmd /c ""%PY%" -m uvicorn app.main:app --host %HOST% --port %PORT% > "%~dp0logs\server.log" 2>&1"

REM ---------- 4. 健康检查 ----------
echo [4/4] 等待服务就绪 ...
set TRY=0
:WAIT
set /a TRY+=1
REM 注意：必须显式绕过系统代理，否则本机 127.0.0.1 请求可能被代理拦截返回 502，误判为"服务没起来"
"%PY%" -c "import urllib.request,sys;op=urllib.request.build_opener(urllib.request.ProxyHandler({}));sys.exit(0 if op.open('http://127.0.0.1:%PORT%/api/health',timeout=3).status==200 else 1)" >nul 2>&1
if not errorlevel 1 goto READY
if %TRY% GEQ 45 goto FAIL
timeout /t 2 >nul >nul
goto WAIT

:READY
echo.
echo ==========================================================
echo   启动成功！
echo   访问地址 : http://127.0.0.1:%PORT%/ui/
echo   接口文档 : http://127.0.0.1:%PORT%/docs
echo   日志文件 : logs\server.log
echo ==========================================================
echo   （服务在独立窗口后台运行，关闭服务请运行 stop.bat）
echo.
start "" http://127.0.0.1:%PORT%/ui/
echo   正在打开浏览器，同时执行一次系统自检：
echo.
"%PY%" scripts\selfcheck.py
echo.
pause
exit /b 0

:FAIL
echo.
echo   [失败] 服务在 90 秒内未就绪。请查看日志 logs\server.log 末尾的报错信息。
pause
exit /b 1
