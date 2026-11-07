@echo off
REM oracle-snatch-windows 启动入口
REM 先做本地自检（--check），配置没填好则不进入主循环，避免空转刷日志。
setlocal
cd /d "%~dp0"

set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

"%PY%" snatch.py --check --config "%~dp0config.env"
if errorlevel 1 (
    echo [%date% %time%] config check FAILED, not starting. >> "%~dp0snatch.log"
    exit /b 1
)

echo [%date% %time%] starting snatch loop. >> "%~dp0snatch.log"
"%PY%" -u snatch.py --config "%~dp0config.env" >> "%~dp0snatch.log" 2>&1
