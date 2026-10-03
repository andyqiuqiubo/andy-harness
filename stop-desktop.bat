@echo off
title andy-harness desktop stopper

echo ==========================================
echo   andy-harness stop desktop shell (Tauri)
echo ==========================================
echo.
echo   Stops the Tauri dev shell (Vite + Rust) and the backend
echo   process tree it started (launcher + uvicorn + MCP servers).
echo   The desktop backend port is assigned dynamically, so the
echo   backend is matched by command line instead of by port;
echo   start-all.bat services (ports 8000 / 5173) are only touched
echo   when the desktop shell itself owns port 5173.
echo.

set FOUND=0

echo Closing the desktop shell window ...
taskkill /FI "WINDOWTITLE eq andy-harness-desktop*" /F /T >nul 2>&1
if not errorlevel 1 set FOUND=1

echo Closing the Tauri app process ...
taskkill /IM andy-harness-gy.exe /F /T >nul 2>&1
if not errorlevel 1 set FOUND=1

echo Cleaning up the desktop backend [launcher / loopback uvicorn] ...
rem Only python processes are matched: the helper cmd/powershell running this
rem filter also carries these strings in its command line, so name filtering
rem keeps the script from killing its own process tree.
for /f "usebackq" %%p in (`powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -like 'python*' -and $_.CommandLine -ne $null -and ($_.CommandLine -like '*launch-backend.py*' -or ($_.CommandLine -like '*uvicorn*' -and $_.CommandLine -like '*harness.main*' -and $_.CommandLine -like '*127.0.0.1*')) } | ForEach-Object { $_.ProcessId }"`) do (
    set FOUND=1
    echo Stopping backend [PID %%p] ...
    taskkill /PID %%p /F /T >nul 2>&1
)

echo.
echo Cleaning up Vite on port 5173 ...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":5173" ^| findstr "LISTENING"') do (
    set FOUND=1
    echo Stopping Vite [PID %%a, port 5173] ...
    taskkill /PID %%a /F >nul 2>&1
)

if %FOUND%==0 (
    echo No running andy-harness desktop shell found.
) else (
    echo.
    echo Desktop shell stopped.
)

echo.
echo Done. All windows closed.
echo.
exit /b 0
