@echo off
setlocal EnableExtensions

rem One-click entry point: restart the service and open the WebUI when ready.
if not exist "%~dp0restart_webui.bat" (
    echo [FAIL] Cannot find restart_webui.bat beside this launcher.
    pause
    exit /b 1
)

if /i "%~1"=="check" (
    call "%~dp0restart_webui.bat" check
) else (
    call "%~dp0restart_webui.bat"
)
exit /b %errorlevel%
