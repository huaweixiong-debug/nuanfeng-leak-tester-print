@echo off
setlocal EnableExtensions EnableDelayedExpansion
rem ============================================================
rem  ATEQ one-click launcher: start the service (if needed) and open the UI full screen.
rem  - Service already running the current version -> just open the UI.
rem  - Not running, old code (file newer than process) or not responding -> (re)start, wait, open.
rem  - A test is in progress                        -> never restart; open only.
rem  Usage: ateq_one_click.bat            normal one-click start
rem         ateq_one_click.bat restart    force restart (still refused while testing)
rem         ateq_one_click.bat check      check prerequisites only, change nothing
rem ============================================================

pushd "%~dp0" >nul 2>&1
if errorlevel 1 (
    echo [FAIL] Cannot access the ATEQ folder: "%~dp0"
    goto fail
)
set "APP_DIR=%CD%"
set "PORT=8001"
set "APP_URL=http://127.0.0.1:%PORT%/"
rem Layout marker of the current UI. The running process must also be newer than
rem webui_server.py on disk; otherwise the code was updated and a restart is needed.
set "MARKER=min-width: 1280px"
set "MODE=%~1"

echo [1/4] Checking Python and project files...
call "%APP_DIR%\start_ateq_webui.bat" check
if errorlevel 1 (
    echo [FAIL] Python with FastAPI or webui_server.py was not found. Nothing was changed.
    goto fail
)
if /i "%MODE%"=="check" (
    echo [OK] Check finished. Nothing was started or stopped.
    goto done_quiet
)

echo.
echo [2/4] Checking the running service on port %PORT%...
call :probe
rem PROBE: 0 = not running, 1 = running current version, 2 = old version or not responding, 3 = busy (test in progress)
if "%PROBE%"=="3" (
    echo [WARN] A test is in progress. The service will NOT be restarted.
    echo        Opening the UI only. Run this again after the test finishes if a restart is needed.
    goto open
)
if "%PROBE%"=="1" if /i not "%MODE%"=="restart" (
    echo [OK] Service is already running the current version.
    goto open
)
if "%PROBE%"=="0" (
    echo       Service is not running.
    goto start
)

echo.
echo [3/4] Stopping the old service process...
powershell -NoProfile -Command "$ids = @(); $ids += (Get-NetTCPConnection -LocalPort %PORT% -State Listen -ErrorAction SilentlyContinue).OwningProcess; $dir = [regex]::Escape('%APP_DIR%'); $ids += (Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -match 'webui_server\.py' -and $_.CommandLine -match $dir }).ProcessId; foreach ($id in ($ids | Where-Object { $_ } | Sort-Object -Unique)) { try { Stop-Process -Id $id -Force -ErrorAction Stop; Write-Host ('      stopped PID ' + $id) } catch { if (Get-Process -Id $id -ErrorAction SilentlyContinue) { Write-Host ('      could not stop PID ' + $id + ': ' + $_.Exception.Message) } } }"
set /a TRIES=0
:waitfree
set /a TRIES+=1
powershell -NoProfile -Command "if (Get-NetTCPConnection -LocalPort %PORT% -State Listen -ErrorAction SilentlyContinue) { exit 1 } else { exit 0 }" >nul 2>nul
if not errorlevel 1 goto start
if %TRIES% GEQ 15 (
    echo [FAIL] Port %PORT% is still occupied. Try running this file as administrator.
    netstat -ano | findstr ":%PORT%" | findstr "LISTENING"
    goto fail
)
timeout /t 1 /nobreak >nul
goto waitfree

:start
echo.
echo [3/4] Starting the ATEQ service...
call "%APP_DIR%\start_ateq_webui.bat"
if errorlevel 1 (
    echo [FAIL] The service process could not be started. Startup log:
    if exist "%APP_DIR%\logs\webui_start_stderr.log" type "%APP_DIR%\logs\webui_start_stderr.log"
    goto fail
)
echo       Waiting for the UI to become ready (up to 60 s)...
for /L %%I in (1,1,60) do (
    call :probe
    if "!PROBE!"=="1" goto ready
    if "!PROBE!"=="3" goto ready
    timeout /t 1 /nobreak >nul
)
call :probe
if "%PROBE%"=="1" goto ready
echo [FAIL] The UI did not become ready within 60 seconds. Startup log:
if exist "%APP_DIR%\logs\webui_start_stderr.log" type "%APP_DIR%\logs\webui_start_stderr.log"
goto fail

:ready
echo [OK] Service is ready.

:open
echo.
echo [4/4] Opening the UI full screen: %APP_URL%
rem Edge app window (no address bar/tabs) in full screen; F11 leaves full screen, Alt+F4 closes.
set "EDGE="
for %%E in ("%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe" "%ProgramFiles%\Microsoft\Edge\Application\msedge.exe") do (
    if not defined EDGE if exist "%%~E" set "EDGE=%%~E"
)
if defined EDGE (
    start "" "!EDGE!" --app=%APP_URL% --start-fullscreen --no-first-run
) else (
    echo       Edge was not found; opening the default browser. Press F11 for full screen.
    start "" "%APP_URL%"
)
echo       If the page looks old, press Ctrl+F5. If it is not full screen, press F11.
popd
timeout /t 5 >nul
exit /b 0

:probe
rem Sets PROBE (see codes above). Never stops or starts anything.
powershell -NoProfile -Command "try { $r = Invoke-WebRequest -UseBasicParsing -Uri '%APP_URL%' -TimeoutSec 3 } catch { if (Get-NetTCPConnection -LocalPort %PORT% -State Listen -ErrorAction SilentlyContinue) { exit 2 } else { exit 0 } }; try { $s = Invoke-RestMethod -Uri '%APP_URL%api/test_status' -TimeoutSec 3; if ($s.busy) { exit 3 } } catch {}; if (-not $r.Content.Contains('%MARKER%')) { exit 2 }; $own = @(Get-NetTCPConnection -LocalPort %PORT% -State Listen -ErrorAction SilentlyContinue)[0].OwningProcess; $proc = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $own) -ErrorAction SilentlyContinue; $file = Get-Item '%APP_DIR%\webui_server.py' -ErrorAction SilentlyContinue; if ($proc -and $file -and $proc.CreationDate -lt $file.LastWriteTime) { exit 2 }; exit 1" >nul 2>nul
set "PROBE=%errorlevel%"
exit /b 0

:done_quiet
popd
exit /b 0

:fail
popd
pause
exit /b 1
