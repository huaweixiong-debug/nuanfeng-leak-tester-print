@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "SCAN_SERIAL_PORT=COM4"
set "SCAN_SERIAL_BAUDRATE=9600"
set "SCAN_SERIAL_BYTESIZE=8"
set "SCAN_SERIAL_PARITY=N"
set "SCAN_SERIAL_STOPBITS=1"

echo Starting serial barcode / QR scanner test...
echo Port: %SCAN_SERIAL_PORT% %SCAN_SERIAL_BAUDRATE% 8N1
echo Close the serial debug tool first, then scan a code.
echo.
python scan_serial_test.py
pause
