@echo off
title andy-harness launcher

echo ==========================================
echo   andy-harness start all services
echo ==========================================
echo.

echo [1/2] Starting backend  - http://localhost:8000 ...
start "andy-harness-backend" cmd /k "cd /d %~dp0backend && .venv\Scripts\python.exe -m uvicorn harness.main:app --reload --host 0.0.0.0 --port 8000"

echo [2/2] Starting frontend - http://localhost:5173 ...
start "andy-harness-frontend" cmd /k "cd /d %~dp0frontend && pnpm dev"

echo.
echo Both services started in separate windows.
echo Close those windows to stop, or double-click stop-all.bat.
echo.
timeout /t 5 >nul
start http://localhost:5173
exit