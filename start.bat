@echo off
setlocal
set "ATEQ_APP_DIR=D:\ateq"
set "START_SCRIPT=%ATEQ_APP_DIR%\start_ateq_webui.bat"
if not exist "%START_SCRIPT%" (
    echo [ERROR] Cannot find "%START_SCRIPT%"
    exit /b 1
)
call "%START_SCRIPT%" %*
exit /b %ERRORLEVEL%
