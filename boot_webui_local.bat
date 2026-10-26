@echo off
rem Start ATEQ WebUI from the project venv, log to logs\webui_boot.log
cd /d "%~dp0"
if not exist logs mkdir logs
".venv\Scripts\python.exe" webui_server.py > logs\webui_boot.log 2>&1
