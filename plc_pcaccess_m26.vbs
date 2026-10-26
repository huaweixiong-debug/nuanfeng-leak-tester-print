Option Explicit
On Error Resume Next

Const OPC_CACHE = 1
Const OPC_DEVICE = 2
Const OPC_QUALITY_GOOD = 192

Dim shell, env, action, servers, items, automationIds, settleMs, retries, readSource
Set shell = CreateObject("WScript.Shell")
Set env = shell.Environment("PROCESS")

action = LCase(Trim(ReadTextEnv(env, "PLC_OPC_ACTION", "")))
settleMs = ReadIntEnv(env, "PLC_VERIFY_SETTLE_MS", 300)
retries = ReadIntEnv(env, "PLC_OPC_RETRIES", 3)
readSource = LCase(Trim(ReadTextEnv(env, "PLC_OPC_READ_SOURCE", "cache")))
automationIds = SplitList(ReadTextEnv(env, "PLC_OPC_AUTOMATION", "OPC.Automation.1;OPCDAAuto.OPCServer"), ";")
servers = SplitList(ReadTextEnv(env, "PLC_OPC_SERVERS", ReadTextEnv(env, "PLC_OPC_SERVER", "S7200Smart.OPCServer")), ",")
items = SplitList(ReadTextEnv(env, "PLC_OPC_ITEMS", _
    "2:0.0.0.0:0201:0201,M26.0,BOOL,RW;" & _
    "MWSMART:2:0.0.0.0:0201:0201,M26.0,BOOL,RW;" & _
    "MWSMART:2:0.0.0.0:0201:0201,M26.0,BOOL,RW,0.0000000,0.0000000"), ";")

If action <> "write_on" And action <> "write_off" And action <> "read" Then
    Fail "PLC_OPC_ACTION must be write_on, write_off, or read."
End If

Dim serverName, itemId, lastError
lastError = ""

For Each serverName In servers
    serverName = Trim(CStr(serverName))
    If Len(serverName) > 0 Then
        For Each itemId In items
            itemId = Trim(CStr(itemId))
            If Len(itemId) > 0 Then
                WScript.Echo "Using OPC server: " & serverName
                WScript.Echo "Using OPC item: " & itemId
                lastError = ""

                If action = "read" Then
                    If ReadValue(serverName, itemId, "", settleMs, lastError) Then
                        WScript.Quit 0
                    End If
                ElseIf action = "write_on" Then
                    If WriteOnRobust(serverName, itemId, retries, lastError) Then
                        WScript.Echo "WRITE ON OK: " & serverName & " | " & itemId
                        WScript.Quit 0
                    End If
                ElseIf action = "write_off" Then
                    If WriteOffRobust(serverName, itemId, retries, lastError) Then
                        WScript.Echo "WRITE OFF OK: " & serverName & " | " & itemId
                        WScript.Quit 0
                    End If
                End If

                WScript.Echo UCase(action) & " candidate failed for " & itemId & ": " & lastError
            End If
        Next
    End If
Next

Fail UCase(action) & " failed. " & lastError

Function WriteOnRobust(serverName, itemId, maxRetries, ByRef lastErrorOut)
    On Error Resume Next

    Dim attempt, directError
    For attempt = 1 To maxRetries
        directError = ""
        If WriteValue(serverName, itemId, True, "ON", directError) Then
            WriteOnRobust = True
            Exit Function
        End If

        lastErrorOut = "attempt " & CStr(attempt) & " direct=[" & directError & "]"
        WScript.Sleep settleMs
    Next

    WriteOnRobust = False
End Function

Function WriteOffRobust(serverName, itemId, maxRetries, ByRef lastErrorOut)
    On Error Resume Next

    Dim attempt, writeError
    For attempt = 1 To maxRetries
        writeError = ""
        If WriteValue(serverName, itemId, False, "OFF", writeError) Then
            WriteOffRobust = True
            Exit Function
        End If

        lastErrorOut = "attempt " & CStr(attempt) & " write=[" & writeError & "]"
        WScript.Sleep settleMs
    Next

    WriteOffRobust = False
End Function

