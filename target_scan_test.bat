@echo off
setlocal EnableExtensions
cd /d "%~dp0"

echo Starting barcode / QR scanner keyboard test...
echo This test does not connect to PLC or ATEQ.
echo Click the test window input box, then scan a code.
echo.
python scan_keyboard_test.py
pause
