@echo off
title andy-harness desktop launcher

echo ==========================================
echo   andy-harness start desktop shell (Tauri)
echo ==========================================
echo.
echo   One native window runs everything:
echo     Vite dev server (5173) + Rust/Tauri shell + backend
echo     started through desktop\launcher\launch-backend.py
echo.
echo   Backend: loopback only (127.0.0.1), dynamic port, data in
echo   %%APPDATA%%\andy-harness -- independent from the start-all.bat
echo   layout (backend\data).  Capabilities: Computer Use, MCP client
echo   + MCP market, Skills + skill market, scheduled tasks, subagent
echo   + orchestration (parallel / pipeline), long-term memory +
echo   vector search, run tracing, checkpoint resume, channels
echo   (webhook / Telegram), auth + multi-user, Docker sandbox,
echo   session search -- all auto-loaded plugins, the SQLite schema
echo   upgrades itself on first launch.
echo.
echo   Stop with: close the app window, Ctrl+C in the
echo   "andy-harness-desktop" window, or double-click stop-desktop.bat.
echo   Do NOT run start-all.bat at the same time (both need port 5173).
echo.
echo   Always the latest code: "pnpm dev" runs "cargo build" on every start
echo   and cargo rebuilds by CONTENT fingerprint (not by timestamp), so the
echo   shell is recompiled exactly when Rust / tauri.conf.json / capabilities
echo   change, and is a no-op otherwise. The frontend is served by Vite on
echo   :5173 (HMR) and is never baked into the dev shell. This script prints
echo   a freshness verdict plus what is running, so you can always tell
echo   which build the window corresponds to.
echo   Options: --help   --clean
echo.

if /i "%~1"=="--help" goto usage
if /i "%~1"=="/?" goto usage
if /i "%~1"=="--clean" goto cleanargs
goto afterargs

:usage
echo   Usage: start-desktop.bat [--clean]
echo.
echo   (no option)  Start the desktop shell. tauri dev rebuilds the Rust
echo                shell incrementally, so andy-harness-gy.exe always
echo                matches the current sources.
echo   --clean      Delete desktop\src-tauri\target first, then rebuild the
echo                shell from scratch (slow: minutes, needs the crates).
echo   --help       Show this text.
echo.
echo   Stop with stop-desktop.bat or by closing the app window.
echo.
exit /b 0

:cleanargs
echo.
echo --clean: about to delete desktop\src-tauri\target (from-scratch rebuild).
choice /c YN /n /t 30 /d N /m "   Delete the build cache and rebuild? [Y/N] (default N in 30s): "
if errorlevel 2 goto cleanabort
rd /s /q "%~dp0desktop\src-tauri\target" >nul 2>&1
echo   target\ removed; tauri dev will rebuild from scratch.
goto afterargs

:cleanabort
echo   Cancelled; keeping the existing build cache.
goto afterargs

:afterargs

