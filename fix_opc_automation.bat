@echo off
setlocal EnableExtensions EnableDelayedExpansion

set "SCRIPT_DIR=%~dp0"
set "LOG=%SCRIPT_DIR%opc_automation_fix.txt"
set "CSCRIPT=%WINDIR%\SysWOW64\cscript.exe"
set "REGSVR32=%WINDIR%\SysWOW64\regsvr32.exe"

echo OPC Automation Wrapper fix > "%LOG%"
echo Time: %DATE% %TIME% >> "%LOG%"
echo Computer: %COMPUTERNAME% >> "%LOG%"
echo User: %USERNAME% >> "%LOG%"
echo. >> "%LOG%"

if not exist "%CSCRIPT%" set "CSCRIPT=%WINDIR%\System32\cscript.exe"
if not exist "%REGSVR32%" set "REGSVR32=%WINDIR%\System32\regsvr32.exe"

net session >nul 2>&1
if not "%ERRORLEVEL%"=="0" (
    echo Administrator rights are required to register OPCDAAuto.dll.
    echo Requesting administrator rights...
    powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~f0' -WorkingDirectory '%SCRIPT_DIR%' -Verb RunAs"
    exit /b 0
)

echo ==== Current 32-bit OPC Automation check ==== >> "%LOG%"
"%CSCRIPT%" //nologo "%SCRIPT_DIR%check_opc_automation.vbs" >> "%LOG%" 2>&1
if "%ERRORLEVEL%"=="0" (
    echo OPC Automation Wrapper is already registered.
    echo Details saved to "%LOG%"
    pause
    exit /b 0
)

echo ==== Searching for OPCDAAuto.dll ==== >> "%LOG%"
set "FOUND_ANY="
call :TryRegister "%WINDIR%\SysWOW64\OPCDAAuto.dll"
call :TryRegister "%ProgramFiles(x86)%\Common Files\OPC Foundation\Bin\OPCDAAuto.dll"
call :TryRegister "%ProgramFiles(x86)%\Common Files\OPC Foundation\OPCDAAuto.dll"
call :TryRegister "%ProgramFiles(x86)%\Common Files\MatrikonOPC\Common\OPCDAAuto.dll"
call :TryRegister "%ProgramFiles(x86)%\Siemens\S7-200 PC Access SMART\OPCDAAuto.dll"
call :TryRegister "%ProgramFiles(x86)%\Siemens\STEP 7-MicroWIN SMART\OPCDAAuto.dll"

for %%R in ("%ProgramFiles(x86)%" "%ProgramFiles%" "%WINDIR%\SysWOW64") do (
    if exist "%%~R" (
        for /f "delims=" %%P in ('dir /b /s "%%~R\OPCDAAuto.dll" 2^>nul') do (
            call :TryRegister "%%~fP"
        )
    )
)

echo. >> "%LOG%"
echo ==== Final 32-bit OPC Automation check ==== >> "%LOG%"
"%CSCRIPT%" //nologo "%SCRIPT_DIR%check_opc_automation.vbs" >> "%LOG%" 2>&1
if "%ERRORLEVEL%"=="0" (
    echo OPC Automation Wrapper registered successfully.
    echo Details saved to "%LOG%"
    pause
    exit /b 0
)

echo OPC Automation Wrapper is still not registered.
if not defined FOUND_ANY (
    echo OPCDAAuto.dll was not found on this computer.
) else (
    echo OPCDAAuto.dll was found, but registration did not make the COM object available.
)
echo Reinstall S7-200 PC Access SMART with OPC components, then rerun this file.
echo Details saved to "%LOG%"
pause
exit /b 1

:TryRegister
set "DLL=%~1"
if not exist "%DLL%" exit /b 0
set "FOUND_ANY=1"
echo Found: %DLL%
echo Found: %DLL% >> "%LOG%"
"%REGSVR32%" /s "%DLL%" >> "%LOG%" 2>&1
echo regsvr32 exit code: %ERRORLEVEL% >> "%LOG%"
exit /b 0
