' ============================================================
' ATEQ silent start: hidden service start + open UI page.
' Double-click it, or put a shortcut to it in the Startup folder.
' No console window is ever shown; the service layer is started
' through WMI so it survives the script and any parent session.
' ============================================================
Option Explicit

Dim shell, fso
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

Const APP_DIR = "D:\ateq"
Const URL = "http://127.0.0.1:8001/"

Function ServiceAlive()
    On Error Resume Next
    Dim http
    Set http = CreateObject("WinHttp.WinHttpRequest.5.1")
    http.SetTimeouts 2000, 2000, 2000, 2000
    http.open "GET", URL, False
    http.setRequestHeader "Cache-Control", "no-cache"
    http.send
    If Err.Number <> 0 Then
        Err.Clear
        ServiceAlive = False
    Else
        ServiceAlive = (http.status = 200)
    End If
    On Error GoTo 0
End Function

If Not ServiceAlive() Then
    If Not fso.FileExists(APP_DIR & "\webui_silent_run.vbs") Then
        MsgBox "Cannot find " & APP_DIR & "\webui_silent_run.vbs", 16, "ATEQ silent start"
        WScript.Quit 1
    End If
    ' WMI launch: detached from this script and any console/session tree,
    ' but still windowless (wscript.exe has no console window).
    On Error Resume Next
    Dim wmi, procId, ret
    Set wmi = GetObject("winmgmts:{impersonationLevel=impersonate}!\\.\root\cimv2")
    ret = wmi.Get("Win32_Process").Create( _
        "wscript.exe """ & APP_DIR & "\webui_silent_run.vbs""", APP_DIR, Null, procId)
    On Error GoTo 0
    If Err.Number <> 0 Or ret <> 0 Then
        MsgBox "Failed to start the ATEQ service (WMI code " & ret & ")", 16, "ATEQ silent start"
        WScript.Quit 1
    End If
End If

' Wait for the service (max 30s), then open the UI page.
' The browser is opened through a hidden "start" so this script never blocks.
Dim waited
waited = 0
Do While Not ServiceAlive() And waited < 30000
    WScript.Sleep 500
    waited = waited + 500
Loop

On Error Resume Next
shell.Run "cmd /c start """" " & URL, 0, False
On Error GoTo 0