echo Checking dependencies ...
if not exist "%~dp0backend\.venv\Scripts\python.exe" (
    echo ERROR: backend virtual environment not found.
    echo   Fix:  cd backend ^&^& uv sync --extra dev
    echo   Then re-run start-desktop.bat
    echo.
    pause
    exit /b 1
)
if not exist "%~dp0frontend\node_modules" (
    echo ERROR: frontend dependencies not installed.
    echo   Fix:  cd frontend ^&^& pnpm install
    echo   Then re-run start-desktop.bat
    echo.
    pause
    exit /b 1
)
where pnpm >nul 2>&1
if errorlevel 1 goto nopnpm
if not exist "%~dp0desktop\node_modules" (
    echo Desktop dependencies missing -- installing Tauri CLI ...
    pushd "%~dp0desktop"
    call pnpm install
    popd
    if not exist "%~dp0desktop\node_modules" goto pnpminstallfail
)
rem Rust toolchain: pick up rustup's CARGO_HOME / RUSTUP_HOME from the user
rem environment even if this window was opened before rustup configured them.
if not defined CARGO_HOME for /f "tokens=2,*" %%a in ('reg query "HKCU\Environment" /v CARGO_HOME 2^>nul') do set "CARGO_HOME=%%b"
if not defined RUSTUP_HOME for /f "tokens=2,*" %%a in ('reg query "HKCU\Environment" /v RUSTUP_HOME 2^>nul') do set "RUSTUP_HOME=%%b"
if defined CARGO_HOME if exist "%CARGO_HOME%\bin" set "PATH=%CARGO_HOME%\bin;%PATH%"
rem The desktop backend shell is compiled from Rust; browser mode does not need it.
where cargo >nul 2>&1
if errorlevel 1 goto norust
where rustc >nul 2>&1
if errorlevel 1 goto norust
rem Windows additionally needs the MSVC toolset + Windows SDK: Tauri links the
rem shell with MSVC's link.exe, so rustup alone is NOT enough here.
set "VCDIR="
set "VSWHERE=%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe"
if exist "%VSWHERE%" for /f "delims=" %%i in ('"%VSWHERE%" -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath 2^>nul') do set "VCDIR=%%i"
if not defined VCDIR goto novc
echo Dependencies OK. [Rust + MSVC toolset at %VCDIR%]
echo.

rem ---- build freshness verdict (no build is forced here; tauri dev does it) ----
set "SRC_TAURI=%~dp0desktop\src-tauri"
set "SHELLEXE=%SRC_TAURI%\target\debug\andy-harness-gy.exe"
set "FRESHLINE="
for /f "usebackq delims=" %%a in (`powershell -NoProfile -Command "$root='%SRC_TAURI%'; $exe=Join-Path $root 'target\debug\andy-harness-gy.exe'; $pat=@('src','Cargo.toml','Cargo.lock','build.rs','tauri.conf.json','capabilities'); $items=@(); foreach($n in $pat){ $p=Join-Path $root $n; if(Test-Path $p){ $i=Get-Item $p; if($i.PSIsContainer){ $items+=@(Get-ChildItem $p -Recurse -File) } else { $items+=$i } } }; $best=$null; foreach($f in $items){ if($null -eq $best -or $f.LastWriteTime -gt $best.LastWriteTime){ $best=$f } }; if(-not (Test-Path $exe)){ 'MISSING' } elseif($null -ne $best -and $best.LastWriteTime -gt (Get-Item $exe).LastWriteTime){ 'STALE ~ newest source: ' + $best.Name + ' @ ' + $best.LastWriteTime.ToString('yyyy-MM-dd HH:mm') } else { 'FRESH ~ built at ' + (Get-Item $exe).LastWriteTime.ToString('yyyy-MM-dd HH:mm') }"`) do set "FRESHLINE=%%a"
echo Checking build freshness ...
if not defined FRESHLINE set "FRESHLINE=UNKNOWN (PowerShell check unavailable)"
echo   shell: %FRESHLINE%
if "%FRESHLINE:~0,7%"=="MISSING" echo   -^) no debug exe yet; tauri dev builds it now (first build takes minutes)
if "%FRESHLINE:~0,5%"=="STALE" echo   -^) a source is newer than the exe; tauri dev re-verifies and rebuilds only if content changed
if "%FRESHLINE:~0,5%"=="FRESH" echo   -^) exe is newer than every tracked source; tauri dev re-verifies with cargo
if "%FRESHLINE:~0,5%"=="UNKNO" echo   -^) freshness unknown; tauri dev still runs cargo build, so the shell stays correct
echo.

echo Checking ports ...
netstat -ano | findstr ":5173" | findstr "LISTENING" >nul 2>&1
if not errorlevel 1 goto port5173busy
echo Port 5173 is free. Starting the desktop shell ...
echo.

echo Launching tauri dev (Vite + Rust shell + backend) ...
start "andy-harness-desktop" cmd /k "cd /d %~dp0desktop && pnpm dev"