Function WriteResetThenOn(serverName, itemId, ByRef lastErrorOut)
    On Error Resume Next

    Dim opcServer
    Set opcServer = ConnectServer(serverName, lastErrorOut)
    If opcServer Is Nothing Then
        WriteResetThenOn = False
        Exit Function
    End If

    Dim group
    Err.Clear
    Set group = opcServer.OPCGroups.Add(UniqueGroupName("ATEQPrintDoorM26ResetOn"))
    If Err.Number <> 0 Then
        lastErrorOut = "Create reset/on group failed: " & Err.Description
        Err.Clear
        DisconnectServer opcServer
        WriteResetThenOn = False
        Exit Function
    End If

    group.IsActive = True
    group.IsSubscribed = False

    Dim opcItem
    Err.Clear
    Set opcItem = group.OPCItems.AddItem(itemId, 1)
    If Err.Number <> 0 Then
        lastErrorOut = "Add reset/on item failed: " & Err.Description
        Err.Clear
        DisconnectServer opcServer
        WriteResetThenOn = False
        Exit Function
    End If

    Err.Clear
    opcItem.Write False
    If Err.Number <> 0 Then
        lastErrorOut = "Reset OFF before ON failed: " & Err.Description
        Err.Clear
    End If

    WScript.Sleep settleMs
    Err.Clear
    opcItem.Write True
    If Err.Number <> 0 Then
        lastErrorOut = "Write ON after reset failed: " & Err.Description
        Err.Clear
        DisconnectServer opcServer
        WriteResetThenOn = False
        Exit Function
    End If

    WScript.Sleep settleMs
    DisconnectServer opcServer
    WriteResetThenOn = True
End Function

Function WriteValue(serverName, itemId, valueToWrite, label, ByRef lastErrorOut)
    On Error Resume Next

    Dim opcServer
    Set opcServer = ConnectServer(serverName, lastErrorOut)
    If opcServer Is Nothing Then
        WriteValue = False
        Exit Function
    End If

    Dim group
    Err.Clear
    Set group = opcServer.OPCGroups.Add(UniqueGroupName("ATEQPrintDoorM26"))
    If Err.Number <> 0 Then
        lastErrorOut = "Create write group failed: " & Err.Description
        Err.Clear
        DisconnectServer opcServer
        WriteValue = False
        Exit Function
    End If

    group.IsActive = True
    group.IsSubscribed = False

    Dim opcItem
    Err.Clear
    Set opcItem = group.OPCItems.AddItem(itemId, 1)
    If Err.Number <> 0 Then
        lastErrorOut = "Add write item failed: " & Err.Description
        Err.Clear
        DisconnectServer opcServer
        WriteValue = False
        Exit Function
    End If

    WScript.Echo "Writing " & label & "..."
    WScript.Sleep 100
    Err.Clear
    If CBool(valueToWrite) Then
        opcItem.Write True
    Else
        opcItem.Write False
    End If
    If Err.Number <> 0 Then
        lastErrorOut = "Write " & label & " failed: " & Err.Description
        Err.Clear
        DisconnectServer opcServer
        WriteValue = False
        Exit Function
    End If

    WScript.Sleep settleMs
    DisconnectServer opcServer
    WriteValue = True
End Function

Function IsCurrentValue(serverName, itemId, expectedText)
    Dim readError
    readError = ""
    IsCurrentValue = ReadValue(serverName, itemId, expectedText, settleMs, readError)
End Function

Function ReadValue(serverName, itemId, expectedText, delayMs, ByRef lastErrorOut)
    On Error Resume Next

    Dim opcServer
    Set opcServer = ConnectServer(serverName, lastErrorOut)
    If opcServer Is Nothing Then
        ReadValue = False
        Exit Function
    End If

    Dim group
    Err.Clear
    Set group = opcServer.OPCGroups.Add(UniqueGroupName("ATEQPrintDoorM26Read"))
    If Err.Number <> 0 Then
        lastErrorOut = "Create read group failed: " & Err.Description
        Err.Clear
        DisconnectServer opcServer
        ReadValue = False
        Exit Function
    End If

    group.IsActive = True
    group.IsSubscribed = False

    Dim opcItem
    Err.Clear
    Set opcItem = group.OPCItems.AddItem(itemId, 1)
    If Err.Number <> 0 Then
        lastErrorOut = "Add read item failed: " & Err.Description
        Err.Clear
        DisconnectServer opcServer
        ReadValue = False
        Exit Function
    End If

    WScript.Sleep delayMs

    Dim cacheOk, deviceOk
    cacheOk = False
    deviceOk = False

    If readSource = "cache" Or readSource = "both" Or readSource = "" Then
        cacheOk = ReadAndReport(opcItem, OPC_CACHE, "cache", expectedText)
    End If

    If readSource = "device" Or readSource = "both" Then
        deviceOk = ReadAndReport(opcItem, OPC_DEVICE, "device", expectedText)
    End If

    DisconnectServer opcServer

    If expectedText = "" Then
        ReadValue = (cacheOk Or deviceOk)
    Else
        ReadValue = (cacheOk Or deviceOk)
    End If
