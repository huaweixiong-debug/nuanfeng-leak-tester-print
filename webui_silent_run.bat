@echo off
rem Hidden launcher for the ATEQ WebUI (started by silent_start_ui.vbs).
rem All output goes to the log file so print() can never fail or show a window.
cd /d "D:\ateq"
".venv\Scripts\python.exe" "webui_server.py" >> "logs\webui_silent.log" 2>&1
