@echo off
setlocal EnableExtensions

rem Resolve local or UNC launch paths. pushd assigns a temporary drive for UNC paths.
pushd "%~dp0" >nul 2>&1
if errorlevel 1 (
    echo [FAIL] Cannot access the ATEQ project folder: "%~dp0"
    pause
    exit /b 1
)

set "APP_DIR=%CD%"
set "APP_URL=http://127.0.0.1:8001/"
set "MARKER=UI-20261001-1"

if /i "%~1"=="check" goto check
if /i "%~1"=="elevated" goto adminready

powershell -NoProfile -Command "$principal = [Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent()); if ($principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { exit 0 } else { exit 1 }" >nul 2>nul
if errorlevel 1 goto request_admin

:adminready

echo [1/3] Checking Python and project files...
call "%APP_DIR%\start_ateq_webui.bat" check
if errorlevel 1 goto dependency_failed

echo.
echo [2/3] Stopping the current ATEQ WebUI process...
powershell -NoProfile -Command "$pids = @(); $pids += (Get-NetTCPConnection -LocalPort 8001 -State Listen -ErrorAction SilentlyContinue).OwningProcess; $project = [regex]::Escape('%APP_DIR%'); $pids += (Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -match 'webui_server\.py' -and $_.CommandLine -match $project }).ProcessId; $pids | Where-Object { $_ } | Sort-Object -Unique | ForEach-Object { try { Stop-Process -Id $_ -Force -ErrorAction Stop; Write-Host ('  stopped PID ' + $_) } catch { Write-Host ('  could not stop PID ' + $_ + ': ' + $_.Exception.Message) } }"

set /a TRIES=0
:waitport
set /a TRIES+=1
if %TRIES% GTR 15 goto portbusy
powershell -NoProfile -Command "if (Get-NetTCPConnection -LocalPort 8001 -State Listen -ErrorAction SilentlyContinue) { exit 1 } else { exit 0 }" >nul 2>nul
if %errorlevel%==0 goto portfree
timeout /t 1 /nobreak >nul
goto waitport

:portbusy
echo [FAIL] Port 8001 is still occupied:
netstat -ano | findstr ":8001" | findstr "LISTENING"
echo Run this script as administrator if the old process cannot be stopped.
pause
exit /b 1

:portfree
echo       Port 8001 is free.
echo.
echo [3/3] Starting ATEQ WebUI and waiting for the page to be ready...
call "%APP_DIR%\start_ateq_webui.bat"
if errorlevel 1 goto start_failed

for /L %%I in (1,1,60) do (
    powershell -NoProfile -Command "try { $r = Invoke-WebRequest -UseBasicParsing -Uri '%APP_URL%' -TimeoutSec 3; if ($r.StatusCode -eq 200 -and $r.Content.Contains('%MARKER%')) { exit 0 } } catch {}; exit 1" >nul 2>nul
    if not errorlevel 1 goto ready
    timeout /t 1 /nobreak >nul
)
goto page_not_ready

:ready
echo [OK] ATEQ WebUI is ready and the expected page version was found.
start "" "%APP_URL%"
echo If the browser still shows an old page, press Ctrl+F5.
pause
exit /b 0

:request_admin
echo [INFO] Requesting administrator permission through Windows UAC...
powershell -NoProfile -Command "try { Start-Process -FilePath '%~f0' -ArgumentList 'elevated' -Verb RunAs; exit 0 } catch { Write-Host ('[FAIL] ' + $_.Exception.Message); exit 1 }"
if errorlevel 1 (
    echo [FAIL] Administrator permission was not granted. No process was stopped.
    pause
    exit /b 1
)
exit /b 0

:dependency_failed
echo [FAIL] Startup prerequisites were not found. The running service was left untouched.
pause
exit /b 1

:start_failed
echo [FAIL] The WebUI process could not be started. Startup log:
if exist "%APP_DIR%\logs\webui_start_stderr.log" type "%APP_DIR%\logs\webui_start_stderr.log"
pause
exit /b 1

:page_not_ready
echo [FAIL] The page did not become ready within 60 seconds.
echo Check the startup log at "%APP_DIR%\logs\webui_start_stderr.log".
if exist "%APP_DIR%\logs\webui_start_stderr.log" type "%APP_DIR%\logs\webui_start_stderr.log"
netstat -ano | findstr ":8001" | findstr "LISTENING"
pause
exit /b 1

:check
echo [CHECK] Verifying the restart script without stopping or starting the service...
call "%APP_DIR%\start_ateq_webui.bat" check
exit /b %errorlevel%