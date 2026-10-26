Option Explicit

Dim shell, fso, startBat

Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

startBat = "D:\ateq\start_ateq_webui.bat"

If Not fso.FileExists(startBat) Then
    WScript.Quit 1
End If

shell.Run "cmd.exe /c " & Chr(34) & Chr(34) & startBat & Chr(34) & Chr(34), 0, False
