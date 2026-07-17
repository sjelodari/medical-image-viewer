' Medical Image Viewer - quiet launcher for Windows (no console window).
' First run shows a window so you can see the one-time setup progress;
' after that it launches silently. Use the "Quit" button in the app to stop it.

Set fso = CreateObject("Scripting.FileSystemObject")
Set sh  = CreateObject("WScript.Shell")

scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = scriptDir

' 1 = normal window (first-time setup), 0 = hidden (already set up)
If fso.FolderExists(scriptDir & "\.venv") And fso.FileExists(scriptDir & "\.venv\.deps_ok") Then
  windowStyle = 0
Else
  windowStyle = 1
End If

sh.Run """" & scriptDir & "\start.bat""", windowStyle, False
