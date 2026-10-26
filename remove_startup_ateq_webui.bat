@echo off
setlocal

set "TARGET_BAT=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\start_ateq_webui.bat"
set "TARGET_VBS=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\start_ateq_webui_silent.vbs"
set "REMOVED_ANY="

if exist "%TARGET_BAT%" (
    del /F /Q "%TARGET_BAT%"
    if errorlevel 1 (
        echo [ERROR] Failed to remove startup script:
        echo %TARGET_BAT%
        exit /b 1
    )
    set "REMOVED_ANY=1"
    echo [OK] Removed:
    echo %TARGET_BAT%
)

if exist "%TARGET_VBS%" (
    del /F /Q "%TARGET_VBS%"
    if errorlevel 1 (
        echo [ERROR] Failed to remove startup script:
        echo %TARGET_VBS%
        exit /b 1
    )
    set "REMOVED_ANY=1"
    echo [OK] Removed:
    echo %TARGET_VBS%
)

if not defined REMOVED_ANY (
    echo [INFO] Startup script was not installed.
)

exit /b 0
