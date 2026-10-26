' Hidden layer: runs the launcher bat with window style 0 (never visible).
' This script is started detached (via WMI) so it survives the parent session.
Option Explicit

Dim shell
Set shell = CreateObject("WScript.Shell")
shell.Run """" & "D:\ateq\webui_silent_run.bat" & """", 0, False
