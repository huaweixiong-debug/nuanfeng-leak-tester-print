@echo off
setlocal

set "SOURCE_VBS=D:\ateq\start_ateq_webui_silent.vbs"
set "STARTUP_DIR=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "TARGET_VBS=%STARTUP_DIR%\start_ateq_webui_silent.vbs"
set "OLD_BAT=%STARTUP_DIR%\start_ateq_webui.bat"

if not exist "%SOURCE_VBS%" (
    echo [ERROR] Cannot find "%SOURCE_VBS%"
    exit /b 1
)

if not exist "%STARTUP_DIR%" (
    echo [ERROR] Windows Startup folder not found: "%STARTUP_DIR%"
    exit /b 1
)

copy /Y "%SOURCE_VBS%" "%TARGET_VBS%" >nul
if errorlevel 1 (
    echo [ERROR] Failed to copy startup script.
    exit /b 1
)

if exist "%OLD_BAT%" (
    del /F /Q "%OLD_BAT%" >nul 2>nul
)

echo [OK] Installed silent startup to Windows Startup:
echo %TARGET_VBS%
exit /b 0
