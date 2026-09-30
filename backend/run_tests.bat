@echo off
REM 企业级测试与覆盖率报告入口（Windows）
REM 用法：双击或在 cmd 中运行 backend\run_tests.bat
cd /d %~dp0
set PYTHONPATH=%CD%

set ROOT=%CD%\..
set COV=%ROOT%\.venv\Scripts\coverage.exe

%COV% erase
if exist .coverage.* del /q .coverage.*

echo ===== 1) 纯逻辑单元测试（进程内覆盖率）=====
%COV% run -p -m pytest tests/test_unit.py -q

echo.
echo ===== 2) 12 个端到端功能脚本（并行模式落盘后合并）=====
for %%s in (scripts\test_*.py) do (
  echo ---- %%s ----
  %COV% run -p "%%s"
)

echo.
echo ===== 3) 合并覆盖率并汇总 =====
%COV% combine
%COV% report --show-missing
%COV% json -o coverage.json
echo DONE -^> backend/coverage.json
pause
