Option Explicit

Dim shell
Set shell = CreateObject("WScript.Shell")

shell.Run Chr(34) & "D:\ateq\start_ateq_webui_silent.vbs" & Chr(34), 0, False
WScript.Sleep 3000
shell.Run "http://127.0.0.1:8001/", 0, False
