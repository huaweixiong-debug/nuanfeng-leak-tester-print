@echo off
setlocal

set "APP_DIR=%ATEQ_APP_DIR%"
for %%I in ("%~dp0.") do if not defined APP_DIR set "APP_DIR=%%~fI"
set "APP_SCRIPT=%APP_DIR%\webui_server.py"
set "LOG_DIR=%APP_DIR%\logs"
set "APP_URL=http://127.0.0.1:8001/"
set "MODE=%~1"

if not exist "%APP_SCRIPT%" (
    echo [ERROR] Cannot find "%APP_SCRIPT%"
    exit /b 1
)

if not exist "%LOG_DIR%" (
    mkdir "%LOG_DIR%" >nul 2>nul
)

set "PYTHON_EXE="
set "PYTHON_ARGS="

for %%I in ("%APP_DIR%\.venv\Scripts\python.exe" "%APP_DIR%\venv\Scripts\python.exe" "D:\anaconda3\python.exe" "D:\miniconda3\python.exe" "C:\Python314\python.exe") do (
    if not defined PYTHON_EXE if exist "%%~I" (
        "%%~I" -c "import fastapi" >nul 2>nul
        if not errorlevel 1 set "PYTHON_EXE=%%~I"
    )
)

if not defined PYTHON_EXE if exist "%SystemRoot%\py.exe" (
    "%SystemRoot%\py.exe" -3 -c "import fastapi" >nul 2>nul
    if not errorlevel 1 (
        set "PYTHON_EXE=%SystemRoot%\py.exe"
        set "PYTHON_ARGS=-3"
    )
)

if not defined PYTHON_EXE (
    for /f "delims=" %%I in ('where python 2^>nul') do (
        if not defined PYTHON_EXE (
            "%%I" -c "import fastapi" >nul 2>nul
            if not errorlevel 1 set "PYTHON_EXE=%%I"
        )
    )
)

if not defined PYTHON_EXE (
    echo [ERROR] Python with FastAPI was not found.
    exit /b 1
)

if /i "%MODE%"=="check" (
    echo [OK] APP_SCRIPT=%APP_SCRIPT%
    echo [OK] PYTHON="%PYTHON_EXE%" %PYTHON_ARGS%
    echo [OK] LOG_DIR=%LOG_DIR%
    exit /b 0
)

powershell -NoProfile -Command "if (Get-NetTCPConnection -LocalPort 8001 -State Listen -ErrorAction SilentlyContinue) { exit 0 } else { exit 1 }" >nul 2>nul
if "%errorlevel%"=="0" (
    echo [INFO] ATEQ WebUI is already running: %APP_URL%
    if /i "%MODE%"=="open" (
        start "" "%APP_URL%"
    )
    exit /b 0
)

set "PS_ARGS='%APP_SCRIPT%'"
if defined PYTHON_ARGS (
    set "PS_ARGS='%PYTHON_ARGS%','%APP_SCRIPT%'"
)

set "STDOUT_LOG=%LOG_DIR%\webui_start_stdout.log"
set "STDERR_LOG=%LOG_DIR%\webui_start_stderr.log"
powershell -NoProfile -Command "Start-Process -FilePath '%PYTHON_EXE%' -ArgumentList @(%PS_ARGS%) -WorkingDirectory '%APP_DIR%' -RedirectStandardOutput '%STDOUT_LOG%' -RedirectStandardError '%STDERR_LOG%' -WindowStyle Minimized" >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Failed to create ATEQ WebUI process. See "%STDERR_LOG%".
    exit /b 1
)

if /i "%MODE%"=="open" (
    timeout /t 3 /nobreak >nul
    start "" "%APP_URL%"
)

exit /b 0
