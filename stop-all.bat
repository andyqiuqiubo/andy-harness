@echo off
title andy-harness stopper

echo ==========================================
echo   andy-harness stop all services
echo ==========================================
echo.
echo   Stops backend (port 8000) and frontend (port 5173).
echo   All built-in capabilities (Skills / permissions / todos /
echo   session export-import-fork / MCP client (stdio + SSE) /
echo   message actions / file transfer / output offload / long-term
echo   memory + auto summary / run tracing / subagent / scheduled
echo   tasks) run inside these two services; stopping them also
echo   terminates any MCP server subprocesses spawned.
echo.

set FOUND=0

echo Closing service windows ...
taskkill /FI "WINDOWTITLE eq andy-harness-backend*" /F /T >nul 2>&1
if not errorlevel 1 set FOUND=1
taskkill /FI "WINDOWTITLE eq andy-harness-frontend*" /F /T >nul 2>&1
if not errorlevel 1 set FOUND=1

echo.
echo Cleaning up by port ...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8000" ^| findstr "LISTENING"') do (
    set FOUND=1
    echo Stopping backend  [PID %%a, port 8000] ...
    taskkill /PID %%a /F >nul 2>&1
)

for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":5173" ^| findstr "LISTENING"') do (
    set FOUND=1
    echo Stopping frontend [PID %%a, port 5173] ...
    taskkill /PID %%a /F >nul 2>&1
)

if %FOUND%==0 (
    echo No running andy-harness service found.
) else (
    echo.
    echo All services stopped.
)

echo.
echo Done. All windows closed.

echo.
pause