@echo off
title andy-harness launcher

echo ==========================================
echo   andy-harness start all services
echo ==========================================
echo.
echo   Capabilities: Skills system, tool-permission + human-in-the-loop,
echo   planning/todo tracking, session export/import/fork, MCP client
echo   (stdio + remote SSE), message actions, file transfer (docs+images),
echo   artifacts offload, long-term memory (+auto summary), run tracing,
echo   subagent, scheduled tasks (MCP/Skills/tools scoped).
echo   (All features are auto-loaded plugins; the SQLite schema
echo    upgrades itself on first launch -- no manual migration needed.)
echo.
echo   MCP: drop a mcp.json next to this script (key "mcpServers") or set
echo   MCP_CONFIG_PATH to auto-connect external MCP servers. Manage them
echo   in Settings -^> MCP tab.
echo.

echo Checking dependencies ...
if not exist "%~dp0backend\.venv\Scripts\python.exe" (
    echo ERROR: backend virtual environment not found.
    echo   Fix:  cd backend ^&^& uv sync --extra dev
    echo   Then re-run start-all.bat
    echo.
    pause
    exit /b 1
)
if not exist "%~dp0frontend\node_modules" (
    echo ERROR: frontend dependencies not installed.
    echo   Fix:  cd frontend ^&^& pnpm install
    echo   Then re-run start-all.bat
    echo.
    pause
    exit /b 1
)
rem File-transfer (multipart upload) needs python-multipart in the venv.
"%~dp0backend\.venv\Scripts\python.exe" -c "import multipart" >nul 2>&1
if errorlevel 1 (
    echo ERROR: python-multipart missing -- file upload would fail.
    echo   Fix:  cd backend ^&^& uv sync --extra dev
    echo   Then re-run start-all.bat
    echo.
    pause
    exit /b 1
)
echo Dependencies OK.
echo.

echo Checking ports ...
netstat -ano | findstr ":8000" | findstr "LISTENING" >nul 2>&1
if not errorlevel 1 goto port8000busy
netstat -ano | findstr ":5173" | findstr "LISTENING" >nul 2>&1
if not errorlevel 1 goto port5173busy
echo Ports are free. Starting services ...
echo.

echo [1/2] Starting backend  - http://localhost:8000 ...
start "andy-harness-backend" cmd /k "cd /d %~dp0backend && .venv\Scripts\python.exe -m uvicorn harness.main:app --reload --host 0.0.0.0 --port 8000"

echo Waiting for backend to be ready on port 8000 ...
set /a tries=0
:wait_backend
timeout /t 1 /nobreak >nul
netstat -ano | findstr ":8000" | findstr "LISTENING" >nul 2>&1
if not errorlevel 1 goto backend_ready
set /a tries+=1
if %tries% lss 60 goto wait_backend
echo WARN: backend did not become ready in 60s, continuing anyway.
:backend_ready

echo [2/2] Starting frontend - http://localhost:5173 ...
start "andy-harness-frontend" cmd /k "cd /d %~dp0frontend && pnpm dev"

echo Waiting for frontend to be ready on port 5173 ...
set /a tries=0
:wait_frontend
timeout /t 1 /nobreak >nul
netstat -ano | findstr ":5173" | findstr "LISTENING" >nul 2>&1
if not errorlevel 1 goto frontend_ready
set /a tries+=1
if %tries% lss 60 goto wait_frontend
echo WARN: frontend did not become ready in 60s, continuing anyway.
:frontend_ready

echo.
echo Both services started in separate windows.
echo Close those windows to stop, or double-click stop-all.bat.
echo.
start http://localhost:5173
exit

:port8000busy
echo ERROR: Port 8000 is already in use.
echo Stop the running backend first [double-click stop-all.bat].
echo.
pause
exit /b 1

:port5173busy
echo ERROR: Port 5173 is already in use.
echo Stop the running frontend first [double-click stop-all.bat].
echo.
pause
exit /b 1
