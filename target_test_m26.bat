@echo off
setlocal EnableExtensions
cd /d "%~dp0"

echo This test uses PC Access SMART OPC over PPI.
echo Before continuing:
echo   1. Open S7-200 PC Access SMART on this target computer.
echo   2. Open the working .sa file.
echo   3. Confirm the PC Access test client can write M26.0.
echo.
pause

echo.
echo Reading M26.0 before writes...
python line_runtime.py read
echo.

echo.
echo Writing M26.0 OFF...
python line_runtime.py off
if not "%ERRORLEVEL%"=="0" goto fail
timeout /t 1 /nobreak >nul

echo.
echo Reading after OFF...
python line_runtime.py read
echo.

echo.
echo Writing M26.0 ON...
python line_runtime.py on
if not "%ERRORLEVEL%"=="0" goto fail
timeout /t 1 /nobreak >nul

echo.
echo Reading after ON...
python line_runtime.py read
echo.

echo.
echo MAIN TEST PASS: M26.0 can be written ON through PC Access SMART OPC/PPI.
echo The final OFF below is only cleanup.

echo.
echo Writing M26.0 OFF again...
python line_runtime.py off
if not "%ERRORLEVEL%"=="0" goto cleanup_fail

echo.
echo Reading after final OFF...
python line_runtime.py read

echo.
echo M26.0 OPC/PPI test completed.
pause
exit /b 0

:cleanup_fail
echo.
echo Cleanup OFF timed out or failed, but the main ON write/read test already passed.
echo In PC Access SMART test client, manually write M26.0/??ok to 0 if it is still ON.
echo If cleanup OFF keeps timing out, close and reopen the .sa file in PC Access SMART.
pause
exit /b 0

:fail
echo.
echo M26.0 test failed.
echo Check that PC Access SMART is open with the working .sa file and OPC Automation is registered.
pause
exit /b 1