echo Waiting for the Vite dev server on port 5173 ...
set /a tries=0
:wait_frontend
timeout /t 2 /nobreak >nul
netstat -ano | findstr ":5173" | findstr "LISTENING" >nul 2>&1
if not errorlevel 1 goto frontend_ready
set /a tries+=1
if %tries% lss 60 goto wait_frontend
echo WARN: Vite did not become ready in 120s, continuing anyway.
:frontend_ready

echo.
echo Vite is ready. The andy-harness window appears as soon as the
echo Rust shell finishes compiling (the first build can take minutes).
echo.
set "GITSHA=(git unavailable)"
for /f "usebackq delims=" %%g in (`git -C "%~dp0." rev-parse --short HEAD 2^>nul`) do set "GITSHA=%%g"
set "GITSTATE=clean"
for /f "usebackq delims=" %%g in (`git -C "%~dp0." status --porcelain -uno 2^>nul`) do set "GITSTATE=has uncommitted changes"
echo What is running
echo   shell exe : %SHELLEXE%
rem NOTE: the ~t modifier only works on %1-style parameters, not on %VAR%,
rem so the build timestamp is read via PowerShell instead.
if exist "%SHELLEXE%" for /f "usebackq delims=" %%t in (`powershell -NoProfile -Command "(Get-Item '%SHELLEXE%').LastWriteTime.ToString('yyyy-MM-dd HH:mm:ss')"`) do echo   built at  : %%t
echo   git HEAD  : %GITSHA% (%GITSTATE%)
echo   frontend  : http://localhost:5173 (Vite dev server, HMR - edits are live)
echo   backend   : http://127.0.0.1:^<dynamic port^> loopback only, data in %%APPDATA%%\andy-harness
echo.
echo   If the app window never appears, read the "andy-harness-desktop"
echo   window - that is where cargo prints compiler errors.
echo.
echo Stop it later with stop-desktop.bat, or just close the window.
echo.
exit /b 0

:nopnpm
echo ERROR: pnpm not found in PATH.
echo   Fix:  npm i -g pnpm
echo   Then re-open this window and re-run start-desktop.bat
echo.
pause
exit /b 1

:pnpminstallfail
echo ERROR: pnpm install failed inside desktop\.
echo   Fix:  cd desktop ^&^& pnpm install
echo   Then re-run start-desktop.bat
echo.
pause
exit /b 1

:norust
echo ERROR: Rust toolchain not found [cargo / rustc].
echo   The desktop shell compiles a small Rust program, so Rust is
echo   required for this mode. Browser mode does NOT need Rust.
echo.
echo   Fix:
echo     1^) Install rustup from https://rustup.rs/ [stable toolchain]
echo     2^) Re-open this window so PATH is refreshed
echo     3^) Re-run start-desktop.bat
echo.
echo   Windows also needs the MSVC C++ toolset + Windows SDK; the script
echo   verifies them with vswhere right after Rust is detected.
echo.
echo   Only want the web UI in a browser? Double-click start-all.bat.
echo.
pause
exit /b 1

:novc
echo ERROR: MSVC C++ toolset not found
echo        [Microsoft.VisualStudio.Component.VC.Tools.x86.x64].
echo   Rust is installed, but the Tauri shell links with MSVC's link.exe,
echo   which also needs the Windows SDK. rustup alone cannot build it.
echo.
echo   Fix (VS 2022 Build Tools):
echo     1^) Download https://aka.ms/vs/17/release/vs_buildtools.exe
echo     2^) Run:
echo        vs_buildtools.exe --quiet --wait --norestart ^
echo           --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended
echo     3^) Re-run start-desktop.bat
echo.
echo   Only want the web UI in a browser? Double-click start-all.bat.
echo.
pause
exit /b 1

:port5173busy
echo ERROR: Port 5173 is already in use.
echo Stop the running desktop shell [stop-desktop.bat] or the
echo browser-mode frontend [stop-all.bat] first.
echo.
pause
exit /b 1
