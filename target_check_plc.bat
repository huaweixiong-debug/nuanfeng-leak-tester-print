@echo off
setlocal EnableExtensions
cd /d "%~dp0"

echo ==== 1. Check OPC Automation Wrapper ====
set "CSCRIPT=%WINDIR%\SysWOW64\cscript.exe"
if not exist "%CSCRIPT%" set "CSCRIPT=%WINDIR%\System32\cscript.exe"
"%CSCRIPT%" //nologo "%~dp0check_opc_automation.vbs"
if not "%ERRORLEVEL%"=="0" (
    echo.
    echo OPC Automation Wrapper is not registered.
    echo Run fix_opc_automation.bat as administrator first.
    pause
    exit /b 1
)

echo.
echo ==== 2. Check Python and project PLC backend ====
python -c "import line_runtime; print('PLC backend:', line_runtime.PLC_BACKEND); print('Settings:', line_runtime.get_line_settings())"
if not "%ERRORLEVEL%"=="0" (
    echo Python could not import line_runtime.py.
    pause
    exit /b 1
)

echo.
echo OPC/Python base check passed.
echo Now make sure S7-200 PC Access SMART is open with the working .sa file.
pause