End Function

Function ReadAndReport(opcItem, source, label, expectedText)
    On Error Resume Next

    Dim value, quality, timestamp, actualBool, expectedBool
    Err.Clear
    opcItem.Read source, value, quality, timestamp
    If Err.Number <> 0 Then
        WScript.Echo "READ " & label & " failed: " & Err.Description
        Err.Clear
        ReadAndReport = False
        Exit Function
    End If

    actualBool = NormalizeBool(value)
    WScript.Echo "READ " & label & ": raw=" & CStr(value) _
        & ", bool=" & BoolText(actualBool) _
        & ", quality=" & CStr(quality) _
        & ", timestamp=" & CStr(timestamp)

    If expectedText = "" Then
        ReadAndReport = True
        Exit Function
    End If

    expectedBool = (expectedText = "on" Or expectedText = "true" Or expectedText = "1")
    ReadAndReport = (IsGoodQuality(quality) And actualBool = expectedBool)
End Function

Function ConnectServer(serverName, ByRef lastErrorOut)
    On Error Resume Next

    Dim opcServer
    Set opcServer = CreateOpcServer(automationIds, lastErrorOut)
    If opcServer Is Nothing Then
        If Len(lastErrorOut) = 0 Then
            lastErrorOut = "OPC Automation Wrapper is not registered"
        End If
        Set ConnectServer = Nothing
        Exit Function
    End If

    Err.Clear
    opcServer.Connect serverName
    If Err.Number <> 0 Then
        lastErrorOut = "Connect " & serverName & " failed: " & Err.Description
        Err.Clear
        Set ConnectServer = Nothing
        Exit Function
    End If

    Set ConnectServer = opcServer
End Function

Sub DisconnectServer(opcServer)
    On Error Resume Next
    opcServer.Disconnect
End Sub

Function NormalizeBool(value)
    If VarType(value) = vbBoolean Then
        NormalizeBool = CBool(value)
    ElseIf IsNumeric(value) Then
        NormalizeBool = (CDbl(value) <> 0)
    Else
        Dim text
        text = LCase(Trim(CStr(value)))
        NormalizeBool = (text = "true" Or text = "on" Or text = "1")
    End If
End Function

Function IsGoodQuality(quality)
    If IsNumeric(quality) Then
        IsGoodQuality = ((CLng(quality) And OPC_QUALITY_GOOD) = OPC_QUALITY_GOOD)
    Else
        IsGoodQuality = False
    End If
End Function

Function BoolText(value)
    If CBool(value) Then
        BoolText = "ON"
    Else
        BoolText = "OFF"
    End If
End Function

Function CreateOpcServer(automationList, ByRef lastErrorOut)
    Dim automationId, obj
    For Each automationId In automationList
        automationId = Trim(CStr(automationId))
        If Len(automationId) > 0 Then
            Err.Clear
            Set obj = CreateObject(automationId)
            If Err.Number = 0 Then
                Set CreateOpcServer = obj
                Exit Function
            End If
            lastErrorOut = "CreateObject(" & automationId & ") failed: " & Err.Description
            Err.Clear
        End If
    Next
    Set CreateOpcServer = Nothing
End Function

Function UniqueGroupName(prefix)
    Randomize
    UniqueGroupName = prefix & "_" & Replace(CStr(Timer), ".", "_") & "_" & CStr(Int(Rnd * 100000))
End Function

Function ReadTextEnv(envBlock, name, defaultValue)
    Dim value
    value = envBlock(name)
    If Len(value) = 0 Then
        ReadTextEnv = defaultValue
    Else
        ReadTextEnv = value
    End If
End Function

Function ReadIntEnv(envBlock, name, defaultValue)
    Dim value
    value = envBlock(name)
    If IsNumeric(value) Then
        ReadIntEnv = CLng(value)
    Else
        ReadIntEnv = defaultValue
    End If
End Function

Function SplitList(value, separator)
    SplitList = Split(value, separator)
End Function

Sub Fail(message)
    WScript.StdErr.WriteLine message
    WScript.Quit 1
End Sub
