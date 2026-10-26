Option Explicit
On Error Resume Next

Dim ids, id, obj
ids = Split("OPC.Automation.1;OPCDAAuto.OPCServer", ";")

For Each id In ids
    Err.Clear
    Set obj = CreateObject(id)
    If Err.Number = 0 Then
        WScript.Echo id & " OK"
        WScript.Quit 0
    End If
    WScript.Echo id & " failed: " & Err.Description
Next

WScript.Quit 1
